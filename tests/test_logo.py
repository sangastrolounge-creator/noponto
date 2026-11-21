"""Logo da empresa em Configurações e nos impressos.
Rodar da raiz: .venv/Scripts/python tests/test_logo.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import logo_empresa, push  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Empresa, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_usuarios = lambda lista: avisos.extend(lista)


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def envia(cli, token, nome, dados, tipo="image/png", **extra):
    return cli.post("/painel/config/logo", data={"csrf": token, **extra}, files={"arquivo": (nome, dados, tipo)})


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar", nome="Gil", email="gil@b.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    tela = g.get("/painel/config").text
    assert 'id="logo"' in tela and 'enctype="multipart/form-data"' in tela and "Sem logo" in tela

    # ---- recusas: vazio, grande demais, arquivo que não é imagem (mesmo com nome .png)
    envia(g, tg, "x.png", b"")
    assert "Escolha a imagem" in g.get("/painel/config").text
    envia(g, tg, "g.png", PNG + b"0" * (1024 * 1024))
    assert "passa de 1 MB" in g.get("/painel/config").text
    envia(g, tg, "f.png", b"<svg onload=alert(1)></svg>")
    assert "JPG, PNG ou WebP" in g.get("/painel/config").text
    with SessionLocal() as db:
        assert db.get(Empresa, 1).logo == ""

    # ---- envio certo: guarda, mostra na configuração e nos impressos, e é servida
    envia(g, tg, "logo.png", PNG)
    with SessionLocal() as db:
        nome = db.get(Empresa, 1).logo
    assert logo_empresa.NOME.match(nome), nome
    assert f'src="/logos/{nome}"' in g.get("/painel/config").text
    r = novo().get(f"/logos/{nome}")
    assert r.status_code == 200 and r.content == PNG and r.headers["x-content-type-options"] == "nosniff"
    assert novo().get("/logos/../teste.db").status_code == 404 and novo().get("/logos/1-abc.png").status_code == 404
    posta(g, "/painel/equipe", tg, nome="Ana Souza", celular="98999990001")
    with SessionLocal() as db:
        aid = db.scalar(select(Usuario.id).where(Usuario.nome == "Ana Souza"))
    for url in (f"/painel/folha?func={aid}", "/painel/fechamento/resumo", "/painel/fechamento/horas"):
        assert f'class="logo-impresso" src="/logos/{nome}"' in g.get(url).text, url

    # ---- trocar apaga a antiga; tirar apaga tudo
    envia(g, tg, "nova.png", PNG)
    with SessionLocal() as db:
        nova = db.get(Empresa, 1).logo
    assert nova != nome and not (logo_empresa.PASTA / nome).exists() and (logo_empresa.PASTA / nova).exists()
    posta(g, "/painel/config/logo", tg, remover="1")
    with SessionLocal() as db:
        assert db.get(Empresa, 1).logo == ""
    assert not (logo_empresa.PASTA / nova).exists()
    assert "logo-impresso" not in g.get("/painel/fechamento/horas").text

    # ---- funcionário não troca a logo
    with SessionLocal() as db:
        convite = db.get(Usuario, aid).convite_token
    ana = novo()
    posta(ana, f"/convite/{convite}", csrf(ana, f"/convite/{convite}"), senha="senha123", consentimento="1")
    r = envia(ana, csrf(ana, "/app"), "logo.png", PNG)
    with SessionLocal() as db:
        assert db.get(Empresa, 1).logo == ""

    print("OK: logo da empresa passou")


if __name__ == "__main__":
    main()
