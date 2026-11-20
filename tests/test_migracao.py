"""Banco antigo (colunas faltando) sobe com o código novo: criar_tabelas() acrescenta o que falta.
Rodar da raiz: .venv/Scripts/python tests/test_migracao.py"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
arquivo = f"{tmp}/teste.db"
os.environ.update({"DATABASE_URL": f"sqlite:///{arquivo}".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})

from sqlalchemy import inspect, select  # noqa: E402

from app import db as banco  # noqa: E402
from app.db import Base, Empresa, SessionLocal, Usuario, engine  # noqa: E402


def main():
    banco.criar_tabelas()
    engine.dispose()
    # Simula um banco de uma versão anterior: tira toda coluna que o SQLite deixa tirar.
    con = sqlite3.connect(arquivo)
    tiradas = []
    for tabela in Base.metadata.sorted_tables:
        for col in tabela.columns:
            if col.primary_key or col.foreign_keys:
                continue
            try:
                con.execute(f"ALTER TABLE {tabela.name} DROP COLUMN {col.name}")
                tiradas.append((tabela.name, col.name))
            except sqlite3.OperationalError:
                pass
    con.commit()
    con.execute("INSERT INTO empresas (id) VALUES (7)")
    con.execute("INSERT INTO usuarios (id, empresa_id) VALUES (3, 7)")
    con.commit()
    con.close()
    assert ("usuarios", "gorjeta") in tiradas and len(tiradas) > 100, len(tiradas)

    banco.criar_tabelas()          # o app sobe de novo
    insp = inspect(engine)
    for tabela in Base.metadata.sorted_tables:
        existentes = {c["name"] for c in insp.get_columns(tabela.name)}
        faltam = {c.name for c in tabela.columns} - existentes
        assert not faltam, (tabela.name, faltam)
    with SessionLocal() as db:
        u = db.get(Usuario, 3)
        assert u.gorjeta is False and u.empresa.id == 7
        assert db.scalar(select(Empresa.intervalo_minimo_min).where(Empresa.id == 7)) == 60
    banco.criar_tabelas()          # rodar de novo não muda nada
    print(f"OK: migração de banco antigo passou ({len(tiradas)} colunas recriadas)")


if __name__ == "__main__":
    main()
