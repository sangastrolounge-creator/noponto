"""Verificação de domínio do Meta pela metatag. Rodar da raiz: python tests/test_meta_dominio.py"""
import os
import re
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

TAG = '<meta name="facebook-domain-verification" content="ywktbj3d8kp8t2zbcuju6rgepotbwf" />'


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def salvar(m, **campos):
    dados = {"csrf": csrf(m, "/master/rastreamento"), "rast_meta_pixel": "", "rast_ga4": "", "rast_google_ads": "",
             "rast_google_ads_rotulo": "", "rast_tiktok_pixel": "", "rast_meta_dominio": ""}
    dados.update(campos)
    m.post("/master/rastreamento", data=dados)


def cabeca(cli, caminho="/"):
    t = cli.get(caminho).text
    return t[:t.index("</head>")] if "</head>" in t else t


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    v = TestClient(app, follow_redirects=False)
    assert "facebook-domain-verification" not in cabeca(v)

    # só a verificação (sem pixel): a tag aparece no <head> da página inicial e nenhum código de anúncio carrega
    salvar(m, rast_meta_dominio=TAG)
    assert 'value="ywktbj3d8kp8t2zbcuju6rgepotbwf"' in m.get("/master/rastreamento").text
    h = cabeca(v)
    assert '<meta name="facebook-domain-verification" content="ywktbj3d8kp8t2zbcuju6rgepotbwf">' in h
    assert "fbevents.js" not in v.get("/").text and "aviso-cookies" not in v.get("/").text

    # com o pixel também: a tag continua no <head>
    salvar(m, rast_meta_dominio="ywktbj3d8kp8t2zbcuju6rgepotbwf", rast_meta_pixel="3207352586140549")
    assert 'content="ywktbj3d8kp8t2zbcuju6rgepotbwf"' in cabeca(v)

    # formato inválido é recusado (fica o que estava); em branco tira a tag
    salvar(m, rast_meta_dominio="<script>", rast_meta_pixel="3207352586140549")
    assert "<script>\"" not in cabeca(v) and 'content="ywktbj3d8kp8t2zbcuju6rgepotbwf"' in cabeca(v)
    salvar(m, rast_meta_pixel="3207352586140549")
    assert "facebook-domain-verification" not in cabeca(v)
    print("ok: verificação de domínio do Meta")


if __name__ == "__main__":
    main()
