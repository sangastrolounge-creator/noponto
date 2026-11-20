"""Gerentes com permissões: convite, menu e telas conforme as permissões, áreas só do dono,
desativação. Rodar da raiz: .venv/Scripts/python tests/test_gerentes.py"""
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/teste.db".replace("\\", "/")
os.environ["SECRET_KEY"] = "teste"
os.environ["IDIOMA_PADRAO"] = "pt"  # as conferências dos testes são em português
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app.db import SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 18, 14, 0)}
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def flash(cli, caminho):
    m = re.search(r'class="aviso[^"]*">([^<]+)<', cli.get(caminho).text)
    return m.group(1) if m else ""


def main():
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Dora Dona",
          email="dora@diner.com", senha="segredo1")
    tg = csrf(g, "/painel")
    with SessionLocal() as db:
        dona = db.scalar(select(Usuario).where(Usuario.email == "dora@diner.com"))
        assert dona.dono and dona.gestor
    posta(g, "/painel/equipe", tg, nome="Ana Souza", celular="3055550001", cargo="Server")
    assert "Gerentes e permissões" in g.get("/painel/equipe").text
    assert "Gerentes e permissões" in g.get("/painel/gerentes").text

    # ---- dona convida a Lia com Escala e Ponto
    posta(g, "/painel/gerentes", tg, nome="", email="x")
    assert "e-mail válido" in flash(g, "/painel/gerentes")
    posta(g, "/painel/gerentes", tg, nome="Lia Lima", email="dora@diner.com")
    assert "Já existe" in flash(g, "/painel/gerentes")
    posta(g, "/painel/gerentes", tg, nome="Lia Lima", email="LIA@diner.com", cargo="Shift lead",
          p_escala="1", p_ponto="1")
    with SessionLocal() as db:
        lia = db.scalar(select(Usuario).where(Usuario.email == "lia@diner.com"))
        assert lia.gestor and not lia.dono and lia.permissoes == "escala,ponto" and not lia.na_escala
        token, lia_id = lia.convite_token, lia.id
    tela = g.get("/painel/gerentes").text
    assert f"/convite/{token}" in tela and "Ainda não criou a senha" in tela
    assert "Lia Lima" not in g.get("/painel/equipe").text   # gerentes ficam na tela própria

    # ---- Lia cria a senha pelo link (gerente fora da escala não precisa do consentimento de GPS)
    lc = novo()
    pagina = lc.get(f"/convite/{token}").text
    assert "lia@diner.com" in pagina and 'name="consentimento"' not in pagina
    r = posta(lc, f"/convite/{token}", csrf(lc, f"/convite/{token}"), senha="senha123")
    assert r.status_code == 303 and r.headers["location"] == "/painel"
    lc = novo()
    r = posta(lc, "/entrar", csrf(lc, "/entrar"), login="lia@diner.com", senha="senha123")
    assert r.headers["location"] == "/painel"
    tl = csrf(lc, "/painel")
    menu = lc.get("/painel").text
    assert 'href="/painel/escala"' in menu and 'href="/painel/ponto"' in menu
    for fora in ("/painel/pagamento", "/painel/config", "/painel/equipe", "/painel/ausencias"):
        assert f'href="{fora}"' not in menu, fora
    assert lc.get("/painel/escala").status_code == 200
    assert lc.get("/painel/ponto").status_code == 200
    assert lc.get("/painel/ponto.csv").status_code == 200
    for caminho in ("/painel/pagamento", "/painel/config", "/painel/gerentes", "/painel/equipe",
                    "/painel/fechamento", "/painel/locais"):
        r = lc.get(caminho)
        assert r.status_code == 303 and r.headers["location"] == "/painel", caminho
    assert "não tem acesso" in flash(lc, "/painel")
    r = posta(lc, "/painel/gerentes", tl, nome="Intruso", email="i@diner.com", p_pagamento="1")
    assert r.headers["location"] == "/painel"
    with SessionLocal() as db:
        assert not db.scalar(select(Usuario).where(Usuario.email == "i@diner.com"))
    assert lc.get("/painel/assinatura").status_code == 200          # ver a assinatura pode
    r = posta(lc, "/painel/assinatura/pagar", tl, plano="1")       # pagar ou mudar, não
    assert r.headers["location"] == "/painel"

    # ---- dona dá Equipe; Lia não mexe nos dados da dona
    posta(g, f"/painel/gerentes/{lia_id}", tg, acao="salvar", p_escala="1", p_equipe="1")
    assert lc.get("/painel/equipe").status_code == 200
    assert lc.get("/painel/ponto").status_code == 303              # Ponto saiu
    posta(lc, f"/painel/equipe/{dona.id}", tl, acao="salvar", nome="Hacker", celular="", cargo="")
    assert "Só o dono" in flash(lc, "/painel/equipe")
    with SessionLocal() as db:
        assert db.get(Usuario, dona.id).nome == "Dora Dona"
    # a dona não pode ser editada pela tela de gerentes
    posta(g, f"/painel/gerentes/{dona.id}", tg, acao="desativar")
    with SessionLocal() as db:
        assert db.get(Usuario, dona.id).ativo

    # ---- desativar: Lia sai do painel na hora
    posta(g, f"/painel/gerentes/{lia_id}", tg, acao="desativar")
    r = lc.get("/painel")
    assert r.status_code == 303 and r.headers["location"] == "/entrar"
    posta(g, f"/painel/gerentes/{lia_id}", tg, acao="reativar")
    posta(g, f"/painel/gerentes/{lia_id}", tg, acao="novo_link")
    with SessionLocal() as db:
        lia = db.get(Usuario, lia_id)
        assert lia.convite_token and not lia.senha_hash
    print("OK: gerentes e permissões passou")


if __name__ == "__main__":
    main()
