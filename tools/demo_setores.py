"""Setores na empresa de demonstração (para as capturas de tela): Cozinha, Salão e Caixa,
ligados pela função de cada pessoa."""
import sqlite3

c = sqlite3.connect("dados/visualizar.db")
eid = c.execute("select empresa_id from usuarios where email='demo@noponto.test'").fetchone()[0]
ids = {}
for nome in ("Cozinha", "Salão", "Caixa"):
    cur = c.execute("insert into setores (empresa_id, nome, ativo) values (?, ?, 1)", (eid, nome))
    ids[nome] = cur.lastrowid
regra = {"cozinh": "Cozinha", "garç": "Salão", "garc": "Salão", "caix": "Caixa"}
for uid, cargo in c.execute("select id, cargo from usuarios where empresa_id=? and papel='funcionario'", (eid,)).fetchall():
    alvo = next((s for k, s in regra.items() if k in (cargo or "").lower()), None)
    if alvo:
        c.execute("update usuarios set setor_id=? where id=?", (ids[alvo], uid))
c.commit()
print("setores da demonstração:", ids)
