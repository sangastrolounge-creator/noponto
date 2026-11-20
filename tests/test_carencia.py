"""Carência ajustável: depois do vencimento da assinatura e depois do fim do teste grátis.
Rodar da raiz: .venv/Scripts/python tests/test_carencia.py"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app.core import fim_da_carencia, situacao  # noqa: E402
from app.db import Empresa, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

INICIO = datetime(2026, 11, 18, 12, 0)
relogio = {"agora": INICIO}
banco.agora = lambda: relogio["agora"]


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def sit(eid):
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        return situacao(e), fim_da_carencia(e)


def plataforma(m, tm, carencia, carencia_teste):
    return m.post("/master/plataforma", data={"csrf": tm, "nome": "Data Worke", "dias_teste": "7",
                                              "carencia_dias": str(carencia),
                                              "carencia_teste_dias": str(carencia_teste),
                                              "cadastro_aberto": "1"})


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    c = TestClient(app, follow_redirects=False)
    c.post("/cadastro", data={"csrf": csrf(c, "/cadastro"), "aceito": "1", "empresa": "Taco", "nome": "Gil",
                              "email": "gil@t.com", "senha": "segredo1"})
    with SessionLocal() as db:
        eid = db.scalar(select(Empresa.id).where(Empresa.nome == "Taco"))
    fim_teste = INICIO + timedelta(days=7)
    tm = csrf(m, "/master/plataforma")
    tela = m.get("/master/plataforma").text
    assert 'name="carencia_dias" min="0" max="30" value="3"' in tela   # padrão: 3 dias
    assert 'name="carencia_teste_dias" min="0" max="30" value="0"' in tela

    # ---- barra do teste grátis no menu: só para quem está em teste
    tela = c.get("/painel").text
    assert "contagem-teste" in tela and "Teste grátis: 7 dia(s)" in tela and 'style="width:100%"' in tela
    assert "contagem-teste" not in m.get("/painel").text          # cortesia: sem barra
    relogio["agora"] = INICIO + timedelta(days=5)
    tela = c.get("/painel").text
    assert "contagem-teste urgente" in tela and "Teste grátis: 2 dia(s)" in tela
    relogio["agora"] = INICIO
    # ---- teste grátis sem carência: fecha no fim do teste
    relogio["agora"] = fim_teste + timedelta(hours=1)
    assert sit(eid)[0] == "vencida"
    assert c.get("/painel/escala").headers["location"] == "/painel/assinatura"
    # ---- com 2 dias de carência no teste: ainda usa, com o aviso
    plataforma(m, tm, 3, 2)
    s, fim = sit(eid)
    assert s == "teste" and fim == fim_teste + timedelta(days=2)
    assert c.get("/painel/escala").status_code == 200
    assert "Seu teste grátis terminou em 25/11/2026" in c.get("/painel").text
    assert "27/11/2026" in c.get("/painel").text
    relogio["agora"] = fim_teste + timedelta(days=2, hours=1)
    assert sit(eid)[0] == "vencida"

    # ---- assinatura paga: 3 dias de carência (padrão), depois 7
    pago_ate = relogio["agora"] + timedelta(days=30)
    with SessionLocal() as db:
        db.get(Empresa, eid).pago_ate = pago_ate
        db.commit()
    relogio["agora"] = pago_ate + timedelta(days=2)
    s, fim = sit(eid)
    assert s == "ativa" and fim == pago_ate + timedelta(days=3)
    assert "O painel continua aberto até" in c.get("/painel").text
    relogio["agora"] = pago_ate + timedelta(days=4)
    assert sit(eid)[0] == "vencida"
    plataforma(m, tm, 7, 2)
    assert sit(eid)[0] == "ativa"
    relogio["agora"] = pago_ate + timedelta(days=8)
    assert sit(eid)[0] == "vencida"

    # ---- validação
    plataforma(m, tm, 45, 0)
    assert "entre 0 e 30" in m.get("/master/plataforma").text
    print("OK: carência passou")


if __name__ == "__main__":
    main()
