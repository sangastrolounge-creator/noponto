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


def as_(hora, d):   # Brasília: UTC-3
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def turno(g, tg, uid, d, ini, fim):
    return g.post("/painel/escala/turno", data={"csrf": tg, "usuario_id": uid, "data": d.isoformat(), "modo": "trabalho",
                                                "inicio": ini, "fim": fim, "intervalo": "0", "repetir": "dia", "fixar": ""})


def puras():
    u = N(nascimento=date(2011, 6, 1), classificacao="clt")
    assert menores.idade(u, date(2026, 5, 31)) == 14 and menores.idade(u, date(2026, 6, 1)) == 15
    assert menores.regra(None, u, SEG) == ({"dia": 480, "semana": 2640}, "14")
    ap = N(nascimento=date(2011, 6, 1), classificacao="aprendiz")
    assert menores.regra(None, ap, SEG)[0]["dia"] == 360                    # aprendiz: até 6 h (art. 432)
    assert menores.regra(None, N(nascimento=date(2009, 3, 1), classificacao="clt"), SEG)[1] == "16"
    assert menores.regra(None, N(nascimento=date(2013, 5, 5), classificacao="clt"), SEG) == (None, "<14")
    assert menores.regra(None, N(nascimento=date(2000, 1, 1), classificacao="clt"), SEG) == (None, "")
    assert menores.regra(None, N(nascimento=None, classificacao="clt"), SEG) == (None, "")
    r = {"dia": 480, "semana": 2640}
    t = N(folga=False, inicio="16:00", fim="23:30", intervalo_min=0, data=SEG)
    assert menores.problemas_do_turno(r, t) == ["trabalho entre 22h e 5h (proibido para menores)"]
    t = N(folga=False, inicio="08:00", fim="17:30", intervalo_min=60, data=SEG)
    assert menores.problemas_do_turno(r, t) == ["mais de 8h00 no dia"]
    t = N(folga=False, inicio="08:00", fim="17:00", intervalo_min=60, data=SEG)
    assert menores.problemas_do_turno(r, t) == []
    t = N(folga=False, inicio="04:00", fim="10:00", intervalo_min=0, data=SEG)
    assert menores.problemas_do_turno(r, t) == ["trabalho entre 22h e 5h (proibido para menores)"]


def main():
    puras()
    as_("08:00", SEG - timedelta(days=1))
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Pizzaria", "nome": "Gil",
                              "email": "gil@mp.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    g.post("/painel/config", data={"csrf": tg, "nome": "Pizzaria", "lat": "-2.53", "lng": "-44.30", "raio_m": "150",
                                   "tolerancia_min": "10", "fuso": "America/Sao_Paulo", "bloquear_fora": "1",
                                   "ajuste_dias": "7", "fotos_dias": "45", "controle": "banco",
                                   "intervalo_padrao_min": "60", "intervalo_auto": "1", "tolerancia_dia_min": "10"})
    for nome, cel in (("Leo Lima", "98999990001"), ("Mia Melo", "98999990002"), ("Ted Tavares", "98999990003"),
                      ("Kai Klein", "98999990004"), ("Ivo Ilha", "98999990005")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        us["Leo"].nascimento, us["Leo"].classificacao = date(2011, 6, 1), "aprendiz"   # 15 anos, aprendiz
        us["Mia"].nascimento = date(2009, 3, 1)                                         # 17 anos
        us["Kai"].nascimento = date(2013, 5, 5)                                         # 13 anos
        us["Ivo"].nascimento = date(2011, 2, 1)                                         # 15 anos, sem ser aprendiz
        ids = {k: u.id for k, u in us.items()}
        db.commit()
    assert 'name="nascimento"' in g.get(f"/painel/equipe/{ids['Leo']}/ficha").text

    # ---- ao salvar: aprendiz com 7 h no dia
    r = turno(g, tg, ids["Leo"], SEG, "08:00", "15:00")
    t = g.get(f"/painel/escala?semana={SEG}").text
    assert "Leo Lima é menor de idade e este turno passa do limite da lei: 16/11: mais de 6h00 no dia." in t, t[:0]
    for i in range(1, 5):
        turno(g, tg, ids["Leo"], SEG + timedelta(days=i), "08:00", "14:00")
    turno(g, tg, ids["Mia"], SEG + timedelta(days=1), "16:00", "23:30")
    turno(g, tg, ids["Ted"], SEG + timedelta(days=1), "16:00", "23:30")
    turno(g, tg, ids["Ivo"], SEG + timedelta(days=2), "08:00", "12:00")
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert grade.count("Limite de menor") == 2, grade.count("Limite de menor")   # Leo seg e Mia ter
    assert "menor de 14 anos: não pode trabalhar" in grade
    assert "14 ou 15 anos: só pode trabalhar como aprendiz" in grade             # Ivo
    assert grade.count('>Menor</span>') == 4                                     # Leo, Mia, Kai, Ivo

    # ---- ao vivo: Mia (17) ainda trabalhando depois das 22h
    ter = SEG + timedelta(days=1)
    as_("16:00", ter)
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Mia"], "tipo": "entrada",
                                         "data": ter.isoformat(), "hora": "16:00", "obs": "t"})
    with SessionLocal() as db:
        as_("21:50", ter)
        assert menores.verificar(db) == []
        as_("22:05", ter)
        novos = menores.verificar(db)
        assert len(novos) == 1 and "(17 anos)" in novos[0].texto and "depois das 22h" in novos[0].texto
        as_("22:40", ter)
        assert menores.verificar(db) == []          # um aviso por dia
    # ---- aprendiz passa de 6 h no dia
    qua = SEG + timedelta(days=2)
    as_("08:00", qua)
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Mia"], "tipo": "saida",
                                         "data": ter.isoformat(), "hora": "22:10", "obs": "t"})
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ids["Leo"], "tipo": "entrada",
                                         "data": qua.isoformat(), "hora": "08:00", "obs": "t"})
    with SessionLocal() as db:
        as_("13:50", qua)
        assert menores.verificar(db) == []
        as_("14:10", qua)
        novos = menores.verificar(db)
        assert [a.usuario_id for a in novos] == [ids["Leo"]], [a.texto for a in novos]
        assert "já trabalhou 6h10 hoje (limite 6h00)" in novos[0].texto and "(15 anos)" in novos[0].texto
    print("OK: trabalho de menores passou")


if __name__ == "__main__":
    main()
