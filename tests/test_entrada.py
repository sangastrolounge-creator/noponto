"""Telas de entrada (cores e imagens do Master) e entrada com o Google.
Rodar da raiz: .venv/Scripts/python tests/test_entrada.py"""
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/teste.db".replace("\\", "/")
os.environ["SECRET_KEY"] = "teste"
os.environ["IDIOMA_PADRAO"] = "pt"
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import entrada  # noqa: E402
from app.db import Empresa, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def usuario(email):
    with SessionLocal() as db:
        return db.scalar(select(Usuario).where(Usuario.email == email))


def google(cli, perfil, modo="entrar"):
    """Faz a ida e a volta do Google com um perfil de mentira."""
    r = cli.get(f"/entrar/google?modo={modo}")
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["redirect_uri"][0].endswith("/entrar/google/volta") and q["scope"][0] == "openid email profile"
    entrada._perfil_google = lambda request, code: perfil
    return cli.get(f"/entrar/google/volta?code=abc&state={q['state'][0]}")


def main():
    # ---- sem as chaves do Google: telas novas, sem o botão
    c = novo()
    t = c.get("/entrar").text
    assert 'class="ent"' in t and "Bem-vindo de volta" in t and "Continuar com o Google" not in t
    assert "Ponto, escala e equipe num lugar só." in t and "ent-cena" in t
    assert "Criar conta com o Google" not in c.get("/cadastro").text
    assert c.get("/entrar/google").headers["location"] == "/entrar"

    # ---- dono da plataforma (primeira conta) personaliza a tela
    posta(c, "/cadastro", csrf(c, "/cadastro"), aceito="1", empresa="Plataforma", nome="Dono",
          email="dono@plat.com", senha="segredo1")
    tc = csrf(c, "/painel")
    assert "Tela de entrada" in c.get("/master/entrada").text
    posta(c, "/master/entrada", tc, cor1="#FF0066", cor2="#00aa88", titulo="  Seu time no ritmo  ", texto="Texto novo")
    posta(c, "/master/entrada", tc, cor1="vermelho", cor2="#00aa88")             # cor inválida: ignora
    r = c.post("/master/entrada/imagem", data={"csrf": tc}, files={"arquivo": ("a.png", PNG, "image/png")})
    assert r.status_code == 303
    r = c.post("/master/entrada/imagem", data={"csrf": tc}, files={"arquivo": ("a.txt", b"oi", "text/plain")})
    nomes = entrada.imagens()
    assert len(nomes) == 1
    sair = novo()
    t = sair.get("/entrar").text
    assert "--ent1:#ff0066" in t and "Seu time no ritmo" in t and f"/entrada-img/{nomes[0]}" in t and "ent-cena" not in t
    assert sair.get(f"/entrada-img/{nomes[0]}").content == PNG
    assert sair.get("/entrada-img/..%2Fteste.db").status_code == 404
    assert sair.get("/master/entrada").status_code == 303                       # só o Master
    c.post("/master/entrada/imagem", data={"csrf": tc}, files={"arquivo": ("b.png", PNG, "image/png")})
    segunda = entrada.imagens()[1]
    posta(c, f"/master/entrada/imagem/{segunda}", tc, acao="primeira")
    assert entrada.imagens()[0] == segunda
    posta(c, f"/master/entrada/imagem/{segunda}", tc, acao="remover")
    assert entrada.imagens() == nomes

    # ---- com as chaves do Google
    os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"] = "id-teste", "segredo-teste"
    g = novo()
    assert "Continuar com o Google" in g.get("/entrar").text and "Criar conta com o Google" in g.get("/cadastro").text

    # estado errado não entra
    g.get("/entrar/google")
    r = g.get("/entrar/google/volta?code=abc&state=outro")
    assert r.headers["location"] == "/entrar"
    # e-mail não confirmado não entra
    r = google(g, {"sub": "1", "email": "x@gmail.com", "email_verified": False})
    assert r.headers["location"] == "/entrar"

    # e-mail novo: cria a conta da empresa sem senha
    r = google(g, {"sub": "g-777", "email": "Ana@Gmail.com", "email_verified": True, "name": "Ana Souza"}, "cadastro")
    assert r.headers["location"] == "/cadastro?google=1"
    t = g.get("/cadastro?google=1").text
    assert "ana@gmail.com" in t and 'action="/cadastro/google"' in t and 'value="Ana Souza"' in t and 'name="senha"' not in t
    tg = csrf(g, "/cadastro?google=1")
    r = posta(g, "/cadastro/google", tg, empresa="Café da Ana", nome="Ana Souza")
    assert r.status_code == 400 and "aceite os Termos" in r.text
    r = posta(g, "/cadastro/google", tg, empresa="Café da Ana", nome="Ana Souza", aceito="1")
    assert r.headers["location"] == "/painel/comecar"
    ana = usuario("ana@gmail.com")
    assert ana.google_sub == "g-777" and ana.dono
    with SessionLocal() as db:
        assert db.get(Empresa, ana.empresa_id).nome == "Café da Ana"

    # volta depois: entra direto pelo Google
    g2 = novo()
    r = google(g2, {"sub": "g-777", "email": "ana@gmail.com", "email_verified": True})
    assert r.headers["location"] == "/painel"
    assert g2.get("/painel").status_code == 200

    # quem já tem conta com senha e entra pelo Google com o mesmo e-mail: liga a conta
    g3 = novo()
    r = google(g3, {"sub": "g-dono", "email": "dono@plat.com", "email_verified": True})
    assert r.headers["location"] == "/painel" and usuario("dono@plat.com").google_sub == "g-dono"

    # funcionário desativado não entra
    with SessionLocal() as db:
        u = db.scalar(select(Usuario).where(Usuario.email == "ana@gmail.com"))
        u.ativo = False
        db.commit()
    r = google(novo(), {"sub": "g-777", "email": "ana@gmail.com", "email_verified": True})
    assert r.headers["location"] == "/entrar"
    print("OK: telas de entrada e Google passaram")


if __name__ == "__main__":
    main()
