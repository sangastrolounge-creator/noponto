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

from app import custos, faturamento, push  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Empresa, FaturamentoMes, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_usuarios = lambda lista: avisos.extend(lista)


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


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
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", estado="FL", semana_inicio="6", frequencia="weekly",
          periodo_ancora="2026-11-15", intervalo_padrao_min="30", tolerancia_dia_min="7",
          pct_noturno="0", pct_feriado="0")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bruno Lima", "3055550002"), ("Caio Reis", "3055550003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
    # Ana R$ 4.400 / 220 h = R$ 20,00 a hora; Bruno R$ 2.200 / 220 h = R$ 10,00; Caio sem salário
    posta(g, "/painel/pagamento/dados", tg, **{
        f"sal_{ids['Ana']}": "4.400,00", f"jor_{ids['Ana']}": "220", f"dep_{ids['Ana']}": "0", f"vt_{ids['Ana']}": "",
        f"sal_{ids['Bruno']}": "2.200,00", f"jor_{ids['Bruno']}": "220", f"dep_{ids['Bruno']}": "0", f"vt_{ids['Bruno']}": "",
        f"sal_{ids['Caio']}": "", f"jor_{ids['Caio']}": "220", f"dep_{ids['Caio']}": "0", f"vt_{ids['Caio']}": ""})

    # ---- escala: Ana seg e ter 8h-12h; custo previsto por pessoa e por dia
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=SEG.isoformat(), modo="trabalho",
          inicio="08:00", fim="12:00", intervalo="0", repetir="dia")
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=(SEG + timedelta(days=1)).isoformat(),
          modo="trabalho", inicio="08:00", fim="12:00", intervalo="0", repetir="dia")
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert "8h00 na semana · R$ 160,00" in grade, grade[:0]
    assert "Total do dia" in grade and "R$ 80,00" in grade
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

    # ---- custo real do dia: Ana 4 h a R$ 20, Bruno 8 h a R$ 10, Caio sem valor
    as_("20:00", SEG)
    for nome, ent, sai in (("Ana", "08:00", "12:00"), ("Bruno", "09:00", "17:00"), ("Caio", "10:00", "11:00")):
        for tipo, hora in (("entrada", ent), ("saida", sai)):
            posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo, data=SEG.isoformat(),
                  hora=hora, obs="t")
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        c = custos.custo_do_dia(db, e, SEG)
    assert c["centavos"] == 8000 + 8000 and c["minutos"] == 240 + 480 + 60 and c["sem_valor"] == ["Caio"], c
    # ---- Hoje: nada de custo por hora; faturamento e CMO só quando a empresa liga
    tela = g.get("/painel").text
    assert "Custo da equipe hoje" not in tela and "Vendas e custo da equipe" not in tela
    assert "Faturamento e CMO" not in tela and "Acompanhe o CMO pelo faturamento" in tela
    posta(g, "/painel/config/faturamento", tg, usa="1", meta="200", regime="simples")
    assert "entre 5 e 80" in g.get("/painel/config").text
    posta(g, "/painel/config/faturamento", tg, usa="1", meta="30", regime="simples")
    tela = g.get("/painel").text
    assert "Faturamento e CMO" in tela and "CMO do mês" in tela and "informe o faturamento" in tela
    assert "1 sem salário na ficha" in tela                      # Caio
    # custo do mês: salários (R$ 4.400 + R$ 2.200) + encargos do Simples (FGTS, 13º, férias com 1/3)
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        c = faturamento.custo_mensal(db, e, "2026-11", SEG)
    enc = faturamento.ENCARGOS["clt"]["simples"]
    assert c["salarios"] == 660000 and c["pessoas"] == 2 and c["sem_valor"] == ["Caio Reis"], c
    assert c["encargos"] == round(440000 * enc) + round(220000 * enc) and 0.28 < float(enc) < 0.30, c
    posta(g, "/painel/faturamento", tg, mes="2026-11", valor="30.000,00")
    tela = g.get("/painel").text
    pct = round(c["total"] * 100 / 3000000, 1)
    assert f"{pct}%".replace(".", ",") in tela and 'etiqueta ok' in tela, pct
    posta(g, "/painel/faturamento", tg, mes="2026-11", valor="20.000,00")        # CMO alto
    assert "etiqueta erro" in g.get("/painel").text
    posta(g, "/painel/faturamento", tg, mes="2026-12", valor="10")               # mês futuro
    assert "Informe o mês" in g.get("/painel").text
    posta(g, "/painel/faturamento", tg, mes="2026-11", valor="")                  # apagar
    assert "informe o faturamento" in g.get("/painel").text
    # Lucro Presumido: encargos maiores (INSS patronal, RAT e terceiros)
    posta(g, "/painel/config/faturamento", tg, usa="1", meta="30", regime="presumido")
    with SessionLocal() as db:
        c2 = faturamento.custo_mensal(db, db.get(Empresa, 1), "2026-11", SEG)
    assert c2["encargos"] > c["encargos"] * 2, (c2, c)

    # ---- gerente sem a permissão Pagamento não vê nem lança o faturamento
    posta(g, "/painel/gerentes", tg, nome="Lia", email="lia@c.com", p_escala="1")
    with SessionLocal() as db:
        token = db.scalar(select(Usuario).where(Usuario.email == "lia@c.com")).convite_token
    lc = novo()
    posta(lc, f"/convite/{token}", csrf(lc, f"/convite/{token}"), senha="senha123")
    assert "Faturamento e CMO" not in lc.get("/painel").text
    r = posta(lc, "/painel/faturamento", csrf(lc, "/painel"), mes="2026-11", valor="1,00")
    assert r.headers["location"] == "/painel"
    with SessionLocal() as db:
        assert not db.scalar(select(FaturamentoMes.id))
    print("OK: custo da equipe, faturamento/CMO e publicar escala passou")


if __name__ == "__main__":
    main()
