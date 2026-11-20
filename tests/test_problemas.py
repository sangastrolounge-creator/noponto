"""Problemas de ponto em lote e regras automáticas: entrada antecipada bloqueada, saída automática
para conferir, saída esquecida com horário sugerido. Rodar da raiz:
.venv/Scripts/python tests/test_problemas.py"""
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

from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Batida, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

DOM = date(2026, 11, 15)
SEG, TER = DOM + timedelta(days=1), DOM + timedelta(days=2)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
LAT, LNG = 25.77, -80.19


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=5)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def flash(cli, caminho):
    m = re.search(r'class="aviso[^"]*">([^<]+)<', cli.get(caminho).text)
    return m.group(1) if m else ""


def saidas(uid):
    with SessionLocal() as db:
        return [(regras.para_local(db.get(Usuario, uid).empresa, b.momento).strftime("%m/%d %H:%M"),
                 b.origem, b.conferida, b.anulada)
                for b in db.scalars(select(Batida).where(Batida.usuario_id == uid, Batida.tipo == "saida")
                                    .order_by(Batida.id))]


def main():
    as_("07:00", DOM)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Gil", email="gil@d.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Diner", lat=str(LAT), lng=str(LNG), raio_m="150",
          tolerancia_min="10", fuso="America/New_York", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", estado="FL", semana_inicio="6", frequencia="weekly",
          periodo_ancora=DOM.isoformat(), intervalo_padrao_min="30", tolerancia_dia_min="7",
          pct_noturno="0", pct_feriado="0")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bruno Lima", "3055550002"), ("Caio Reis", "3055550003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        token = db.get(Usuario, ids["Ana"]).convite_token
    ana = novo()
    posta(ana, f"/convite/{token}", csrf(ana, f"/convite/{token}"), senha="senha123", consentimento="1")
    ta = csrf(ana, "/app")
    for nome, ini, fim in (("Ana", "09:00", "17:00"), ("Bruno", "09:00", "13:00")):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=SEG.isoformat(), modo="trabalho",
              inicio=ini, fim=fim, intervalo="0", repetir="uteis")

    # ---- regras: só o dono vê e salva
    tela = g.get("/painel/ponto/problemas").text
    assert "Regras automáticas do ponto" in tela and "Nenhum problema" in tela
    posta(g, "/painel/config/regras-ponto", tg, entrada_antecipada_min="abc", saida_auto_min="0")
    assert "entre 0 e 720" in flash(g, "/painel/ponto/problemas")
    posta(g, "/painel/config/regras-ponto", tg, entrada_antecipada_min="30", saida_auto_min="30")

    # ---- entrada antecipada: às 8h ainda não abre; às 8h40 abre
    as_("08:00", SEG)
    r = ana.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10}, headers={"X-CSRF": ta})
    assert r.status_code == 400 and "O ponto abre às 08:30" in r.json()["erro"], r.text
    as_("08:40", SEG)
    r = ana.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10}, headers={"X-CSRF": ta})
    assert r.status_code == 200 and r.json()["tipo"] == "entrada"

    # ---- saída automática: Bruno entra às 9h e esquece a saída (turno até 13h, regra de 30 min)
    as_("12:00", SEG)
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Bruno"], tipo="entrada", data=SEG.isoformat(),
          hora="09:00", obs="t")
    as_("13:20", SEG)
    with SessionLocal() as db:
        regras.verificar_turnos(db)
    assert saidas(ids["Bruno"]) == []
    as_("13:31", SEG)
    with SessionLocal() as db:
        _, lembretes = regras.verificar_turnos(db)
    assert saidas(ids["Bruno"]) == [("11/16 13:00", "auto", False, False)]
    assert any(l[0] == ids["Bruno"] and "automaticamente" in l[1] for l in lembretes)
    with SessionLocal() as db:   # não duplica no minuto seguinte
        as_("13:32", SEG)
        regras.verificar_turnos(db)
    assert len(saidas(ids["Bruno"])) == 1

    # ---- Ana sai às 17h; terça esquece a saída; Caio entra no domingo sem escala e esquece
    as_("17:00", SEG)
    ana.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10}, headers={"X-CSRF": ta})
    posta(g, "/painel/config/regras-ponto", tg, entrada_antecipada_min="30", saida_auto_min="0")
    as_("18:00", TER)
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Ana"], tipo="entrada", data=TER.isoformat(),
          hora="09:00", obs="t")
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Caio"], tipo="entrada", data=DOM.isoformat(),
          hora="10:00", obs="t")
    tela = g.get("/painel/ponto/problemas").text
    assert tela.count("Saída esquecida") >= 2 and "Saída automática para conferir" in tela
    assert 'value="17:00"' in tela and 'value="13:00"' in tela and "Sem escala: informe o horário." in tela
    assert "Problemas (3)" in g.get("/painel/ponto").text

    # ---- resolver em lote: Ana com a sugestão, Bruno trocando o horário, Caio sem horário
    tp = csrf(g, "/painel/ponto/problemas")
    r = g.post("/painel/ponto/problemas", data={
        "csrf": tp, f"sel_s-{ids['Ana']}-{TER.isoformat()}": "1", f"hora_s-{ids['Ana']}-{TER.isoformat()}": "17:00",
        f"sel_s-{ids['Caio']}-{DOM.isoformat()}": "1", f"hora_s-{ids['Caio']}-{DOM.isoformat()}": ""})
    assert "1 problema(s) resolvido(s). 1 com horário inválido" in flash(g, "/painel/ponto")
    with SessionLocal() as db:
        auto_id = db.scalar(select(Batida.id).where(Batida.origem == "auto"))
    g.post("/painel/ponto/problemas", data={"csrf": tp, f"sel_a-{auto_id}": "1", f"hora_a-{auto_id}": "13:15"})
    assert ("11/17 17:00", "", True, False) in saidas(ids["Ana"])
    sb = saidas(ids["Bruno"])
    assert sb[0] == ("11/16 13:00", "auto", True, True) and sb[1][0] == "11/16 13:15", sb
    tela = g.get("/painel/ponto/problemas").text
    assert "Caio Reis" in tela and "Bruno Lima" not in tela and "Problemas (1)" in g.get("/painel/ponto").text
    print("OK: problemas de ponto e regras automáticas passou")


if __name__ == "__main__":
    main()
