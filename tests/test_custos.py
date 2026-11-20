"""Custo da equipe x vendas do dia, custo previsto na escala e "Publicar e avisar".
Rodar da raiz: .venv/Scripts/python tests/test_custos.py"""
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

from app import custos, push  # noqa: E402
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
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=5)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Cafe", nome="Gil", email="gil@c.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Cafe", lat="25.77", lng="-80.19", raio_m="150",
          tolerancia_min="10", fuso="America/New_York", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", estado="FL", semana_inicio="6", frequencia="weekly",
          periodo_ancora="2026-11-15", intervalo_padrao_min="30", tolerancia_dia_min="7",
          pct_noturno="0", pct_feriado="0")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bruno Lima", "3055550002"), ("Caio Reis", "3055550003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
    posta(g, "/painel/pagamento/dados", tg, **{
        f"taxa_{ids['Ana']}": "20.00", f"sal_{ids['Ana']}": "",
        f"taxa_{ids['Bruno']}": "0", f"isento_{ids['Bruno']}": "1", f"sal_{ids['Bruno']}": "700.00",
        f"taxa_{ids['Caio']}": "", f"sal_{ids['Caio']}": ""})

    # ---- escala: Ana seg e ter 8h-12h; custo previsto por pessoa e por dia
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=SEG.isoformat(), modo="trabalho",
          inicio="08:00", fim="12:00", intervalo="0", repetir="dia")
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=(SEG + timedelta(days=1)).isoformat(),
          modo="trabalho", inicio="08:00", fim="12:00", intervalo="0", repetir="dia")
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert "8h00 na semana · $160.00" in grade, grade[:0]
    assert "Total do dia" in grade and "$80.00" in grade
    assert "Publicar e avisar (1)" in grade

    # ---- publicar: avisa só quem tem escala; publicar de novo sem mudança não avisa ninguém
    posta(g, "/painel/escala/publicar", tg, semana=SEG.isoformat())
    assert [a[0] for a in avisos] == [ids["Ana"]] and avisos[0][1] == "Escala publicada"
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert "Publicar e avisar (" not in grade and "Publicada por Gil" in grade
    posta(g, "/painel/escala/publicar", tg, semana=SEG.isoformat())
    assert len(avisos) == 1
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=SEG.isoformat(), modo="trabalho",
          inicio="09:00", fim="12:00", intervalo="0", repetir="dia")
    assert "Publicar e avisar (1)" in g.get(f"/painel/escala?semana={SEG}").text
    posta(g, "/painel/escala/publicar", tg, semana=SEG.isoformat())
    assert avisos[-1][0] == ids["Ana"] and avisos[-1][1] == "Escala atualizada"

    # ---- custo real do dia: Ana 4 h a $20, Bruno exempt ($700/semana = $100/dia), Caio sem valor
    as_("20:00", SEG)
    for nome, ent, sai in (("Ana", "08:00", "12:00"), ("Bruno", "09:00", "17:00"), ("Caio", "10:00", "11:00")):
        for tipo, hora in (("entrada", ent), ("saida", sai)):
            posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo, data=SEG.isoformat(),
                  hora=hora, obs="t")
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        c = custos.custo_do_dia(db, e, SEG)
    assert c["centavos"] == 8000 + 10000 and c["minutos"] == 240 + 480 + 60 and c["sem_valor"] == ["Caio"], c
    tela = g.get("/painel").text
    assert "Custo da equipe hoje" in tela and "$180.00" in tela and "informe as vendas" in tela
    assert "1 sem valor da hora" in tela

    # ---- vendas do dia: $720 -> 25%
    posta(g, "/painel/vendas", tg, data=SEG.isoformat(), valor="720.00")
    tela = g.get("/painel").text
    assert "25.0%" in tela and "vendas: $720.00" in tela
    posta(g, "/painel/vendas", tg, data=(SEG + timedelta(days=3)).isoformat(), valor="10")   # futuro
    assert "Informe o dia" in g.get("/painel").text
    posta(g, "/painel/vendas", tg, data=SEG.isoformat(), valor="")     # apagar
    assert "informe as vendas" in g.get("/painel").text

    # ---- gerente sem a permissão Pagamento não vê custo nem vendas
    posta(g, "/painel/gerentes", tg, nome="Lia", email="lia@c.com", p_escala="1")
    with SessionLocal() as db:
        token = db.scalar(select(Usuario).where(Usuario.email == "lia@c.com")).convite_token
    lc = novo()
    posta(lc, f"/convite/{token}", csrf(lc, f"/convite/{token}"), senha="senha123")
    assert "Custo da equipe hoje" not in lc.get("/painel").text
    assert "$160.00" not in lc.get(f"/painel/escala?semana={SEG}").text
    r = posta(lc, "/painel/vendas", csrf(lc, "/painel"), data=SEG.isoformat(), valor="1.00")
    assert r.headers["location"] == "/painel"
    print("OK: custo da equipe, vendas e publicar escala passou")


if __name__ == "__main__":
    main()
