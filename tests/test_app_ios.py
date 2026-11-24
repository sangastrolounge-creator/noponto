"""Modo app do iPhone: sem login do Google, cadastro e assinatura (App Store 3.1.1 e 4.8).
Rodar da raiz: python tests/test_app_ios.py"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

UA_IOS = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 DataWorkeIOS"}


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    web = TestClient(app, follow_redirects=False)
    ios = TestClient(app, follow_redirects=False, headers=UA_IOS)
    # navegador continua igual
    assert web.get("/").status_code == 200
    assert web.get("/cadastro").status_code == 200
    # app: abre na entrada, sem cadastro nem Google
    assert ios.get("/").headers["location"] == "/entrar"
    t = ios.get("/entrar").text
    assert "Criar conta grátis" not in t and "/entrar/google" not in t.split("<style>")[0]
    assert "/static/app_ios.js" in t
    for p in ("/cadastro", "/painel/assinatura", "/entrar/google", "/p/abc", "/i/abc"):
        r = ios.get(p)
        assert r.status_code == 200 and "Disponível pelo site" in r.text, p
    # gestor no app: menu sem assinatura e sem contagem do teste
    web.post("/cadastro", data={"csrf": csrf(web, "/cadastro"), "aceito": "1", "empresa": "Bar Mar",
                                "nome": "Gil", "email": "gil@bar.com", "senha": "segredo1"})
    ios.cookies = web.cookies
    painel = ios.get("/painel").text
    assert "Assinatura e plano" not in painel and "contagem-teste" not in painel
    assert "Assinatura e plano" in web.get("/painel/config").text
    print("ok: modo app iOS")


if __name__ == "__main__":
    main()
