"""Setores: cadastro, setor na ficha e filtros por setor na Equipe, Escala, Ponto, Relatórios
e Fechamento. Rodar da raiz: .venv/Scripts/python tests/test_setores.py"""
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
from app.db import SessionLocal, Setor, Usuario  # noqa: E402
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


def setor(nome):
    with SessionLocal() as db:
        return db.scalar(select(Setor).where(Setor.nome == nome))


def cadastrar(cli, email, empresa):
    posta(cli, "/cadastro", csrf(cli, "/cadastro"), aceito="1", empresa=empresa, nome="Dono " + empresa,
          email=email, senha="segredo1")
    return csrf(cli, "/painel")


def main():
    g = novo()
    tg = cadastrar(g, "dona@diner.com", "Diner")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bruno Lima", "3055550002"), ("Caio Reis", "3055550003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel, cargo="Garçom")

    # ---- cadastro
    assert "Nenhum setor ainda" in g.get("/painel/setores").text
    assert "/painel/setores" in g.get("/painel/equipe").text          # botão e menu
    posta(g, "/painel/setores", tg, nome="  Cozinha ")
    posta(g, "/painel/setores", tg, nome="Salão")
    posta(g, "/painel/setores", tg, nome="cozinha")
    assert "Já existe" in flash(g, "/painel/setores")
    posta(g, "/painel/setores", tg, nome="")
    assert "Dê um nome" in flash(g, "/painel/setores")
    coz, sal = setor("Cozinha"), setor("Salão")
    assert coz and sal and coz.nome == "Cozinha"

    # ---- setor na ficha
    ficha = g.get(f"/painel/equipe/{pessoa('Ana Souza').id}/ficha").text
    assert 'name="setor_id"' in ficha and "Cozinha" in ficha
    for nome, cel, sid in (("Ana Souza", "3055550001", coz.id), ("Bruno Lima", "3055550002", sal.id)):
        r = posta(g, f"/painel/equipe/{pessoa(nome).id}/ficha", tg, nome=nome, celular=cel,
                  nivel="funcionario", classificacao="clt", setor_id=str(sid), funcao_0="Garçom")
        assert r.status_code == 303, r.text[:500]
    assert pessoa("Ana Souza").setor_id == coz.id and pessoa("Bruno Lima").setor_id == sal.id
    assert pessoa("Caio Reis").setor_id is None
    tela = g.get("/painel/setores").text
    assert "1 pessoa(s)" in tela and "ainda sem setor" in tela

    # ---- Equipe
    eq = g.get(f"/painel/equipe?setor={coz.id}").text
    assert "Ana Souza" in eq and "Bruno Lima" not in eq and "Caio Reis" not in eq
    eq = g.get("/painel/equipe?setor=-1").text
    assert "Caio Reis" in eq and "Ana Souza" not in eq
    assert all(n in g.get("/painel/equipe").text for n in ("Ana Souza", "Bruno Lima", "Caio Reis"))

    # ---- Escala: agrupa por setor; o filtro fica guardado
    esc = g.get("/painel/escala").text
    grupos = re.findall(r'class="grupo-setor"><td colspan="\d+">([^<]+)<', esc)
    assert grupos == ["Cozinha", "Salão", "Sem setor"], grupos
    esc = g.get(f"/painel/escala?setor={sal.id}").text
    assert "Bruno Lima" in esc and "Ana Souza" not in esc and "grupo-setor" not in esc
    esc = g.get("/painel/escala").text                                   # sem ?setor: o guardado
    assert "Bruno Lima" in esc and "Ana Souza" not in esc
    assert "Ana Souza" in g.get("/painel/escala?setor=0").text

    # ---- Ponto, Relatórios, Fechamento
    p = g.get(f"/painel/ponto?setor={coz.id}").text
    assert re.search(r'<option value="\d+"[^>]*>Ana Souza</option>', p) and not re.search(r'<option value="\d+"[^>]*>Bruno Lima</option>', p)
    assert f"setor={coz.id}" in p                                         # CSV e calendário mantêm o filtro
    assert g.get(f"/painel/ponto.csv?setor={coz.id}").status_code == 200
    assert g.get(f"/painel/relatorios?setor={coz.id}").status_code == 200
    assert g.get(f"/painel/relatorios?setor={coz.id}&formato=csv").status_code == 200
    for nome in ("Ana Souza", "Bruno Lima"):
        r = posta(g, "/painel/escala/turno", tg, usuario_id=str(pessoa(nome).id), data="2026-11-16",
                  modo="trabalho", inicio="09:00", fim="17:00")
        assert r.status_code == 303
    per = "de=2026-11-01&ate=2026-11-17"
    fe = g.get(f"/painel/fechamento?{per}&setor={coz.id}").text
    assert '"nome-forte">Ana Souza<' in fe and '"nome-forte">Bruno Lima<' not in fe
    assert '"nome-forte">Bruno Lima<' in g.get(f"/painel/fechamento?{per}&setor=0").text
    assert "equipe inteira" in fe and f"horas?de=" in fe and f"setor={coz.id}" in fe
    hr = g.get(f"/painel/fechamento/horas?{per}&setor={coz.id}").text
    assert "Setor: Cozinha" in hr and "Ana Souza" in hr and "Bruno Lima" not in hr
    assert "Setor: Cozinha" in g.get(f"/painel/fechamento/resumo?{per}&setor={coz.id}").text

    # ---- outra empresa não usa nem enxerga os setores daqui
    h = novo()
    th = cadastrar(h, "hugo@outro.com", "Outro")
    posta(h, "/painel/equipe", th, nome="Iris Neves", celular="3055550009", cargo="Caixa")
    iris = pessoa("Iris Neves")
    posta(h, f"/painel/equipe/{iris.id}/ficha", th, nome="Iris Neves", celular="3055550009",
          nivel="funcionario", classificacao="clt", setor_id=str(coz.id))
    assert pessoa("Iris Neves").setor_id is None
    assert "Iris Neves" in h.get(f"/painel/equipe?setor={coz.id}").text  # id alheio = sem filtro
    assert h.post(f"/painel/setores/{coz.id}", data={"csrf": th, "acao": "excluir"}).headers["location"] == "/painel"
    assert setor("Cozinha")

    # ---- renomear, desativar, excluir
    posta(g, f"/painel/setores/{coz.id}", tg, acao="salvar", nome="Salão")
    assert "Já existe" in flash(g, "/painel/setores")
    posta(g, f"/painel/setores/{coz.id}", tg, acao="salvar", nome="Cozinha quente")
    assert setor("Cozinha quente")
    posta(g, f"/painel/setores/{coz.id}", tg, acao="desativar")
    assert not setor("Cozinha quente").ativo
    assert "Cozinha quente" in g.get(f"/painel/equipe/{pessoa('Ana Souza').id}/ficha").text  # ainda é o dela
    assert "Cozinha quente" not in g.get(f"/painel/equipe/{pessoa('Caio Reis').id}/ficha").text
    posta(g, f"/painel/setores/{coz.id}", tg, acao="excluir")
    assert not setor("Cozinha quente") and pessoa("Ana Souza").setor_id is None
    assert "ficaram sem setor" in flash(g, "/painel/setores")
    print("OK: setores passou")


if __name__ == "__main__":
    main()
