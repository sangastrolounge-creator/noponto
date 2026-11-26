"""Menu do Master à direita, por categoria, sem o menu da empresa. Rodar da raiz: python tests/test_menu_master.py"""
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
for k in ("ANTHROPIC_API_KEY", "ASAAS_API_KEY", "PUBLIC_URL"):
    os.environ.pop(k, None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main():
    m = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', m.get("/cadastro").text).group(1)
    m.post("/cadastro", data={"csrf": tok, "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    for caminho, ativo in (("/master", "Empresas"), ("/master/crm", "CRM (funil)"),
                           ("/master/plataforma", "Plataforma")):
        tela = m.get(caminho).text
        assert 'class="master-menu"' in tela, caminho
        for grupo in ("Vendas", "Clientes", "Financeiro", "Marketing e site", "Sistema"):
            assert f"<span>{grupo}</span>" in tela, (caminho, grupo)
        # o grupo da tela atual abre e o item fica marcado
        assert re.search(r'class="ativa" aria-current="page">' + re.escape(ativo), tela), caminho
        # o menu da empresa some; fica só o caminho de volta
        menu = tela[tela.index('class="abas"'):tela.index("</nav>", tela.index('class="abas"'))]
        assert "/painel/escala" not in menu and "/painel/fechamento" not in menu and 'href="/painel"' in menu
        assert 'class="subabas"' not in tela
    # fora do Master o menu da empresa continua igual
    tela = m.get("/painel").text
    menu = tela[tela.index('class="abas"'):tela.index("</nav>", tela.index('class="abas"'))]
    assert "/painel/escala" in menu and 'class="master-menu"' not in tela
    print("OK: menu do Master passou")


if __name__ == "__main__":
    main()
