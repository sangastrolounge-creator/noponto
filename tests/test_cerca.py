"""Cerca virtual: contorno no mapa, motivo de quem bate fora e conferência durante o turno.
Rodar da raiz: .venv/Scripts/python tests/test_cerca.py"""
import json
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

from app import cerca, push  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Alerta, Batida, Empresa, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_gestores = lambda *a, **k: avisos.append(a)
push.avisar_alertas = lambda *a, **k: None

# um quarteirão em Miami (~100 m x 100 m)
QUADRA = [[25.7745, -80.1905], [25.7745, -80.1895], [25.7736, -80.1895], [25.7736, -80.1905]]
DENTRO = (25.7740, -80.1900)
FORA_PERTO = (25.7750, -80.1900)    # ~55 m ao norte (dentro do raio de 150 m, fora da quadra)
LONGE = (25.7800, -80.1900)


def as_(hora, d):   # Flórida em novembro: UTC-5
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=5)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def bater(cli, tk, pos, **extra):
    return cli.post("/app/ponto", json={"lat": pos[0], "lng": pos[1], "precisao": 10, **extra},
                    headers={"X-CSRF": tk})


def puras():
    poli = [tuple(p) for p in QUADRA]
    assert cerca.dentro_poligono(*DENTRO, poli) and not cerca.dentro_poligono(*FORA_PERTO, poli)
    assert 40 <= cerca.distancia_borda(*FORA_PERTO, poli) <= 70
    assert cerca.medir(*DENTRO, ("", 25.774, -80.19, 150, poli)) == (0, True)
    assert cerca.medir(*FORA_PERTO, ("", 25.774, -80.19, 150, []))[1] is True        # sem contorno vale o raio
    assert cerca.pontos("[[1,2],[3,4]]") == [] and len(cerca.pontos(json.dumps(QUADRA))) == 4
    assert cerca.pontos("lixo") == []


def main():
    puras()
    as_("08:00", SEG)
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Miami Deli", "nome": "Gil",
                              "email": "gil@md.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    g.post("/painel/config", data={"csrf": tg, "nome": "Miami Deli", "lat": "25.7740", "lng": "-80.1900", "raio_m": "150",
                                   "tolerancia_min": "10", "fuso": "America/New_York", "ajuste_dias": "7",
                                   "fotos_dias": "45", "estado": "FL", "semana_inicio": "6", "frequencia": "weekly",
                                   "periodo_ancora": (SEG - timedelta(days=1)).isoformat(),
                                   "intervalo_padrao_min": "30", "tolerancia_dia_min": "7",
                                   "pct_noturno": "0", "pct_feriado": "0"})     # sem bloquear fora da área
    g.post("/painel/equipe", data={"csrf": tg, "nome": "Ana Souza", "celular": "3055550001"})
    with SessionLocal() as db:
        tk = db.scalar(select(Usuario.convite_token).where(Usuario.celular == "3055550001"))
    ana = novo()
    ana.post(f"/convite/{tk}", data={"csrf": csrf(ana, f"/convite/{tk}"), "senha": "senha123", "consentimento": "1"})
    ta = csrf(ana, "/app")

    # ---- editor da cerca
    loc = g.get("/painel/locais").text
    assert 'href="/painel/cerca/principal"' in loc and 'id="cerca"' in loc
    ed = g.get("/painel/cerca/principal").text
    assert "leaflet.min.js" in ed and "Cerca virtual: Miami Deli" in ed
    g.post("/painel/cerca/principal", data={"csrf": tg, "contorno": "[[1,2],[3,4]]"})
    assert "Desenhe a cerca com 3 a 40 pontos" in g.get("/painel/cerca/principal").text
    g.post("/painel/cerca/principal", data={"csrf": tg, "contorno": json.dumps(QUADRA)})
    assert "cerca desenhada" in g.get("/painel/locais").text

    # ---- fora da quadra (mas dentro do raio antigo): pede o motivo
    r = bater(ana, ta, FORA_PERTO)
    assert r.status_code == 400 and r.json()["codigo"] == "precisa_motivo" and "informe o motivo" in r.json()["erro"]
    r = bater(ana, ta, FORA_PERTO, motivo="Entrega no cliente")
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        b = db.scalar(select(Batida).order_by(Batida.id.desc()))
        assert (b.dentro, b.motivo_fora) == (False, "Entrega no cliente") and 40 <= b.distancia_m <= 70
        a = db.scalar(select(Alerta).where(Alerta.tipo == "fora_area"))
        assert "Motivo: Entrega no cliente" in a.texto

    # ---- conferência durante o turno (desligada por padrão)
    as_("09:00", SEG)
    assert ana.post("/app/cerca", data={"lat": LONGE[0], "lng": LONGE[1]}, headers={"X-CSRF": ta}).json() == {"ativo": False}
    assert "a empresa confere se você está na área" not in ana.get("/app").text
    g.post("/painel/config/cerca", data={"csrf": tg, "motivo_fora": "1", "cerca_turno": "1", "cerca_turno_min": "1"})
    assert "Use de 3 a 120 minutos" in g.get("/painel/locais").text
    g.post("/painel/config/cerca", data={"csrf": tg, "motivo_fora": "1", "cerca_turno": "1", "cerca_turno_min": "10"})
    assert "a empresa confere se você está na área" in ana.get("/app").text
    j = ana.post("/app/cerca", data={"lat": DENTRO[0], "lng": DENTRO[1], "precisao": "10"}, headers={"X-CSRF": ta}).json()
    assert j["ativo"] and j["dentro"] is True
    j = ana.post("/app/cerca", data={"lat": LONGE[0], "lng": LONGE[1], "precisao": "10", "fora_desde_min": "4"},
                 headers={"X-CSRF": ta}).json()
    assert j["dentro"] is False and not j["avisou"]
    j = ana.post("/app/cerca", data={"lat": LONGE[0], "lng": LONGE[1], "precisao": "2000", "fora_desde_min": "12"},
                 headers={"X-CSRF": ta}).json()
    assert j["dentro"] is None                                          # GPS impreciso: não afirma nada
    j = ana.post("/app/cerca", data={"lat": LONGE[0], "lng": LONGE[1], "precisao": "10", "fora_desde_min": "12"},
                 headers={"X-CSRF": ta}).json()
    assert j["avisou"] and avisos[-1][1] == "Fora da área: Ana" and "com o ponto aberto" in avisos[-1][2]
    j = ana.post("/app/cerca", data={"lat": LONGE[0], "lng": LONGE[1], "precisao": "10", "fora_desde_min": "20"},
                 headers={"X-CSRF": ta}).json()
    assert not j["avisou"]                                              # um aviso por turno
    # depois da saída, nada é conferido
    as_("09:30", SEG)
    bater(ana, ta, DENTRO)
    assert ana.post("/app/cerca", data={"lat": LONGE[0], "lng": LONGE[1]}, headers={"X-CSRF": ta}).json() == {"ativo": False}
    # apagar o contorno volta para o raio
    g.post("/painel/cerca/principal", data={"csrf": tg, "contorno": "[]"})
    with SessionLocal() as db:
        assert db.scalar(select(Empresa).where(Empresa.nome == "Miami Deli")).cerca == ""
    print("OK: cerca virtual passou")


if __name__ == "__main__":
    main()
