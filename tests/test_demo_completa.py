"""A demonstração do visualizar.bat continua funcionando (é criada pelas telas do sistema).
Rodar da raiz: .venv/Scripts/python tests/test_demo_completa.py"""
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import Batida, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from tools import demo_completa  # noqa: E402


def main():
    g = TestClient(app, follow_redirects=False)
    ids = demo_completa.criar(g, hoje=date(2026, 11, 19))
    assert len(ids) == 4
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Batida.id))) > 40
    for tela in ("/painel", "/painel/escala", "/painel/ferias", "/painel/fechamento", "/painel/equipe"):
        assert g.get(tela).status_code == 200, tela
    assert "Ana Souza" in g.get("/painel/ferias").text
    print("OK: demonstração completa passou")


if __name__ == "__main__":
    main()
