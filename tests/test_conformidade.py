"""Central de conformidade (CLT): junta as conferências e dá uma nota.
Rodar da raiz: .venv/Scripts/python tests/test_conformidade.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import conformidade  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Empresa, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
LAT, LNG = -2.5307, -44.3068


def as_(hora, d):   # Brasília: UTC-3
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def itens():
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Padaria A"))
        u = db.scalar(select(Usuario).where(Usuario.email == "gil@a.com"))
        c = conformidade.verificar(db, e, u)
    return c, {i["chave"]: i for i in c["itens"]}


def main():
    as_("09:00", SEG + timedelta(days=2))
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Padaria A",
                              "nome": "Gil", "email": "gil@a.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bia Reis", "98999990002")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})

    # ---- empresa ainda sem local, sem salários e sem admissão: pede atenção
    c, it = itens()
    assert it["cadastro"]["situacao"] == "atencao" and "Local da empresa" in it["cadastro"]["detalhes"][0]
    assert it["sem_valor"]["situacao"] == "atencao"
    assert it["ferias"]["situacao"] == "atencao" and "sem data de admissão" in it["ferias"]["detalhes"][-1]
    assert it["refeicao"]["situacao"] == "ok"

    g.post("/painel/config", data={"csrf": tg, "nome": "Padaria A", "lat": str(LAT), "lng": str(LNG),
                                   "raio_m": "150", "tolerancia_min": "10", "fuso": "America/Sao_Paulo",
                                   "bloquear_fora": "1", "ajuste_dias": "7", "fotos_dias": "45",
                                   "controle": "banco", "intervalo_padrao_min": "60", "intervalo_auto": "1",
                                   "tolerancia_dia_min": "10"})
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        ids = {k: u.id for k, u in us.items()}
        us["Ana"].salario_centavos = 100000          # abaixo do mínimo de 220 h
        us["Bia"].salario_centavos = 200000
        us["Ana"].data_admissao = date(2024, 10, 1)  # férias do 1º período venceram em 30/09/2026
        us["Bia"].data_admissao = date(2026, 1, 5)
        db.commit()

    c, it = itens()
    assert it["minimo"]["situacao"] == "problema" and "Ana Souza" in it["minimo"]["detalhes"][0]
    assert it["ferias"]["situacao"] == "problema" and "Ana Souza" in it["ferias"]["detalhes"][0]
    assert c["problemas"] == 2 and 0 < c["nota"] < 100

    # ---- página
    t = g.get("/painel/conformidade").text
    assert "Conformidade" in t and "2 problema(s) para resolver" in t and 'id="c-minimo"' in t
    assert t.index('id="c-minimo"') < t.index('id="c-ponto"')         # problemas primeiro
    assert 'href="/painel/conformidade"' in t                           # item no menu

    # ---- corrige: salário acima do mínimo e férias marcadas
    with SessionLocal() as db:
        db.get(Usuario, ids["Ana"]).salario_centavos = 180000
        db.commit()
    g.post("/painel/ferias", data={"csrf": tg, "usuario_id": ids["Ana"], "inicio": "2027-01-04",
                                   "fim": "2027-02-02", "abono": "0", "confirmar": "1"})
    c, it = itens()
    assert it["minimo"]["situacao"] == "ok" and it["ferias"]["situacao"] == "ok", it["ferias"]
    assert c["problemas"] == 0

    # ---- gerente sem permissão de ponto não abre
    g.post("/painel/gerentes", data={"csrf": tg, "nome": "Lia", "email": "lia@a.com", "p_mural": "1"})
    with SessionLocal() as db:
        tk = db.scalar(select(Usuario.convite_token).where(Usuario.email == "lia@a.com"))
    lia = novo()
    lia.post(f"/convite/{tk}", data={"csrf": csrf(lia, f"/convite/{tk}"), "senha": "senha123"})
    assert lia.get("/painel/conformidade").status_code in (302, 303)
    print("OK: central de conformidade passou")


if __name__ == "__main__":
    main()
