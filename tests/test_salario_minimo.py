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


def emp(estado, cidade="", proprio=0, proprio_g=0):
    return N(estado=estado, cidade_minimo=cidade, minimo_proprio_centavos=proprio,
             minimo_gorjeta_proprio_centavos=proprio_g)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def tabela():
    hoje = date(2026, 11, 19)
    m = sm.minimo
    assert m(emp("TX"), hoje) == {"centavos": 725, "fonte": "Federal"}
    assert m(emp("TX"), hoje, True)["centavos"] == 213
    assert m(emp("GA"), hoje)["centavos"] == 725           # estado abaixo do federal: vale o federal
    assert m(emp("FL"), hoje)["centavos"] == 1500 and m(emp("FL"), hoje, True)["centavos"] == 1198
    assert m(emp("FL"), date(2026, 9, 1))["centavos"] == 725   # antes de 30/09/2026 não há valor na tabela
    assert m(emp("CA"), hoje, True)["centavos"] == 1690    # sem tip credit
    assert m(emp("CA"), date(2027, 1, 1))["centavos"] == 1740
    assert m(emp("WA", "seattle"), hoje) == {"centavos": 2130, "fonte": "Seattle"}
    assert m(emp("WA", "seattle"), date(2027, 1, 2))["centavos"] == 2214
    assert m(emp("CA", "seattle"), hoje)["centavos"] == 1690   # cidade de outro estado é ignorada
    assert m(emp("OR", "ornaourbano"), hoje)["centavos"] == 1455  # região abaixo do padrão do estado
    assert m(emp("OR"), hoje)["centavos"] == 1555
    assert m(emp("IL", "chicago"), hoje, True)["centavos"] == 1432
    assert m(emp("NY", "nyc"), hoje, True)["centavos"] == 1135
    assert m(emp("NM", "lascruces"), hoje, True)["centavos"] == 1301 - 900  # crédito do estado
    assert m(emp("TX", "", 1000, 500), hoje) == {"centavos": 1000, "fonte": "Valor informado pela empresa"}
    p = sm.proxima_mudanca(emp("WA", "seattle"), hoje)
    assert p["data"] == date(2027, 1, 1) and p["normal"]["centavos"] == 2214
    assert sm.proxima_mudanca(emp("TX"), hoje) is None
    assert sm.proxima_mudanca(emp("CA", "sanfrancisco"), hoje) is None   # 17,40 do estado < 19,61 da cidade
    for estado, lista in sm.ESTADOS.items():
        assert all(a[0] < b[0] for a, b in zip(lista, lista[1:])), estado
        assert all(x[2] is None or x[2] <= x[1] for x in lista), estado
    for chave, (_, estado, lista, _) in sm.CIDADES.items():
        assert estado in sm.ESTADOS, chave


def telas():
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Taco", "nome": "Gil",
                              "email": "gil@t.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    g.post("/painel/config", data={"csrf": tg, "nome": "Taco", "lat": "47.6", "lng": "-122.3", "raio_m": "150",
                                   "tolerancia_min": "10", "fuso": "America/Los_Angeles", "bloquear_fora": "1",
                                   "ajuste_dias": "7", "fotos_dias": "45", "estado": "WA", "semana_inicio": "6",
                                   "frequencia": "weekly", "periodo_ancora": "2026-11-15",
                                   "intervalo_padrao_min": "30", "tolerancia_dia_min": "7",
                                   "pct_noturno": "0", "pct_feriado": "0"})
    t = g.get("/painel/config").text
    assert 'id="salario-minimo"' in t and "$17.13" in t and 'value="seattle"' in t and 'value="denver"' not in t
    for nome, cel in (("Ana Souza", "2065550001"), ("Bia Reis", "2065550002"), ("Caio Lima", "2065550003")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
    # Ana 18.00, Bia 17.50 (passa no estado, não em Seattle), Caio exempt
    g.post("/painel/pagamento/dados", data={"csrf": tg, f"taxa_{ids['Ana']}": "18.00", f"taxa_{ids['Bia']}": "17.50",
                                            f"taxa_{ids['Caio']}": "0", f"isento_{ids['Caio']}": "1",
                                            f"sal_{ids['Caio']}": "900.00"})
    painel = g.get("/painel").text
    assert "abaixo do salário mínimo" not in painel
    # o estado sobe para 17,73 em 01/01/2027: Bia (17,50) já aparece no aviso do aumento
    assert "A partir de 01/01/2027, o mínimo passa a $17.73" in painel and "Bia Reis $17.50" in painel
    assert "Ana Souza $18.00" not in painel
    # cidade: Seattle
    r = g.post("/painel/config/salario-minimo", data={"csrf": tg, "cidade": "denver"})
    assert "Escolha uma cidade do estado" in g.get("/painel/config").text
    g.post("/painel/config/salario-minimo", data={"csrf": tg, "cidade": "seattle"})
    t = g.get("/painel/config").text
    assert "Em uso agora: $21.30 por hora (Seattle)" in t and "A partir de 01/01/2027: $22.14" in t
    painel = g.get("/painel").text
    assert "2 valor(es) da hora abaixo do salário mínimo" in painel and "Ana Souza" in painel and "Bia Reis" in painel
    assert "Caio" not in painel.split('id="salario-minimo"')[1].split("</div>")[0]
    pag = g.get("/painel/pagamento").text
    assert pag.count("Abaixo do mínimo") == 2
    # salvar valores: aviso de erro com os nomes
    g.post("/painel/pagamento/dados", data={"csrf": tg, f"taxa_{ids['Ana']}": "22.00", f"taxa_{ids['Bia']}": "21.00",
                                            f"isento_{ids['Caio']}": "1", f"taxa_{ids['Caio']}": "0",
                                            f"sal_{ids['Caio']}": "900.00"})
    t = g.get("/painel/pagamento").text
    assert "Atenção: valor da hora abaixo do salário mínimo do local: Bia Reis ($21.00 &lt; $21.30)" in t, t[:0]
    painel = g.get("/painel").text
    assert "1 valor(es) da hora abaixo" in painel
    assert "A partir de 01/01/2027, o mínimo passa a $22.14" in painel and "Ana Souza $22.00" in painel
    # ficha: salvar um valor abaixo avisa
    ficha = g.get(f"/painel/equipe/{ids['Bia']}").text
    assert ficha
    # mínimo próprio: maior valor vale
    g.post("/painel/config/salario-minimo", data={"csrf": tg, "cidade": "", "proprio": "23.00"})
    assert "Em uso agora: $23.00 por hora (Valor informado pela empresa)" in g.get("/painel/config").text
    g.post("/painel/config/salario-minimo", data={"csrf": tg, "cidade": "", "proprio": "abc"})
    assert "Use valores em dólares" in g.get("/painel/config").text


def main():
    tabela()
    telas()
    print("OK: salário mínimo passou")


if __name__ == "__main__":
    main()
