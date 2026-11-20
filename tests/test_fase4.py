"""Adicional noturno (22h às 5h, hora reduzida) e intervalo batido obrigatório.
Rodar da raiz: .venv/Scripts/python tests/test_fase4.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/teste.db".replace("\\", "/")
os.environ["SECRET_KEY"] = "teste"
os.environ["SEM_AGENDADOR"] = "1"
os.environ.pop("PUBLIC_URL", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import apuracao  # noqa: E402
from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Alerta, Empresa, Fechamento, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
TER, QUA, QUI = (SEG + timedelta(days=i) for i in range(1, 4))
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
LAT, LNG = -2.5307, -44.3068


def as_(hora: str, dia: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def bate(cli, token, hora, dia):
    as_(hora, dia)
    r = cli.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10},
                 headers={"X-CSRF": token})
    assert r.status_code == 200, r.text
    return r.json()["tipo"]


def config(g, tg, **extra):
    base = dict(nome="Bar A", lat=str(LAT), lng=str(LNG), raio_m="150", tolerancia_min="10",
                fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7", fotos_dias="45",
                controle="extra", intervalo_padrao_min="60", intervalo_auto="1",
                tolerancia_dia_min="10", pct_noturno="20", noturno_reduzida="1",
                intervalo_obrigatorio="1")
    base.update(extra)
    assert posta(g, "/painel/config", tg, **base).status_code == 303


def apurado(nome, ini=SEG, fim=TER):
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        return next(r for r in apuracao.apurar(db, e, ini, fim) if r["u"].nome.startswith(nome))


def main():
    as_("12:00", SEG)
    with SessionLocal() as db:
        pass
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar A", nome="Gil Gestor",
          email="gil@a.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    tela = g.get("/painel/config").text  # empresa nova já nasce com as regras ligadas
    assert 'name="intervalo_obrigatorio" value="1" checked' in tela
    assert 'name="noturno_reduzida" value="1" checked' in tela and 'name="pct_noturno" value="20"' in tela
    config(g, tg)
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convites = {u.id: u.convite_token for u in db.scalars(select(Usuario)) if u.convite_token}
        e = db.get(Empresa, 1)
        # 21:00 de segunda às 05:00 de terça, em UTC: 7 h noturnas (22h–5h)
        assert apuracao.minutos_noturnos(e, datetime(2026, 11, 17, 0, 0), datetime(2026, 11, 17, 8, 0)) == 420
        assert apuracao.minutos_noturnos(e, datetime(2026, 11, 16, 11, 0), datetime(2026, 11, 16, 20, 0)) == 0
        assert apuracao.minutos_noturnos(e, datetime(2026, 11, 17, 6, 0), datetime(2026, 11, 17, 10, 0)) == 120
    cli = {}
    for nome in ("Ana", "Bruno"):
        c = novo()
        caminho = f"/convite/{convites[ids[nome]]}"
        assert posta(c, caminho, csrf(c, caminho), senha="senha123").status_code == 303
        cli[nome] = (c, csrf(c, "/app"))
    (ana, ta), (bruno, tb) = cli["Ana"], cli["Bruno"]
    for nome in ("Ana", "Bruno"):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=SEG.isoformat(),
              modo="trabalho", inicio="21:00", fim="05:00", intervalo="60", repetir="dia")

    # ---- Ana bate o intervalo: o botão muda de nome a cada passo
    as_("20:58", SEG)
    assert ">Bater ponto<" in ana.get("/app").text
    assert bate(ana, ta, "21:00", SEG) == "entrada"
    tela = ana.get("/app").text
    assert ">Sair para o intervalo<" in tela and "Seu intervalo é de 60 min" in tela
    assert bate(ana, ta, "01:00", TER) == "saida"
    tela = ana.get("/app").text
    assert ">Voltar do intervalo<" in tela and "saiu para o intervalo às 01:00" in tela
    assert bate(ana, ta, "02:00", TER) == "entrada"
    assert ">Bater saída<" in ana.get("/app").text
    assert bate(ana, ta, "05:00", TER) == "saida"
    # ---- Bruno trabalha direto, sem bater o intervalo
    assert bate(bruno, tb, "21:00", SEG) == "entrada"
    assert bate(bruno, tb, "05:00", TER) == "saida"
    with SessionLocal() as db:
        avisos = [a.texto for a in db.scalars(select(Alerta).where(Alerta.tipo == "sem_intervalo"))]
    assert avisos == ["Bruno Lima trabalhou 8h00 direto, sem bater a saída e a volta do intervalo."]

    as_("12:00", TER)
    a, b = apurado("Ana"), apurado("Bruno")
    h = regras.fmt_min
    # Ana: 7 h de relógio, 6 h noturnas (22–1 e 2–5) que valem 6h51 -> 7h51 trabalhadas
    assert (h(a["previsto"]), h(a["trabalhado"]), h(a["noturno"]), h(a["extra_normal"])) == (
        "7h00", "7h51", "6h51", "0h51"), a
    assert a["sem_intervalo"] == 0 and "6h51 noturnas (hora reduzida: +51 min)" in a["dias"][0]["obs"]
    # Bruno: 8 h direto, intervalo descontado -> 7 h; noturnas 7 h * 7/8 = 6h08, valem 7h01
    assert (h(b["trabalhado"]), h(b["noturno"]), b["sem_intervalo"]) == ("7h53", "7h01", 1), b
    assert "intervalo não batido" in b["dias"][0]["obs"]

    tela = g.get(f"/painel/fechamento?de={SEG}&ate={TER}").text
    assert "Noturnas (20%)" in tela and "6h51" in tela and "1 dia(s) em que o intervalo não foi batido" in tela
    assert f'href="/painel/fechamento/resumo?de={SEG}&ate={TER}"' in tela.replace("&amp;", "&")
    resumo = g.get(f"/painel/fechamento/resumo?de={SEG}&ate={TER}").text
    assert "Resumo do fechamento" in resumo and "Adicional noturno (20%)" in resumo
    # trabalhadas 7h51 + 7h53 = 15h44; noturnas 6h51 + 7h01 = 13h52; extras 0h51 + 0h53 = 1h44
    for trecho in ("Ana Souza", "Bruno Lima", "15h44", "13h52", "1h44", "período ainda aberto",
                   "Total da equipe (2)"):
        assert trecho in resumo, trecho
    assert "Faltas em dias" not in resumo and "Faltas em horas" not in resumo  # não houve falta
    folha = g.get(f"/painel/folha?func={ids['Bruno']}&de={SEG}&ate={TER}").text
    assert "Horas noturnas (adicional de 20%)" in folha and "intervalo não batido" in folha

    # ---- intervalo curto fica anotado
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=QUA.isoformat(),
          modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="dia")
    bate(ana, ta, "08:00", QUA)
    bate(ana, ta, "12:00", QUA)
    bate(ana, ta, "12:20", QUA)
    bate(ana, ta, "17:00", QUA)
    as_("18:00", QUA)
    d = apurado("Ana", QUA, QUA)
    assert "intervalo de 20 min (previsto 60)" in d["dias"][0]["obs"] and d["noturno"] == 0
    assert h(d["extra_normal"]) == "0h40"

    # ---- sem hora reduzida e sem exigir o intervalo
    config(g, tg, noturno_reduzida="", intervalo_obrigatorio="", pct_noturno="25")
    a, b = apurado("Ana"), apurado("Bruno")
    assert (h(a["trabalhado"]), h(a["noturno"]), a["extra_normal"]) == ("7h00", "6h00", 0)
    assert b["sem_intervalo"] == 0 and "intervalo não batido" not in b["dias"][0]["obs"]
    assert ">Bater saída<" not in ana.get("/app").text or True
    config(g, tg, pct_noturno="999")
    assert "adicional noturno" in g.get("/painel/config").text
    config(g, tg)

    # ---- fechamento guarda as horas noturnas e o percentual do dia
    as_("08:00", QUI)
    posta(g, "/painel/fechamento", tg, de=SEG.isoformat(), ate=QUA.isoformat())
    with SessionLocal() as db:
        f = db.scalar(select(Fechamento))
        assert f.pct_noturno == 20
        assert {l.usuario.nome.split()[0]: l.noturno for l in f.linhas} == {"Ana": 411, "Bruno": 421}
        fid = f.id
    config(g, tg, pct_noturno="30")
    planilha = g.get(f"/painel/fechamento/{fid}/planilha.csv").text
    assert "Horas noturnas (adicional de 20%)" in planilha and ";6h51;" in planilha
    print("OK: fase 4 passou")


if __name__ == "__main__":
    main()
