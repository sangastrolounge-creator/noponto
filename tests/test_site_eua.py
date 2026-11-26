"""Site americano (dataworke.com) na plataforma unificada: página inicial, lista de espera,
cadastro de empresa americana e textos legais pelo país. Rodar da raiz: python tests/test_site_eua.py"""
import os
import re
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "SEM_AGENDADOR": "1", "ADMIN_EMAIL": "dono@plataforma.com"})
for k in ("ANTHROPIC_API_KEY", "ASAAS_API_KEY", "PUBLIC_URL", "IDIOMA_PADRAO"):
    os.environ.pop(k, None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import core  # noqa: E402
from app.db import Empresa, Lead, Plano, Plataforma, SessionLocal, Usuario  # noqa: E402
from app.legal import VERSAO, VERSAO_EUA  # noqa: E402
from app.main import app  # noqa: E402

EUA, BR = "http://dataworke.com", "http://dataworke.com.br"


def cli(base):
    return TestClient(app, base_url=base, follow_redirects=False)


def csrf(c, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(caminho).text).group(1)


def cadastro(c, empresa, nome, email):
    return c.post("/cadastro", data={"csrf": csrf(c, "/cadastro"), "aceito": "1", "empresa": empresa,
                                     "nome": nome, "email": email, "senha": "segredo1"})


def main():
    m = cli(BR)
    cadastro(m, "Plataforma", "Dono", "dono@plataforma.com")
    with SessionLocal() as db:
        db.add(Plano(nome="Starter US", preco_centavos=2900, limite_funcionarios=10, ativo=True, pais="US"))
        db.add(Plano(nome="Essencial BR", preco_centavos=5990, limite_funcionarios=10, ativo=True, pais="BR"))
        db.commit()

    # página inicial: o domínio americano abre o site dos EUA, em inglês, com os planos em dólar
    v = cli(EUA)
    tela = v.get("/").text
    assert 'lang="en"' in tela and "Starter US" in tela and "$29.00" in tela, tela[:1500]
    assert "Essencial BR" not in tela and "R$" not in tela
    with SessionLocal() as db:   # o nome "... Brasil" vira só "Data Worke" no site americano
        db.get(Plataforma, 1).nome = "Data Worke Brasil"
        db.commit()
    core.plataforma_mudou()
    assert "Data Worke Brasil" not in v.get("/").text and "<title>Data Worke" in v.get("/").text
    assert "Data Worke Brasil" not in v.get("/entrar").text
    assert "Data Worke Brasil" in cli(BR).get("/entrar").text
    tela_br = cli(BR).get("/").text
    assert "Starter US" not in tela_br and 'lang="pt-BR"' in tela_br
    # quem escolheu um idioma continua nele
    v.cookies.set("idioma", "es")
    assert 'lang="es"' in v.get("/").text
    v.cookies.clear()

    # cadastro fechado nos EUA: lista de espera vira lead do país EUA no CRM
    tela = v.get("/cadastro").text
    assert 'action="/lista-de-espera"' in tela
    tok = csrf(v, "/cadastro")
    r = v.post("/lista-de-espera", data={"csrf": tok, "empresa": "Taco Bar", "nome": "Gil",
                                         "email": "gil@taco.com", "telefone": "(305) 555-0123",
                                         "funcionarios": "12", "autorizo": "1"})
    assert r.status_code == 200
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.email == "gil@taco.com"))
        assert (ld.pais, ld.origem, ld.funcionarios, ld.telefone) == ("US", "espera", 12, "3055550123")
    assert v.post("/cadastro", data={"csrf": tok, "aceito": "1", "empresa": "X", "nome": "Y",
                                     "email": "y@x.com", "senha": "segredo1"}).status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(Usuario).where(Usuario.email == "y@x.com")) is None
    tela = m.get("/master/crm?pais=US").text
    assert "Taco Bar" in tela
    assert "Taco Bar" not in m.get("/master/crm?pais=BR").text

    # textos legais: os da empresa americana no domínio dos EUA, os brasileiros no Brasil
    assert "Terms of Use" in v.get("/terms").text
    assert "Termos de Uso" in cli(BR).get("/terms").text and "Terms of Use" not in cli(BR).get("/terms").text

    # com o cadastro americano aberto, a empresa nasce nos EUA com os padrões de lá
    with SessionLocal() as db:
        db.get(Plataforma, 1).cadastro_eua_aberto = True
        db.commit()
    core.plataforma_mudou()
    n = cli(EUA)
    assert 'action="/cadastro"' in n.get("/cadastro").text
    assert cadastro(n, "Burger Joint", "Ann", "ann@burger.com").status_code == 303
    with SessionLocal() as db:
        u = db.scalar(select(Usuario).where(Usuario.email == "ann@burger.com"))
        e = u.empresa
        assert (e.pais, e.estado, e.fuso, e.intervalo_padrao_min, e.pct_noturno) == ("US", "FL", "America/New_York", 30, 0)
        assert u.termos_versao == VERSAO_EUA
        ld = db.scalar(select(Lead).where(Lead.empresa_id == e.id))
        assert ld is None or ld.pais == "US"
    assert "California ★" in n.get("/painel/config").text
    assert "Terms of Use" in n.get("/terms").text

    # e o cadastro brasileiro continua brasileiro
    b = cli(BR)
    cadastro(b, "Bar do Zé", "Zé", "ze@bar.com")
    with SessionLocal() as db:
        u = db.scalar(select(Usuario).where(Usuario.email == "ze@bar.com"))
        assert (u.empresa.pais, u.termos_versao) == ("BR", VERSAO)
    print("OK: site dos EUA passou")


if __name__ == "__main__":
    main()
