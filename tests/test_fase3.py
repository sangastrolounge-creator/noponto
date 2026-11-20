"""Fase 3: locais extras (filiais), totem (código do minuto, QR, PIN) e atalhos de WhatsApp.
Rodar da raiz: .venv/Scripts/python tests/test_fase3.py"""
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

from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Batida, Local, SessionLocal, Totem, Turno, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
TER, QUA = SEG + timedelta(days=1), SEG + timedelta(days=2)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora: str, dia: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


MATRIZ = (-2.5307, -44.3068)
FILIAL = (-2.5600, -44.2500)   # a uns 7 km da matriz
LONGE = (-2.7000, -44.0000)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def bate(cli, token, **corpo):
    return cli.post("/app/ponto", json=corpo, headers={"X-CSRF": token})


def gps(ponto):
    return dict(lat=ponto[0], lng=ponto[1], precisao=10)


def ultima(nome):
    with SessionLocal() as db:
        return db.scalar(select(Batida).join(Usuario).where(Usuario.nome.startswith(nome))
                         .order_by(Batida.id.desc()).limit(1))


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Padaria A", nome="Gil Gestor",
          email="gil@a.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Padaria A", lat=str(MATRIZ[0]), lng=str(MATRIZ[1]),
          raio_m="150", tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1",
          aparelho_unico="1")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convites = {u.id: u.convite_token for u in db.scalars(select(Usuario)) if u.convite_token}
    cli = {}
    for nome in ("Ana", "Bruno"):
        c = novo()
        caminho = f"/convite/{convites[ids[nome]]}"
        assert posta(c, caminho, csrf(c, caminho), senha="senha123").status_code == 303
        cli[nome] = (c, csrf(c, "/app"))
    (ana, ta), (bruno, tb) = cli["Ana"], cli["Bruno"]

    # ---- locais extras
    posta(g, "/painel/locais", tg, nome="Filial Centro", lat="abc", lng="1", raio_m="150")
    assert "Latitude ou longitude inválida" in g.get("/painel/locais").text
    posta(g, "/painel/locais", tg, nome="Filial Centro", lat=str(FILIAL[0]), lng=str(FILIAL[1]),
          raio_m="100")
    with SessionLocal() as db:
        filial = db.scalar(select(Local)).id
    tela = g.get("/painel/locais").text
    assert "Filial Centro" in tela and "principal" in tela

    # sem local no turno: vale na matriz e na filial; longe das duas, não
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=SEG.isoformat(),
          modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="dia")
    as_("08:00", SEG)
    r = bate(ana, ta, **gps(LONGE))
    assert r.status_code == 400 and "m da empresa" in r.json()["erro"] or "do local" in r.json()["erro"]
    assert bate(ana, ta, **gps(FILIAL)).json()["tipo"] == "entrada"
    b = ultima("Ana")
    assert (b.local_nome, b.dentro, b.origem) == ("Filial Centro", True, "")
    as_("17:00", SEG)
    assert bate(ana, ta, **gps(MATRIZ)).json()["tipo"] == "saida"
    assert ultima("Ana").local_nome == ""

    # turno com local definido: só vale lá
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=TER.isoformat(),
          modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="dia",
          local_id=str(filial))
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert f'data-local="{filial}"' in grade and "<small>Filial Centro</small>" in grade
    assert "wa.me/5598999990001?text=" in grade and "Filial%20Centro" in grade
    as_("08:00", TER)
    assert "Filial Centro" in ana.get("/app").text
    r = bate(ana, ta, **gps(MATRIZ))
    assert r.status_code == 400 and "do local Filial Centro" in r.json()["erro"], r.text
    assert bate(ana, ta, **gps(FILIAL)).status_code == 200
    # copiar a semana leva o local e o intervalo
    posta(g, "/painel/escala/copiar", tg, semana=(SEG + timedelta(days=7)).isoformat())
    with SessionLocal() as db:
        t = db.scalar(select(Turno).where(Turno.data == TER + timedelta(days=7)))
        assert (t.local_id, t.intervalo_min) == (filial, 60)
    # local desativado deixa de valer (o turno volta a aceitar qualquer local)
    as_("17:00", TER)
    posta(g, f"/painel/locais/{filial}", tg, acao="desativar")
    assert bate(ana, ta, **gps(FILIAL)).status_code == 400
    posta(g, f"/painel/locais/{filial}", tg, acao="reativar")
    assert bate(ana, ta, **gps(FILIAL)).json()["tipo"] == "saida"

    # ---- totem
    assert "Totem não ativado" in novo().get("/totem").text
    assert "Usar o código do totem" not in ana.get("/app").text
    posta(g, "/painel/totens", tg, nome="Tablet da entrada", local_id=str(filial))
    tela = g.get("/painel/locais").text
    link = re.search(r'value="(http[^"]+/totem/ativar/[^"]+)"', tela).group(1)
    assert "/totem/ativar/" not in g.get("/painel/locais").text  # aparece uma vez só
    tablet = novo()
    assert tablet.get("/totem/ativar/errado").headers["location"] == "/totem"
    assert "Totem não ativado" in tablet.get("/totem").text
    assert tablet.get(link.split("testserver")[-1]).status_code == 303
    pagina = tablet.get("/totem").text
    assert "Ana Souza" in pagina and "Bruno Lima" in pagina and "Gil Gestor" not in pagina
    assert tablet.get("/totem/qr.svg").text.startswith("<?xml")
    as_("08:00", QUA)
    codigo = tablet.get("/totem/estado").json()["codigo"]
    assert len(codigo) == 6 and "Usar o código do totem" in bruno.get("/app").text
    assert f'value="{codigo}"' in bruno.get(f"/app?codigo={codigo}").text

    # código errado, código certo sem GPS, código vencido
    errado = f"{(int(codigo) + 1) % 1000000:06d}"
    r = bate(bruno, tb, codigo=errado)
    assert r.status_code == 400 and "inválido ou vencido" in r.json()["erro"]
    assert bate(bruno, tb, codigo=codigo).json()["tipo"] == "entrada"
    b = ultima("Bruno")
    assert (b.origem, b.dentro, b.local_nome, b.lat, b.obs) == (
        "codigo", True, "Filial Centro", None, "totem Tablet da entrada")
    as_("08:01", QUA)
    assert regras.codigo_totem  # o código do minuto anterior ainda vale; o de 3 min atrás não
    as_("08:04", QUA)
    assert bate(bruno, tb, codigo=codigo).status_code == 400
    # o aparelho continua sendo conferido mesmo com o código
    outro = novo()
    posta(outro, "/entrar", csrf(outro), login="98999990002", senha="senha123")
    t2 = csrf(outro, "/app")
    r = bate(outro, t2, codigo=tablet.get("/totem/estado").json()["codigo"])
    assert r.status_code == 400 and "aparelho" in r.json()["erro"]
    # outra empresa não usa o código desta
    h = novo()
    posta(h, "/cadastro", csrf(h, "/cadastro"), aceito="1", empresa="Loja B", nome="Hugo", email="hugo@b.com",
          senha="segredo2")
    th = csrf(h, "/app")
    assert bate(h, th, codigo=tablet.get("/totem/estado").json()["codigo"]).status_code == 400
    posta(h, f"/painel/locais/{filial}", csrf(h, "/painel/locais"), acao="desativar")
    with SessionLocal() as db:
        assert db.get(Local, filial).ativo and db.scalar(select(Totem)).nome == "Tablet da entrada"

    # ---- PIN no totem
    tt = re.search(r'"X-CSRF": "([^"]+)"', tablet.get("/totem").text).group(1)
    def no_totem(uid, pin):
        return tablet.post("/totem/ponto", json={"uid": uid, "pin": pin}, headers={"X-CSRF": tt})
    assert "ainda não criou" in no_totem(ids["Ana"], "1234").json()["erro"]
    posta(ana, "/app/pin", ta, senha="senha123", pin="1111")
    assert "não todos iguais" in ana.get("/app").text
    posta(ana, "/app/pin", ta, senha="errada", pin="4821")
    assert "Senha incorreta" in ana.get("/app").text
    posta(ana, "/app/pin", ta, senha="senha123", pin="4821")
    assert "Trocar o PIN do totem" in ana.get("/app").text
    assert no_totem(ids["Ana"], "0000").json()["erro"] == "PIN incorreto."
    as_("08:10", QUA)
    r = no_totem(ids["Ana"], "4821")
    assert r.status_code == 200 and "Ana, entrada registrada às 08:10" in r.json()["mensagem"], r.text
    b = ultima("Ana")
    assert (b.origem, b.dentro, b.local_nome) == ("totem", True, "Filial Centro")
    assert "Seu ponto já foi registrado" in no_totem(ids["Ana"], "4821").json()["erro"]
    for _ in range(5):
        no_totem(ids["Ana"], "9999")
    assert "bloqueado" in no_totem(ids["Ana"], "4821").json()["erro"]
    posta(ana, "/app/pin", ta, senha="senha123", pin="5310")
    as_("17:00", QUA)
    assert no_totem(ids["Ana"], "5310").json()["tipo"] == "saida"
    assert no_totem(ids["Gil"], "1234").status_code == 400      # gestor fora da escala
    assert tablet.post("/totem/ponto", json={"uid": ids["Ana"], "pin": "5310"}).status_code in (400, 403)
    orig = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{QUA.isoformat()}/originais").text
    assert "Totem" in orig and "totem Tablet da entrada" in orig and "Filial Centro" in orig, orig

    # ---- novo endereço e remoção desligam o tablet
    posta(g, f"/painel/totens/{db_totem()}", tg, acao="novo_link")
    assert "Totem não ativado" in tablet.get("/totem").text
    assert tablet.get("/totem/estado").status_code == 403
    posta(g, f"/painel/totens/{db_totem()}", tg, acao="remover")
    with SessionLocal() as db:
        assert not db.scalar(select(Totem))

    # ---- WhatsApp: cobrar quem não bateu
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Bruno"], data=(QUA + timedelta(days=1)).isoformat(),
          modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="dia")
    as_("09:00", QUA + timedelta(days=1))
    hoje = g.get("/painel").text
    assert "Chamar no WhatsApp" in hoje and "wa.me/5598999990002?text=Oi%20Bruno" in hoje
    print("OK: fase 3 passou")


def db_totem():
    with SessionLocal() as db:
        return db.scalar(select(Totem.id))


if __name__ == "__main__":
    main()
