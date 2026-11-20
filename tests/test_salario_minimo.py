"""Salário mínimo federal, estadual e municipal: tabela, avisos e seletor de cidade.
Rodar da raiz: .venv/Scripts/python tests/test_salario_minimo.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import salario_minimo as sm  # noqa: E402
from app.db import SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 19, 15, 0)}
banco.agora = lambda: relogio["agora"]


def emp(proprio=0):
    return N(minimo_proprio_centavos=proprio)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def tabela():
    hoje = date(2026, 11, 19)
    m = sm.minimo
    assert m(emp(), hoje) == {"centavos": 162100, "fonte": "Salário mínimo nacional", "hora": 737}
    assert m(emp(), date(2025, 6, 1))["centavos"] == 151800
    assert m(emp(175000), hoje) == {"centavos": 175000, "fonte": "Piso informado pela empresa", "hora": 795}
    assert m(emp(150000), hoje)["centavos"] == 162100          # piso abaixo do nacional: vale o nacional
    p = sm.proxima_mudanca(emp(), date(2025, 12, 1))
    assert p["data"] == date(2026, 1, 1) and p["normal"]["centavos"] == 162100
    assert sm.proxima_mudanca(emp(), hoje) is None
    assert sm.proporcional(m(emp(), hoje), 180) == 132627          # jornada de 36 h por semana
    assert all(a[0] < b[0] for a, b in zip(sm.NACIONAL, sm.NACIONAL[1:]))


def telas():
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Padaria", "nome": "Gil",
                              "email": "gil@t.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    t = g.get("/painel/config").text
    assert 'id="salario-minimo"' in t
    assert "Em uso agora: R$ 1.621,00 por mês para 220 horas (Salário mínimo nacional), ou R$ 7,37 por hora." in t
    for nome, cel in (("Ana Souza", "98999990001"), ("Bia Reis", "98999990002"), ("Caio Lima", "98999990003")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        db.get(Usuario, ids["Caio"]).classificacao = "pj"        # PJ não entra na conferência
        db.get(Usuario, ids["Caio"]).salario_centavos = 100000
        db.commit()
    # Ana R$ 1.500 em 220 h (abaixo); Bia R$ 1.400 em 180 h (acima do proporcional de R$ 1.326,27)
    g.post("/painel/pagamento/dados", data={"csrf": tg, f"sal_{ids['Ana']}": "1.500,00", f"jor_{ids['Ana']}": "220",
                                            f"dep_{ids['Ana']}": "0", f"vt_{ids['Ana']}": "",
                                            f"sal_{ids['Bia']}": "1.400,00", f"jor_{ids['Bia']}": "180",
                                            f"dep_{ids['Bia']}": "0", f"vt_{ids['Bia']}": ""})
    t = g.get("/painel/pagamento").text
    assert "Atenção: salário abaixo do mínimo proporcional à jornada: Ana Souza (R$ 1.500,00 &lt; R$ 1.621,00)" in t, t[:0]
    painel = g.get("/painel").text
    alerta = painel.split('id="salario-minimo"')[1].split("</div>")[0]
    assert "abaixo do salário mínimo" in alerta and "Ana Souza" in alerta
    assert "Bia" not in alerta and "Caio" not in alerta
    # piso da categoria: R$ 1.750 por 220 h; Bia (180 h) precisa de R$ 1.431,82
    g.post("/painel/config/salario-minimo", data={"csrf": tg, "proprio": "1.750,00"})
    t = g.get("/painel/config").text
    assert "Em uso agora: R$ 1.750,00 por mês para 220 horas (Piso informado pela empresa)" in t
    with SessionLocal() as db:
        from app.db import Empresa
        nomes = [x["u"].nome for x in sm.abaixo(db, db.get(Empresa, 1), date(2026, 11, 19))]
    assert nomes == ["Ana Souza", "Bia Reis"], nomes
    g.post("/painel/config/salario-minimo", data={"csrf": tg, "proprio": "abc"})
    assert "Use o valor mensal do piso" in g.get("/painel/config").text


def main():
    tabela()
    telas()
    print("OK: salário mínimo passou")


if __name__ == "__main__":
    main()
