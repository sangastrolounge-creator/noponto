"""Gerente com alcance: ligado a setores e/ou locais, só vê e mexe nas pessoas deles.
Rodar da raiz: .venv/Scripts/python tests/test_alcance.py"""
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
os.environ["IDIOMA_PADRAO"] = "pt"
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app.db import Batida, Local, SessionLocal, Setor, Turno, Usuario  # noqa: E402
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


def pessoa(nome):
    with SessionLocal() as db:
        return db.scalar(select(Usuario).where(Usuario.nome == nome))


def linha_pessoa(html, nome):
    return f">{nome}<" in html


def main():
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Dora Dona",
          email="dora@diner.com", senha="segredo1")
    tg = csrf(g, "/painel")
    posta(g, "/painel/setores", tg, nome="Cozinha")
    posta(g, "/painel/setores", tg, nome="Salão")
    with SessionLocal() as db:
        coz = db.scalar(select(Setor).where(Setor.nome == "Cozinha")).id
        sal = db.scalar(select(Setor).where(Setor.nome == "Salão")).id
        eid = db.scalar(select(Usuario).where(Usuario.email == "dora@diner.com")).empresa_id
        db.add(Local(empresa_id=eid, nome="Filial Centro", lat=-2.5, lng=-44.3, raio_m=150))
        db.commit()
        filial = db.scalar(select(Local).where(Local.nome == "Filial Centro")).id
    for nome, cel, sid in (("Ana Souza", "3055550001", coz), ("Bruno Lima", "3055550002", sal),
                           ("Caio Reis", "3055550003", coz)):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel, cargo="Garçom")
        r = posta(g, f"/painel/equipe/{pessoa(nome).id}/ficha", tg, nome=nome, celular=cel, nivel="funcionario",
                  classificacao="clt", setor_id=str(sid), funcao_0="Garçom",
                  local_id=str(filial) if nome == "Caio Reis" else "")
        assert r.status_code == 303
    assert pessoa("Caio Reis").local_id == filial

    # ---- dona cadastra a Lia ligada só à Cozinha
    assert "Setores e locais que gerencia" in g.get("/painel/gerentes").text
    posta(g, "/painel/gerentes", tg, nome="Lia Lima", email="lia@diner.com", p_equipe="1", p_escala="1",
          p_ponto="1", p_fechamento="1", p_ausencias="1", **{f"s_{coz}": "1"})
    lia = pessoa("Lia Lima")
    assert lia.alcance_setores == str(coz) and lia.alcance_locais == ""
    token = lia.convite_token
    lc = novo()
    posta(lc, f"/convite/{token}", csrf(lc, f"/convite/{token}"), senha="senha123")
    lc = novo()
    posta(lc, "/entrar", csrf(lc, "/entrar"), login="lia@diner.com", senha="senha123")
    tl = csrf(lc, "/painel")

    # ---- Lia vê só a Cozinha
    eq = lc.get("/painel/equipe").text
    assert "Você gerencia só: Cozinha" in eq
    assert "Ana Souza" in eq and "Caio Reis" in eq and "Bruno Lima" not in eq and "Dora Dona" not in eq
    esc = lc.get("/painel/escala").text
    assert "Ana Souza" in esc and "Bruno Lima" not in esc
    ponto = lc.get("/painel/ponto").text
    assert "Ana Souza" in ponto and "Bruno Lima" not in ponto
    # e não mexe em quem está fora (nem pela ficha, nem pelo link direto)
    bruno = pessoa("Bruno Lima")
    r = lc.get(f"/painel/equipe/{bruno.id}/ficha")
    assert r.status_code == 303 and r.headers["location"] == "/painel"
    posta(lc, f"/painel/equipe/{bruno.id}", tl, acao="salvar", nome="Hacker", celular="3055550002", cargo="x")
    assert pessoa("Bruno Lima").nome == "Bruno Lima"
    r = posta(lc, "/painel/escala/turno", tl, usuario_id=str(bruno.id), data="2026-11-19",
              modo="trabalho", inicio="09:00", fim="17:00")
    with SessionLocal() as db:
        assert not db.scalar(select(Turno).where(Turno.usuario_id == bruno.id))
    # na Cozinha, sim
    ana = pessoa("Ana Souza")
    posta(lc, "/painel/escala/turno", tl, usuario_id=str(ana.id), data="2026-11-19",
          modo="trabalho", inicio="09:00", fim="17:00")
    with SessionLocal() as db:
        assert db.scalar(select(Turno).where(Turno.usuario_id == ana.id))

    # ---- quem a Lia cadastra entra na Cozinha; o celular de alguém de fora continua bloqueado
    posta(lc, "/painel/equipe", tl, nome="Duda Melo", celular="3055550004", cargo="Auxiliar")
    assert pessoa("Duda Melo").setor_id == coz
    posta(lc, "/painel/equipe", tl, nome="Outro", celular="3055550002", cargo="x")
    assert "já está cadastrado" in lc.get("/painel/equipe").text
    ficha = lc.get(f"/painel/equipe/{ana.id}/ficha").text
    assert "Salão" not in ficha and "Sem setor" not in ficha
    posta(lc, f"/painel/equipe/{ana.id}/ficha", tl, nome="Ana Souza", celular="3055550001", nivel="funcionario",
          classificacao="clt", setor_id=str(sal), funcao_0="Garçom")
    assert pessoa("Ana Souza").setor_id == coz              # não manda a pessoa para fora do alcance

    # ---- ações da empresa inteira ficam com o dono
    r = posta(lc, "/painel/fechamento", tl, de="2026-11-01", ate="2026-11-15")
    assert r.status_code == 303 and "empresa inteira" in lc.get("/painel/equipe").text
    posta(lc, "/painel/feriados", tl, data="2026-12-24", nome="Véspera")
    assert "empresa inteira" in lc.get("/painel/equipe").text
    posta(lc, "/painel/setores", tl, nome="Bar")
    with SessionLocal() as db:
        assert not db.scalar(select(Setor).where(Setor.nome == "Bar"))

    # ---- Cozinha + Filial Centro: só quem está nos dois
    posta(g, f"/painel/gerentes/{lia.id}", tg, acao="salvar", p_equipe="1", p_escala="1", p_ponto="1",
          **{f"s_{coz}": "1", f"l_{filial}": "1"})
    assert pessoa("Lia Lima").alcance_locais == str(filial)
    eq = lc.get("/painel/equipe").text
    assert "Caio Reis" in eq and "Ana Souza" not in eq and "Bruno Lima" not in eq

    # ---- sem nada marcado: volta a ver todos
    posta(g, f"/painel/gerentes/{lia.id}", tg, acao="salvar", p_equipe="1")
    eq = lc.get("/painel/equipe").text
    assert all(n in eq for n in ("Ana Souza", "Bruno Lima", "Caio Reis")) and "Você gerencia só" not in eq

    # ---- a dona vê todos o tempo todo
    assert all(n in g.get("/painel/equipe").text for n in ("Ana Souza", "Bruno Lima", "Caio Reis", "Lia Lima"))
    print("OK: alcance do gerente passou")


if __name__ == "__main__":
    main()
