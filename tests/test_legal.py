"""Termos de Uso, Política de Privacidade e aceite no cadastro e no convite.
Rodar da raiz: .venv/Scripts/python tests/test_legal.py"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import legal  # noqa: E402
from app.db import SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    # ---- páginas públicas, nos três idiomas
    v = novo()
    assert v.get("/termos").headers["location"] == "/terms"
    assert v.get("/privacidade").headers["location"] == "/privacy"
    t = v.get("/terms").text
    assert "Termos de Uso" in t and "Não somos o seu processador de folha" in t and "vale a" in t
    v.get("/idioma/en?volta=/terms")
    t = v.get("/terms").text
    assert "Terms of Use" in t and "We are not your payroll processor" in t and "State of Florida" in t
    p = v.get("/privacy").text
    assert "We never read location when the app is closed" in p and "<strong>Brazil</strong>" in p and "OpenStreetMap" in p
    assert "We do not use face recognition" in p
    v.get("/idioma/es?volta=/privacy")
    assert "Nunca leemos la ubicación con la app cerrada" in v.get("/privacy").text
    for pag in ("/entrar", "/cadastro", "/"):
        h = novo().get(pag).text
        assert 'href="/terms"' in h and 'href="/privacy"' in h, pag

    # ---- cadastro: sem aceitar, não cria; aceitando, guarda data e versão
    m = novo()
    dados = {"empresa": "Plataforma", "nome": "Dono", "email": "dono@p.com", "senha": "segredo1"}
    r = m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), **dados})
    assert r.status_code == 400 and "aceite os Termos de Uso" in r.text
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", **dados})
    with SessionLocal() as db:
        dono = db.scalar(select(Usuario).where(Usuario.email == "dono@p.com"))
        assert dono.termos_versao == legal.VERSAO and dono.termos_em is not None
        assert dono.is_admin

    # ---- Master -> Plataforma: dados legais aparecem nas páginas
    tm = csrf(m, "/master/plataforma")
    r = m.post("/master/plataforma", data={"csrf": tm, "nome": "Data Worke", "dias_teste": "7", "carencia_dias": "3",
                                           "carencia_teste_dias": "0", "cadastro_aberto": "1", "email_legal": "isso não"})
    assert "inválido" in m.get("/master/plataforma").text
    m.post("/master/plataforma", data={"csrf": tm, "nome": "Data Worke", "dias_teste": "7", "carencia_dias": "3",
                                       "carencia_teste_dias": "0", "cadastro_aberto": "1",
                                       "razao_social": "Data Worke LLC", "email_legal": "Privacy@Example.com",
                                       "estado_lei": "Texas", "pais_servidores": "United States",
                                       "endereco": "1 Main St, Austin, TX"})
    e = novo()
    e.get("/idioma/en?volta=/terms")
    t = e.get("/terms").text
    assert "Data Worke LLC" in t and "State of Texas" in t and "privacy@example.com" in t and "1 Main St, Austin, TX" in t
    assert "<strong>United States</strong>" in e.get("/privacy").text

    # ---- convite do funcionário: aviso com os links e aceite registrado
    tg = csrf(m, "/painel/equipe")
    m.post("/painel/equipe", data={"csrf": tg, "nome": "Ana Souza", "celular": "3055550001"})
    with SessionLocal() as db:
        token = db.scalar(select(Usuario.convite_token).where(Usuario.celular == "3055550001"))
    a = novo()
    pagina = a.get(f"/convite/{token}").text
    assert "Ao entrar, você concorda com os" in pagina and 'href="/privacy"' in pagina
    a.post(f"/convite/{token}", data={"csrf": csrf(a, f"/convite/{token}"), "senha": "senha123", "consentimento": "1"})
    with SessionLocal() as db:
        ana = db.scalar(select(Usuario).where(Usuario.celular == "3055550001"))
        assert ana.termos_versao == legal.VERSAO and ana.consentimento_em is not None
    print("OK: termos de uso e privacidade passou")


if __name__ == "__main__":
    main()
