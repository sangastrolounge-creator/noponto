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
    assert "Termos de Uso" in t and "Estes Termos seguem as leis do Brasil" in t and "Código de Defesa do Consumidor" in t
    p = v.get("/privacy").text
    assert "A localização nunca é lida com o aplicativo fechado" in p and "<strong>Brasil</strong>" in p
    assert "Não fazemos reconhecimento facial" in p and "OpenStreetMap" in p and "art. 33 da LGPD" in p
    v.get("/idioma/en?volta=/terms")
    assert "Estes Termos seguem as leis do Brasil" in v.get("/terms").text   # o texto legal fica em português
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
    r = m.post("/master/plataforma", data={"csrf": tm, "nome": "No Ponto", "dias_teste": "7", "carencia_dias": "3",
                                           "carencia_teste_dias": "0", "cadastro_aberto": "1", "email_legal": "isso não"})
    assert "inválido" in m.get("/master/plataforma").text
    m.post("/master/plataforma", data={"csrf": tm, "nome": "No Ponto", "dias_teste": "7", "carencia_dias": "3",
                                       "carencia_teste_dias": "0", "cadastro_aberto": "1",
                                       "razao_social": "No Ponto Tecnologia Ltda, CNPJ 00.000.000/0001-00",
                                       "email_legal": "Privacidade@Example.com", "estado_lei": "São Luís/MA",
                                       "pais_servidores": "Brasil", "endereco": "Rua 1, São Luís/MA"})
    e = novo()
    t = e.get("/terms").text
    assert "No Ponto Tecnologia Ltda" in t and "da comarca de São Luís/MA" in t
    assert "privacidade@example.com" in t and "Rua 1, São Luís/MA" in t
    assert "<strong>Brasil</strong>" in e.get("/privacy").text

    # ---- convite do funcionário: aviso com os links e aceite registrado
    tg = csrf(m, "/painel/equipe")
    m.post("/painel/equipe", data={"csrf": tg, "nome": "Ana Souza", "celular": "98999990001"})
    with SessionLocal() as db:
        token = db.scalar(select(Usuario.convite_token).where(Usuario.celular == "98999990001"))
    a = novo()
    pagina = a.get(f"/convite/{token}").text
    assert "Ao entrar, você concorda com os" in pagina and 'href="/privacy"' in pagina
    a.post(f"/convite/{token}", data={"csrf": csrf(a, f"/convite/{token}"), "senha": "senha123", "consentimento": "1"})
    with SessionLocal() as db:
        ana = db.scalar(select(Usuario).where(Usuario.celular == "98999990001"))
        assert ana.termos_versao == legal.VERSAO and ana.consentimento_em is not None
    print("OK: termos de uso e privacidade passou")


if __name__ == "__main__":
    main()
