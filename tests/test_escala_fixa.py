"""Escala fixa: repete sozinha toda semana, com exceções por data.
Rodar da raiz: .venv/Scripts/python tests/test_escala_fixa.py"""
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
for k in ("PUBLIC_URL", "ADMIN_EMAIL", "ASAAS_API_KEY"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import EscalaFixa, SessionLocal, Turno, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
LAT, LNG = -2.5307, -44.3068


def dia(n):
    return SEG + timedelta(days=n)


def as_(hora: str, d: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def turnos(uid, ini, fim):
    with SessionLocal() as db:
        return {t.data: ("folga" if t.folga else f"{t.inicio}-{t.fim}", t.fixa, t.intervalo_min)
                for t in db.scalars(select(Turno).where(Turno.usuario_id == uid,
                                                        Turno.data.between(ini, fim)))}


def main():
    as_("07:00", dia(2))  # quarta-feira
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar A", nome="Gil", email="gil@a.com",
          senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Bar A", lat=str(LAT), lng=str(LNG), raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convite = db.scalar(select(Usuario.convite_token).where(Usuario.id == ids["Ana"]))
    ana = novo()
    posta(ana, f"/convite/{convite}", csrf(ana, f"/convite/{convite}"), senha="senha123")
    ta = csrf(ana, "/app")
    assert 'name="fixar" value="1" id="d-fixar" checked' in g.get("/painel/escala").text

    # ---- segunda a sexta, toda semana: vale nesta semana e nas seguintes, sem copiar
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia(2).isoformat(),
          modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="uteis", fixar="1")
    with SessionLocal() as db:
        assert len(list(db.scalars(select(EscalaFixa)))) == 5
    t = turnos(ids["Ana"], dia(0), dia(20))
    assert t[dia(0)] == ("08:00-17:00", True, 60) and dia(5) not in t      # sábado sem escala
    assert t[dia(7)] == ("08:00-17:00", True, 60) and t[dia(16)][1], t      # próximas duas semanas
    # semana bem à frente: é criada ao abrir a tela
    longe = dia(70)
    assert not turnos(ids["Ana"], longe, longe + timedelta(days=6))
    grade = g.get(f"/painel/escala?semana={longe}").text
    assert grade.count("08:00<br>17:00") == 5 and "escala fixa se repete" in grade
    assert len(turnos(ids["Ana"], longe, longe + timedelta(days=6))) == 5
    # semana passada: nada é criado para trás
    g.get(f"/painel/escala?semana={dia(-7)}")
    assert not turnos(ids["Ana"], dia(-7), dia(-1))
    assert "08:00 às 17:00" in ana.get("/app/escala").text

    # ---- exceção: só nesta quinta ela entra mais tarde; a quinta seguinte continua igual
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia(3).isoformat(),
          modo="trabalho", inicio="10:00", fim="19:00", intervalo="60", repetir="dia")
    t = turnos(ids["Ana"], dia(0), dia(14))
    assert t[dia(3)] == ("10:00-19:00", False, 60) and t[dia(10)] == ("08:00-17:00", True, 60)
    assert "só nesta semana" in g.get(f"/painel/escala?semana={SEG}").text
    # limpar a exceção devolve o dia ao horário fixo
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia(3).isoformat(),
          modo="limpar", repetir="dia")
    assert turnos(ids["Ana"], dia(3), dia(3))[dia(3)] == ("08:00-17:00", True, 60)

    # ---- mudar a escala fixa: sexta vira folga daqui em diante, sem mexer no que já passou
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia(4).isoformat(),
          modo="folga", repetir="dia", fixar="1")
    t = turnos(ids["Ana"], dia(0), dia(14))
    assert t[dia(4)][0] == "folga" and t[dia(11)][0] == "folga" and t[dia(0)][0] == "08:00-17:00"
    # dia com batida não é apagado quando a escala fixa muda
    as_("08:00", dia(2))
    r = ana.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10}, headers={"X-CSRF": ta})
    assert r.status_code == 200, r.text
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia(1).isoformat(),
          modo="trabalho", inicio="09:00", fim="18:00", intervalo="60", repetir="uteis", fixar="1")
    t = turnos(ids["Ana"], dia(0), dia(14))
    assert t[dia(2)][0] == "09:00-18:00" and t[dia(9)][0] == "09:00-18:00"
    assert t[dia(11)][0] == "09:00-18:00", "a regra nova vale para todos os dias escolhidos"

    # ---- tirar um dia da escala fixa
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia(4).isoformat(),
          modo="limpar", repetir="dia", fixar="1")
    t = turnos(ids["Ana"], dia(0), dia(14))
    assert dia(4) not in t and dia(11) not in t and dia(10) in t
    with SessionLocal() as db:
        assert len(list(db.scalars(select(EscalaFixa)))) == 4

    # ---- "Repetir esta semana sempre": a semana montada vira a escala fixa de todos
    r = posta(g, "/painel/escala/fixar", tg, semana=dia(77).isoformat())
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Bruno"], data=dia(5).isoformat(),
          modo="trabalho", inicio="18:00", fim="02:00", intervalo="30", repetir="dia")
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Bruno"], data=dia(6).isoformat(),
          modo="folga", repetir="dia")
    assert dia(12) not in turnos(ids["Bruno"], dia(7), dia(13))
    posta(g, "/painel/escala/fixar", tg, semana=SEG.isoformat())
    assert "se repete sozinha" in g.get(f"/painel/escala?semana={SEG}").text
    t = turnos(ids["Bruno"], dia(0), dia(14))
    assert t[dia(12)] == ("18:00-02:00", True, 30) and t[dia(13)][0] == "folga"
    assert "escala fixa alterada" in g.get("/painel/historico").text

    # ---- o agendador mantém os próximos dias criados; funcionário desativado sai
    as_("07:00", dia(30))
    with SessionLocal() as db:
        regras.verificar_turnos(db)
    assert dia(38) in turnos(ids["Ana"], dia(30), dia(44))
    posta(g, f"/painel/equipe/{ids['Bruno']}", tg, acao="desativar")
    with SessionLocal() as db:
        regras.verificar_turnos(db)
        assert regras.gerar_da_fixa(db, db.get(Usuario, ids["Ana"]).empresa, dia(60), dia(66)) == 4
    assert not turnos(ids["Bruno"], dia(60), dia(66))

    # ---- hora de saída para o intervalo: validação, tela do gestor, app e lembrete
    as_("07:00", dia(35))                     # segunda-feira
    def salva(**extra):
        base = dict(usuario_id=ids["Ana"], data=dia(35).isoformat(), modo="trabalho", inicio="08:00",
                    fim="17:00", intervalo="60", repetir="uteis", fixar="1")
        base.update(extra)
        posta(g, "/painel/escala/turno", tg, **base)
        return g.get(f"/painel/escala?semana={dia(35)}").text
    assert "não cabe dentro do turno" in salva(intervalo_inicio="16:30")
    assert "não cabe dentro do turno" in salva(intervalo_inicio="07:00")
    assert "informe também a duração" in salva(intervalo_inicio="12:00", intervalo="0")
    grade = salva(intervalo_inicio="12:00")
    assert 'data-saidaint="12:00"' in grade and "intervalo 12:00" in grade and 'id="d-saidaint"' in grade
    assert "intervalo%20%C3%A0s%2012%3A00" in grade        # vai na mensagem do WhatsApp
    with SessionLocal() as db:
        assert {r.intervalo_inicio for r in db.scalars(select(EscalaFixa).where(
            EscalaFixa.usuario_id == ids["Ana"]))} == {"12:00"}
        t = db.scalar(select(Turno).where(Turno.usuario_id == ids["Ana"], Turno.data == dia(42)))
        assert t.intervalo_inicio == "12:00"              # as semanas seguintes também
    tela = ana.get("/app").text
    assert "Intervalo às <strong>12:00</strong> (60 min)" in tela
    assert "intervalo às 12:00" in ana.get("/app/escala").text
    as_("08:00", dia(35))
    assert ana.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10},
                    headers={"X-CSRF": ta}).status_code == 200
    as_("11:50", dia(35))
    with SessionLocal() as db:
        assert not [l for l in regras.verificar_turnos(db)[1] if l[1] == "Hora do intervalo"]
    as_("12:01", dia(35))
    with SessionLocal() as db:
        avisos = [l for l in regras.verificar_turnos(db)[1] if l[1] == "Hora do intervalo"]
        assert avisos == [(ids["Ana"], "Hora do intervalo",
                           "Seu intervalo de 60 min começa às 12:00. Bata a saída ao parar.")], avisos
        assert not [l for l in regras.verificar_turnos(db)[1] if l[1] == "Hora do intervalo"]  # uma vez só
    # turno que vira a noite: intervalo depois da meia-noite vale
    assert regras.intervalo_cabe("18:00", "02:00", "00:30", 30)
    assert not regras.intervalo_cabe("18:00", "02:00", "01:45", 30)
    assert "intervalo " not in salva(intervalo_inicio="").split("Bruno Lima")[0].split("Ana Souza")[1]

    # ---- outra empresa não mexe nesta
    h = novo()
    posta(h, "/cadastro", csrf(h, "/cadastro"), aceito="1", empresa="Loja B", nome="Hugo", email="h@b.com",
          senha="segredo2")
    posta(h, "/painel/escala/turno", csrf(h, "/painel/config"), usuario_id=ids["Ana"],
          data=dia(31).isoformat(), modo="limpar", repetir="semana", fixar="1")
    with SessionLocal() as db:
        assert len(list(db.scalars(select(EscalaFixa).where(EscalaFixa.usuario_id == ids["Ana"])))) == 5
    print("OK: escala fixa passou")


if __name__ == "__main__":
    main()
