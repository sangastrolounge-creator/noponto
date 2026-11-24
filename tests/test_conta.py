"""Minha conta e exclusão de conta (exigência das lojas) e assetlinks.json do app Android.
Rodar da raiz: python tests/test_conta.py"""
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Alerta, Batida, Empresa, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 23, 15, 0)}
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def flash(cli, caminho):
    m = re.search(r'class="aviso[^"]*">([^<]+)<', cli.get(caminho).text)
    return m.group(1) if m else ""


def entra_convite(nome):
    with SessionLocal() as db:
        tok = db.scalar(select(Usuario).where(Usuario.nome == nome)).convite_token
    c = novo()
    c.post(f"/convite/{tok}", data={"csrf": csrf(c, f"/convite/{tok}"), "senha": "senha123", "consentimento": "1"})
    return c


def main():
    m = novo()
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Bar Mar", "nome": "Gil",
                              "email": "gil@bar.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/equipe")
    g.post("/painel/equipe", data={"csrf": tg, "nome": "Ana Souza", "celular": "98999990001"})
    g.post("/painel/equipe", data={"csrf": tg, "nome": "Bia Lima", "celular": "98999990002"})
    ana, bia = entra_convite("Ana Souza"), entra_convite("Bia Lima")

    # ---- página pública e links
    pub = novo().get("/excluir-conta")
    assert pub.status_code == 200 and "Como excluir a sua conta" in pub.text and "LGPD, art. 16" in pub.text
    assert "/excluir-conta" in novo().get("/privacy").text
    assert 'href="/conta"' in ana.get("/app").text and 'href="/conta"' in g.get("/painel").text

    # ---- Ana (sem registros): senha errada não apaga; certa apaga o cadastro inteiro
    ta = csrf(ana, "/conta")
    assert "Excluir minha conta" in ana.get("/conta").text
    ana.post("/conta/excluir", data={"csrf": ta, "senha": "errada", "confirmo": "1"})
    assert "Senha incorreta" in flash(ana, "/conta")
    ana.post("/conta/excluir", data={"csrf": ta, "senha": "senha123"})
    assert "Marque a caixa" in flash(ana, "/conta")
    r = ana.post("/conta/excluir", data={"csrf": ta, "senha": "senha123", "confirmo": "1"})
    assert r.headers["location"] == "/entrar"
    with SessionLocal() as db:
        assert not db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza"))
    assert ana.get("/app").headers["location"] == "/entrar"

    # ---- Bia (com marcação): acesso encerrado, registros guardados, gestor avisado
    with SessionLocal() as db:
        b = db.scalar(select(Usuario).where(Usuario.nome == "Bia Lima"))
        bid = b.id
        b.pin_hash, b.facial_descritor = "x", "[1]"
        regras.bater(db, b, None, None, None, aparelho="aparelho-bia-123", ua="teste")
    tb = csrf(bia, "/conta")
    bia.post("/conta/excluir", data={"csrf": tb, "senha": "senha123", "confirmo": "1"})
    with SessionLocal() as db:
        b = db.get(Usuario, bid)
        assert b and b.conta_excluida_em and b.senha_hash == "" and b.pin_hash is None and b.facial_descritor == ""
        assert db.scalar(select(Batida.id).where(Batida.usuario_id == bid))
        al = db.scalar(select(Alerta).where(Alerta.tipo == "conta_excluida"))
        assert al and "Bia Lima" in al.texto and "convite novo" in al.texto
    assert bia.get("/app").headers["location"] == "/entrar"
    c = novo()
    c.post("/entrar", data={"csrf": csrf(c, "/entrar"), "login": "98999990002", "senha": "senha123"})
    assert c.get("/app").headers["location"] == "/entrar"
    # convite novo devolve o acesso
    with SessionLocal() as db:
        db.get(Usuario, bid).convite_token = "convite-novo-da-bia-123456"
        db.commit()
    c = novo()
    c.post("/convite/convite-novo-da-bia-123456",
           data={"csrf": csrf(c, "/convite/convite-novo-da-bia-123456"), "senha": "nova1234", "consentimento": "1"})
    assert c.get("/app").status_code == 200
    with SessionLocal() as db:
        assert db.get(Usuario, bid).conta_excluida_em is None

    # ---- dono: pedido registrado, conta continua até a plataforma concluir; Master vê
    tg = csrf(g, "/conta")
    assert "encerra a conta da empresa inteira" in g.get("/conta").text
    g.post("/conta/excluir", data={"csrf": tg, "senha": "segredo1", "confirmo": "1"})
    assert "Pedido registrado" in flash(g, "/conta")
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Bar Mar"))
        assert e.exclusao_pedida_em and e.ativo
        eid = e.id
    assert "Pedido de exclusão registrado" in g.get("/conta").text
    assert "Pediu exclusão em" in m.get("/master").text
    assert "O dono pediu o encerramento" in m.get(f"/master/empresas/{eid}").text

    # ---- assetlinks.json: vazio até o Master salvar; impressão digital conferida
    assert novo().get("/.well-known/assetlinks.json").json() == []
    tm = csrf(m, "/master/lojas")
    assert "br.com.dataworke.app" in m.get("/master/lojas").text
    m.post("/master/lojas", data={"csrf": tm, "android_pacote": "br.com.dataworke.app", "android_sha256": "AB:CD"})
    assert "Impressão digital inválida" in flash(m, "/master/lojas")
    dig = ":".join(["A1"] * 32)
    m.post("/master/lojas", data={"csrf": tm, "android_pacote": "br.com.dataworke.app",
                                  "android_sha256": dig.replace(":", "").lower() + "\n" + dig})
    j = novo().get("/.well-known/assetlinks.json").json()
    assert j[0]["target"] == {"namespace": "android_app", "package_name": "br.com.dataworke.app",
                              "sha256_cert_fingerprints": [dig]}, j
    assert g.get("/master/lojas").status_code in (303, 307, 403, 404)   # só o Master
    print("ok: conta e lojas")


if __name__ == "__main__":
    main()
