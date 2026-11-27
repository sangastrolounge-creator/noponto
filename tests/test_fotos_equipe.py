"""Fotos da equipe (ficha, app, opção da empresa, quem pode ver), ícone do WhatsApp no lugar
do link escrito e letra menor na coluna Funcionário.
Rodar da raiz: python tests/test_fotos_equipe.py"""
import base64
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
for k in ("ADMIN_EMAIL", "PUBLIC_URL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app.core import gerar_hash  # noqa: E402
from app.db import Empresa, SessionLocal, Turno, Usuario  # noqa: E402
from app.main import app  # noqa: E402

HOJE = date(2026, 11, 18)
banco.agora = lambda: datetime(2026, 11, 18, 13, 0)
# PNG de 1x1 pixel
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
DADOS = "data:image/png;base64," + base64.b64encode(PNG).decode()


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def cadastra(empresa, nome, email):
    c = novo()
    posta(c, "/cadastro", csrf(c, "/cadastro"), aceito="1", empresa=empresa, nome=nome, email=email,
          senha="segredo1")
    return c


def main():
    g = cadastra("Bar do Zé", "Zé Silva", "ze@bar.com")
    outra = cadastra("Outro Bar", "Bia", "bia@outro.com")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/equipe", tg, nome="Ana Souza", celular="98999990001")
    with SessionLocal() as db:
        ana = db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza"))
        ana.senha_hash = gerar_hash("senha123")
        aid, eid = ana.id, ana.empresa_id
        seg = HOJE - timedelta(days=HOJE.weekday())
        db.add(Turno(empresa_id=eid, usuario_id=aid, data=seg, inicio="09:00", fim="17:00"))
        db.commit()

    # sem foto: iniciais na escala, letra menor na coluna e WhatsApp como ícone
    tela = g.get("/painel/escala").text
    assert 'class="col-func"' in tela and 'avatar-ini' in tela and ">AS<" in tela
    tela_eq = g.get("/painel/equipe").text
    assert "avatar-ini" in tela_eq

    # o gestor envia a foto na ficha
    tf = csrf(g, f"/painel/equipe/{aid}/ficha")
    assert 'data-foto-form' in g.get(f"/painel/equipe/{aid}/ficha").text
    assert posta(g, f"/painel/equipe/{aid}/foto", tf, dados="data:image/png;base64,xxx").status_code == 303
    with SessionLocal() as db:
        assert db.get(Usuario, aid).foto_perfil == ""          # imagem inválida não entra
    assert posta(g, f"/painel/equipe/{aid}/foto", tf, dados=DADOS).status_code == 303
    with SessionLocal() as db:
        nome = db.get(Usuario, aid).foto_perfil
    assert re.match(rf"^{eid}-{aid}-[0-9a-f]{{12}}\.png$", nome), nome
    tela = g.get("/painel/escala").text
    assert f'src="/equipe/foto/{nome}"' in tela
    assert f'src="/equipe/foto/{nome}"' in g.get("/painel/equipe").text
    r = g.get(f"/equipe/foto/{nome}")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert outra.get(f"/equipe/foto/{nome}").status_code == 404        # outra empresa não vê
    assert novo().get(f"/equipe/foto/{nome}").status_code in (302, 303, 307)   # sem login

    # a empresa desliga as fotos: some das telas (a ficha continua mostrando)
    tc = csrf(g, "/painel/config")
    assert 'id="fotos"' in g.get("/painel/config").text
    assert posta(g, "/painel/config/fotos", tc).status_code == 303
    with SessionLocal() as db:
        assert db.get(Empresa, eid).mostrar_fotos is False
    tela = g.get("/painel/escala").text
    assert "/equipe/foto/" not in tela and "avatar-ini" not in tela
    assert f'src="/equipe/foto/{nome}"' in g.get(f"/painel/equipe/{aid}/ficha").text
    posta(g, "/painel/config/fotos", tc, mostrar="1")

    # a própria pessoa troca e tira a foto pelo app
    a = novo()
    assert posta(a, "/entrar", csrf(a, "/entrar"), login="98999990001", senha="senha123").headers["location"] == "/app"
    ta = csrf(a, "/app/ajustes")
    assert "Minha foto" in a.get("/app/ajustes").text
    assert posta(a, "/app/foto", ta, dados=DADOS).status_code == 303
    with SessionLocal() as db:
        novo_nome = db.get(Usuario, aid).foto_perfil
    assert novo_nome and novo_nome != nome and not (banco.PASTA_DADOS / "perfis" / nome).exists()
    assert a.get(f"/equipe/foto/{novo_nome}").status_code == 200
    assert posta(a, "/app/foto", ta, remover_foto="1").status_code == 303
    with SessionLocal() as db:
        assert db.get(Usuario, aid).foto_perfil == ""

    # WhatsApp: ícone na escala; a página Equipe continua com o link escrito
    with SessionLocal() as db:
        db.get(Usuario, aid).celular = "98999990001"
        db.commit()
    tela = g.get("/painel/escala").text
    assert 'class="zap-icone"' in tela and "/static/whatsapp.svg" in tela
    assert ">Enviar pelo WhatsApp</a>" not in tela
    print("OK: fotos da equipe, ícone do WhatsApp e coluna Funcionário passaram")


if __name__ == "__main__":
    main()
