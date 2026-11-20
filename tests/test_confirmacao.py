"""Confirmação de turno, sugestão de quem escalar e trava da escala.
Rodar da raiz: .venv/Scripts/python tests/test_confirmacao.py"""
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

from app import confirmacao  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Disponibilidade, Oferta, SessionLocal, Turno, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
TER, QUA = SEG + timedelta(days=1), SEG + timedelta(days=2)
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


def entrar(token):
    c = novo()
    c.post(f"/convite/{token}", data={"csrf": csrf(c, f"/convite/{token}"), "senha": "senha123", "consentimento": "1"})
    return c, csrf(c, "/app")


def main():
    as_("08:00", SEG)
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Miami Bistro", "nome": "Gil",
                              "email": "gil@mb.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    g.post("/painel/config", data={"csrf": tg, "nome": "Miami Bistro", "lat": "25.77", "lng": "-80.19", "raio_m": "150",
                                   "tolerancia_min": "10", "fuso": "America/New_York", "bloquear_fora": "1",
                                   "ajuste_dias": "7", "fotos_dias": "45", "estado": "FL", "semana_inicio": "6",
                                   "frequencia": "weekly", "periodo_ancora": (SEG - timedelta(days=1)).isoformat(),
                                   "intervalo_padrao_min": "30", "tolerancia_dia_min": "7",
                                   "pct_noturno": "0", "pct_feriado": "0"})
    for nome, cel in (("Ana Souza", "3055550001"), ("Bia Reis", "3055550002"), ("Caio Lima", "3055550003"),
                      ("Dan Cruz", "3055550004")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        ids = {k: u.id for k, u in us.items()}
        tokens = {k: u.convite_token for k, u in us.items()}
        for k, c in (("Ana", 1500), ("Bia", 1800), ("Caio", 1500), ("Dan", 1500)):
            us[k].taxa_hora_centavos = c
        db.add(Disponibilidade(empresa_id=us["Caio"].empresa_id, usuario_id=ids["Caio"], dia_semana=QUA.weekday(), tipo="nao"))
        db.commit()

    r = g.post("/painel/config/escala-regras", data={"csrf": tg, "confirmar_turno": "1", "confirmar_antes_h": "999"})
    assert "Use de 1 a 168 horas" in g.get("/painel/config").text
    g.post("/painel/config/escala-regras", data={"csrf": tg, "confirmar_turno": "1", "confirmar_antes_h": "24",
                                                 "oferecer": "1", "travar": "3"})
    assert 'id="escala-regras"' in g.get("/painel/config").text

    turno(g, tg, ids["Ana"], TER, "09:00", "17:00")
    turno(g, tg, ids["Ana"], QUA, "09:00", "17:00")
    turno(g, tg, ids["Bia"], TER, "09:00", "17:00")
    for d in (SEG, SEG + timedelta(days=3), SEG + timedelta(days=4), SEG + timedelta(days=5)):   # Dan: 32 h
        turno(g, tg, ids["Dan"], d, "08:00", "16:00")
    ana, ta = entrar(tokens["Ana"])
    assert "Vou estar lá" not in ana.get("/app/escala").text          # ainda não publicada
    g.post("/painel/escala/publicar", data={"csrf": tg, "semana": SEG.isoformat()})
    t = ana.get("/app/escala").text
    assert "Confirme os seus turnos" in t and "Confirmar todos (2)" in t and t.count("Vou estar lá") == 2
    with SessionLocal() as db:
        tid_ter = db.scalar(select(Turno.id).where(Turno.usuario_id == ids["Ana"], Turno.data == TER))
    ana.post(f"/app/turnos/{tid_ter}/confirmar", data={"csrf": ta})
    t = ana.get("/app/escala").text
    assert "Confirmar todos (1)" in t and "✓ confirmado" in t
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert "✓ confirmado" in grade and "não confirmado" in grade

    # mudar o turno pede nova confirmação
    turno(g, tg, ids["Ana"], TER, "10:00", "18:00")
    with SessionLocal() as db:
        assert db.get(Turno, tid_ter).confirmado_em is None

    # lembrete e aviso 24 h antes; oferta na metade do prazo
    with SessionLocal() as db:
        as_("08:00", SEG)
        assert confirmacao.verificar(db) == ([], [])                 # terça 09:00/10:00 está a mais de 24 h
        as_("11:00", SEG)
        novos, lembretes = confirmacao.verificar(db)
        assert sorted(a.usuario_id for a in novos) == sorted([ids["Ana"], ids["Bia"]])
        assert {l[0] for l in lembretes} == {ids["Ana"], ids["Bia"]} and lembretes[0][1] == "Confirme o seu turno"
        assert confirmacao.verificar(db) == ([], [])                 # uma vez só
        as_("22:00", SEG)                                              # faltam 11–12 h: metade do prazo
        novos, lembretes = confirmacao.verificar(db)
        ofertas = list(db.scalars(select(Oferta).where(Oferta.obs == "Turno não confirmado")))
        assert sorted(o.de_id for o in ofertas) == sorted([ids["Ana"], ids["Bia"]])
        assert any(l[1] == "Turno disponível" for l in lembretes)
        confirmacao.verificar(db)
        assert len(list(db.scalars(select(Oferta).where(Oferta.obs == "Turno não confirmado")))) == 2
    ana.post("/app/escala/confirmar-todos", data={"csrf": ta})
    assert "Confirme os seus turnos" not in ana.get("/app/escala").text

    # quem pode cobrir quarta 09:00–17:00
    as_("08:00", SEG)
    r = g.get(f"/painel/escala/sugestoes?data={QUA}&inicio=09:00&fim=17:00").json()
    nomes = [p["nome"] for p in r["pessoas"]]
    assert "Ana Souza" not in nomes                                    # já trabalha na quarta
    assert nomes[0] == "Bia Reis" and r["pessoas"][0]["ok"] and r["pessoas"][0]["custo"] == 14400
    caio = next(p for p in r["pessoas"] if p["nome"] == "Caio Lima")
    dan = next(p for p in r["pessoas"] if p["nome"] == "Dan Cruz")
    assert "fora da disponibilidade" in caio["motivos"]
    assert dan["motivos"] == [] and nomes.index("Dan Cruz") > nomes.index("Bia Reis")   # 32 + 8 = 40 h: sem extra, mas mais horas
    turno(g, tg, ids["Dan"], SEG - timedelta(days=1), "08:00", "12:00")   # domingo 15/11, mesma semana de trabalho: 36 h
    r2 = g.get(f"/painel/escala/sugestoes?data={QUA}&inicio=09:00&fim=17:00").json()
    dan = next(p for p in r2["pessoas"] if p["nome"] == "Dan Cruz")
    assert dan["motivos"] == ["4h00 de hora extra"], dan
    assert g.get("/painel/escala/sugestoes?data=x").status_code == 400
    assert "Ver quem pode" in g.get(f"/painel/escala?semana={SEG}").text

    # trava: 4 dias atrás não muda mais
    antigo = SEG - timedelta(days=4)
    turno(g, tg, ids["Bia"], antigo, "09:00", "17:00")
    assert "a escala trava 3 dia(s)" in g.get(f"/painel/escala?semana={antigo}").text
    with SessionLocal() as db:
        assert db.scalar(select(Turno).where(Turno.usuario_id == ids["Bia"], Turno.data == antigo)) is None
    print("OK: confirmação de turno, sugestões e trava passou")


if __name__ == "__main__":
    main()
