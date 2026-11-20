"""Trabalho de menores: limites na escala, aviso ao salvar e aviso ao vivo.
Rodar da raiz: .venv/Scripts/python tests/test_menores.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import menores  # noqa: E402
from app.db import Alerta, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora, d):   # Flórida em novembro: UTC-5
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=5)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def turno(g, tg, uid, d, ini, fim):
    return g.post("/painel/escala/turno", data={"csrf": tg, "usuario_id": uid, "data": d.isoformat(), "modo": "trabalho",
                                                "inicio": ini, "fim": fim, "intervalo": "0", "repetir": "dia", "fixar": ""})


def puras():
    u = N(nascimento=date(2011, 6, 1))
    assert menores.idade(u, date(2026, 5, 31)) == 14 and menores.idade(u, date(2026, 6, 1)) == 15
    assert menores.verao(date(2026, 7, 4)) and menores.verao(date(2026, 9, 7)) and not menores.verao(date(2026, 9, 8))
    assert menores.dia_de_aula(SEG, set()) and not menores.dia_de_aula(SEG, {SEG})
    assert not menores.dia_de_aula(SEG + timedelta(days=5), set())    # sábado
    fl, ca, tx = N(estado="FL"), N(estado="CA"), N(estado="TX")
    assert menores.regra(fl, u, SEG)[0]["semana_aula"] == 900
    assert menores.regra(tx, u, SEG)[0]["semana_aula"] == 1080        # federal
    v = N(nascimento=date(2009, 3, 1))
    assert menores.regra(ca, v, SEG)[0]["fim_aula"] == 1320 and menores.regra(tx, v, SEG) == (None, "16")
    assert menores.regra(tx, N(nascimento=date(2000, 1, 1)), SEG) == (None, "")
    assert menores.regra(tx, N(nascimento=None), SEG) == (None, "")
    t = N(folga=False, inicio="15:00", fim="19:30", intervalo_min=0, data=SEG)
    assert menores.problemas_do_turno(menores.FED_14, t, set()) == ["mais de 3h00 no dia de aula", "termina depois das 19:00"]
    t = N(folga=False, inicio="15:00", fim="19:30", intervalo_min=0, data=date(2026, 7, 6))
    assert menores.problemas_do_turno(menores.FED_14, t, set()) == []   # verão: até 21h e 8 h


def main():
    puras()
    as_("08:00", SEG - timedelta(days=1))
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Miami Pizza", "nome": "Gil",
                              "email": "gil@mp.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    g.post("/painel/config", data={"csrf": tg, "nome": "Miami Pizza", "lat": "25.77", "lng": "-80.19", "raio_m": "150",
                                   "tolerancia_min": "10", "fuso": "America/New_York", "bloquear_fora": "1",
                                   "ajuste_dias": "7", "fotos_dias": "45", "estado": "FL", "semana_inicio": "6",
                                   "frequencia": "weekly", "periodo_ancora": (SEG - timedelta(days=1)).isoformat(),
                                   "intervalo_padrao_min": "30", "tolerancia_dia_min": "7",
                                   "pct_noturno": "0", "pct_feriado": "0"})
    for nome, cel in (("Leo Lima", "3055550001"), ("Mia Melo", "3055550002"), ("Ted Tavares", "3055550003"),
                      ("Kai Klein", "3055550004")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        us["Leo"].nascimento, us["Mia"].nascimento, us["Kai"].nascimento = date(2011, 6, 1), date(2009, 3, 1), date(2013, 5, 5)
        ids = {k: u.id for k, u in us.items()}
        db.commit()
    assert 'name="nascimento"' in g.get(f"/painel/equipe/{ids['Leo']}/ficha").text

    r = turno(g, tg, ids["Leo"], SEG, "15:00", "19:30")
    t = g.get(f"/painel/escala?semana={SEG}").text
    assert "Leo Lima é menor de idade e este turno passa do limite da lei: 11/16: mais de 3h00 no dia de aula; 11/16: termina depois das 19:00." in t
    for i in range(1, 5):
        turno(g, tg, ids["Leo"], SEG + timedelta(days=i), "15:00", "18:00")
    turno(g, tg, ids["Leo"], SEG + timedelta(days=5), "08:00", "16:00")
    turno(g, tg, ids["Mia"], SEG + timedelta(days=1), "16:00", "23:30")
    turno(g, tg, ids["Ted"], SEG + timedelta(days=1), "16:00", "23:30")
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert grade.count("Limite de menor") == 2, grade.count("Limite de menor")   # Leo seg e Mia ter
    assert "24h30 na semana: o limite é 15h00 em semana com aula" in grade
    assert "menor de 14 anos" in grade
    assert grade.count('>Menor</span>') == 3                                    # Leo, Mia, Kai

    # ---- ao vivo, segunda (dia de aula): Leo passa de 3 h no dia
    as_("15:00", SEG)
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Leo"], "tipo": "entrada",
                                         "data": SEG.isoformat(), "hora": "15:00", "obs": "t"})
    with SessionLocal() as db:
        as_("17:50", SEG)
        assert menores.verificar(db) == []
        as_("18:10", SEG)
        novos = menores.verificar(db)
        assert len(novos) == 1 and "já trabalhou 3h10 hoje (limite 3h00)" in novos[0].texto
        assert "(15 anos)" in novos[0].texto
        as_("19:40", SEG)
        assert menores.verificar(db) == []          # um aviso por dia
    # ---- sábado (sem aula, até 8 h): trabalhando depois das 19:00
    sab = SEG + timedelta(days=5)
    as_("14:00", sab)
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Mia"], "tipo": "entrada",
                                         "data": sab.isoformat(), "hora": "14:00", "obs": "t"})
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Leo"], "tipo": "saida",
                                         "data": SEG.isoformat(), "hora": "18:15", "obs": "t"})
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Leo"], "tipo": "entrada",
                                         "data": sab.isoformat(), "hora": "14:00", "obs": "t"})
    with SessionLocal() as db:
        as_("19:10", sab)
        novos = menores.verificar(db)
        assert [a.usuario_id for a in novos] == [ids["Leo"]], [a.texto for a in novos]   # Mia (17, FL) pode até mais tarde
        assert "depois das 19:00" in novos[0].texto
    print("OK: trabalho de menores passou")


if __name__ == "__main__":
    main()
