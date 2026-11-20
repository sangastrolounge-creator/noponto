"""E-mail e "esqueci a senha": Master -> E-mail, e-mail de teste, link que vale 1 hora e uma vez,
resposta igual para e-mail desconhecido. Envio simulado. Rodar da raiz:
.venv/Scripts/python tests/test_email.py"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com", "PUBLIC_URL": "https://app.example.com"})
for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

from app import db as banco  # noqa: E402
from app import email_envio  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 18, 15, 0)}
banco.agora = lambda: relogio["agora"]
enviados = []
email_envio._mandar = lambda para, assunto, texto: enviados.append((para, assunto, texto))


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def flash(cli, caminho):
    m = re.search(r'class="aviso[^"]*">([^<]+)<', cli.get(caminho).text)
    return m.group(1) if m else ""


def entra(email, senha):
    c = novo()
    return c.post("/entrar", data={"csrf": csrf(c, "/entrar"), "login": email, "senha": senha})


def link_do(texto):
    return re.search(r"https://app\.example\.com(/redefinir/\S+)", texto).group(1)


def main():
    m = novo()
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Taco", "nome": "Gil Gomes",
                              "email": "gil@taco.com", "senha": "senhaantiga"})
    assert 'href="/esqueci"' in novo().get("/entrar").text

    # ---- sem e-mail configurado: a tela avisa e nada é enviado
    assert "ainda não foi ligado" in novo().get("/esqueci").text
    tm = csrf(m, "/master/email")
    assert "Desligado" in m.get("/master/email").text
    m.post("/master/email", data={"csrf": tm, "host": "smtp.hostinger.com", "porta": "465",
                                  "usuario": "x", "senha": "s"})
    assert "Confira o servidor" in flash(m, "/master/email")
    m.post("/master/email", data={"csrf": tm, "host": "smtp.hostinger.com", "porta": "465",
                                  "usuario": "no-reply@app.example.com", "senha": "senha-da-caixa"})
    tela = m.get("/master/email").text
    assert "Ligado" in tela and "senha-da-caixa" not in tela and email_envio.configurado()
    # salvar de novo sem senha mantém a senha
    m.post("/master/email", data={"csrf": tm, "host": "smtp.hostinger.com", "porta": "587",
                                  "usuario": "no-reply@app.example.com", "senha": ""})
    assert email_envio.dados()["smtp_senha"] == "senha-da-caixa" and email_envio.dados()["smtp_port"] == "587"
    m.post("/master/email", data={"csrf": tm, "acao": "testar"})
    assert enviados[-1][0] == "dono@plataforma.com" and "Teste de e-mail" in enviados[-1][1]
    assert "enviado para dono@plataforma.com" in flash(m, "/master/email")
    assert novo().get("/master/email").status_code == 303     # só o dono da plataforma

    # ---- e-mail desconhecido: mesma resposta, nada enviado
    c = novo()
    antes = len(enviados)
    r = c.post("/esqueci", data={"csrf": csrf(c, "/esqueci"), "email": "ninguem@x.com"})
    assert "Se este e-mail estiver cadastrado" in r.text and len(enviados) == antes

    # ---- pedido do Gil: link por e-mail, vale uma vez
    r = c.post("/esqueci", data={"csrf": csrf(c, "/esqueci"), "email": "GIL@taco.com"})
    assert "Se este e-mail estiver cadastrado" in r.text
    para, assunto, texto = enviados[-1]
    assert para == "gil@taco.com" and "Redefinir a sua senha" in assunto and "Gil" in texto
    caminho = link_do(texto)
    pagina = c.get(caminho)
    assert pagina.status_code == 200 and "gil@taco.com" in pagina.text
    r = c.post(caminho, data={"csrf": csrf(c, caminho), "senha": "nova12345", "senha2": "outra123"})
    assert r.status_code == 400 and "não são iguais" in r.text
    r = c.post(caminho, data={"csrf": csrf(c, caminho), "senha": "nova12345", "senha2": "nova12345"})
    assert r.status_code == 303 and r.headers["location"] == "/entrar"
    assert entra("gil@taco.com", "nova12345").headers["location"] == "/painel"
    assert entra("gil@taco.com", "senhaantiga").status_code == 400
    assert c.get(caminho).status_code == 404                  # já usado

    # ---- link vencido (mais de 1 hora)
    c = novo()
    c.post("/esqueci", data={"csrf": csrf(c, "/esqueci"), "email": "gil@taco.com"})
    caminho = link_do(enviados[-1][2])
    relogio["agora"] += timedelta(hours=1, minutes=1)
    assert c.get(caminho).status_code == 404
    assert "Link vencido" in c.get(caminho).text
    print("OK: e-mail e esqueci a senha passou")


if __name__ == "__main__":
    main()
