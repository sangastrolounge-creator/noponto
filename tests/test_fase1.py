"""Fase 1 (confiança no ponto): aparelho autorizado, foto, anulação com histórico,
pedido de ajuste e lembretes. Rodar da raiz: .venv/Scripts/python tests/test_fase1.py"""
import base64
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
from app.db import (PASTA_FOTOS, Ajuste, Alerta, Aparelho, Batida, Historico,  # noqa: E402
                    SessionLocal, Usuario)
from app.main import app  # noqa: E402

HOJE = date(2026, 11, 16)  # segunda-feira
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora: str, dia: date = HOJE):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


LAT, LNG = -2.5307, -44.3068
FOTO = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 500).decode()


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def bater(cli, token, foto=None):
    return cli.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10, "foto": foto},
                    headers={"X-CSRF": token})


def entrar(celular):
    """Novo navegador (= novo aparelho) logado como o funcionário."""
    c = novo()
    r = posta(c, "/entrar", csrf(c), login=celular, senha="senha123")
    assert r.headers["location"] == "/app", r.text
    return c, csrf(c, "/app")


def conta(modelo, *filtros):
    with SessionLocal() as db:
        return len(list(db.scalars(select(modelo).where(*filtros))))


def config(g, tg, **extra):
    base = dict(nome="Padaria A", lat=str(LAT), lng=str(LNG), raio_m="150", tolerancia_min="10",
                fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7", fotos_dias="45")
    base.update(extra)
    assert posta(g, "/painel/config", tg, **base).status_code == 303


def main():
    as_("07:00")
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Padaria A", nome="Gil Gestor",
          email="gil@a.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    config(g, tg, aparelho_unico="1")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convites = {u.id: u.convite_token for u in db.scalars(select(Usuario)) if u.convite_token}
    cel = {}
    for nome in ("Ana", "Bruno"):
        c = novo()
        caminho = f"/convite/{convites[ids[nome]]}"
        assert posta(c, caminho, csrf(c, caminho), senha="senha123").status_code == 303
        cel[nome] = (c, csrf(c, "/app"))
    ana, ta = cel["Ana"]
    bruno, tb = cel["Bruno"]
    for uid, ini in ((ids["Ana"], "08:00"), (ids["Bruno"], "08:00")):
        posta(g, "/painel/escala/turno", tg, usuario_id=uid, data=HOJE.isoformat(),
              modo="trabalho", inicio=ini, fim="16:00", repetir="uteis")

    # ---- lembretes: 5 min antes da entrada, uma vez só
    as_("07:56")
    with SessionLocal() as db:
        alertas, lembretes = regras.verificar_turnos(db)
        assert not alertas and sorted(l[0] for l in lembretes) == sorted([ids["Ana"], ids["Bruno"]])
        assert "Hora de bater o ponto" == lembretes[0][1]
        assert regras.verificar_turnos(db) == ([], [])

    # ---- aparelho: o primeiro fica autorizado
    as_("08:00")
    r = bater(ana, ta)
    assert r.status_code == 200 and r.json()["tipo"] == "entrada", r.text
    assert conta(Aparelho, Aparelho.usuario_id == ids["Ana"], Aparelho.situacao == "autorizado") == 1
    assert "Aparelho autorizado" in g.get("/painel/equipe").text

    # colega tentando bater o ponto do Bruno no celular da Ana
    r = posta(ana, "/entrar", csrf(ana), login="98999990002", senha="senha123")
    assert r.headers["location"] == "/app"
    r = bater(ana, csrf(ana, "/app"))
    assert r.status_code == 400 and "outro colaborador" in r.json()["erro"], r.text
    assert conta(Alerta, Alerta.tipo == "aparelho") == 1
    assert "no aparelho de Ana Souza" in g.get("/painel").text
    assert conta(Batida, Batida.usuario_id == ids["Bruno"]) == 0

    # Bruno no próprio celular: funciona. Sem lembrete repetido de "não bateu".
    as_("08:05")
    assert bater(bruno, tb).status_code == 200

    # Ana em outro aparelho: bloqueia e pede liberação; o gestor libera; o antigo deixa de valer
    as_("12:00")
    ana2, ta2 = entrar("98999990001")
    r = bater(ana2, ta2)
    assert r.status_code == 400 and "não é o seu aparelho autorizado" in r.json()["erro"], r.text
    bater(ana2, ta2)
    assert conta(Aparelho, Aparelho.usuario_id == ids["Ana"], Aparelho.situacao == "pendente") == 1
    assert "1 aparelho(s) novo(s)" in g.get("/painel").text
    equipe = g.get("/painel/equipe").text
    assert "Novo aparelho pedindo liberação" in equipe
    with SessionLocal() as db:
        ap_id = db.scalar(select(Aparelho.id).where(Aparelho.situacao == "pendente"))
    posta(g, f"/painel/equipe/{ids['Ana']}", tg, acao="autorizar_aparelho", aparelho_id=ap_id)
    r = bater(ana2, ta2)
    assert r.status_code == 200 and r.json()["tipo"] == "saida", r.text
    # o aparelho antigo virou "novo" e precisa de liberação outra vez
    as_("13:00")
    ana, ta = entrar("98999990001")  # mesmo login, outro navegador = outro aparelho
    assert bater(ana, ta).status_code == 400
    with SessionLocal() as db:
        ap_id = db.scalar(select(Aparelho.id).where(Aparelho.situacao == "pendente"))
    posta(g, f"/painel/equipe/{ids['Ana']}", tg, acao="recusar_aparelho", aparelho_id=ap_id)
    r = bater(ana, ta)
    assert r.status_code == 400 and "não liberou" in r.json()["erro"], r.text
    # liberar troca: o próximo aparelho fica autorizado sozinho
    posta(g, f"/painel/equipe/{ids['Ana']}", tg, acao="liberar_troca")
    assert bater(ana, ta).status_code == 200  # entrada da volta do almoço

    # ---- foto obrigatória
    config(g, tg, aparelho_unico="1", exigir_foto="1")
    as_("16:00")
    r = bater(ana, ta)
    assert r.status_code == 400 and "foto é obrigatória" in r.json()["erro"], r.text
    r = bater(ana, ta, "data:image/jpeg;base64," + base64.b64encode(b"nao e jpeg").decode())
    assert r.status_code == 400 and "ler a foto" in r.json()["erro"]
    r = bater(ana, ta, FOTO)
    assert r.status_code == 200 and r.json()["tipo"] == "saida", r.text
    with SessionLocal() as db:
        b = db.scalar(select(Batida).where(Batida.foto != ""))
        bid, arquivo = b.id, PASTA_FOTOS / str(b.empresa_id) / b.foto
    assert arquivo.is_file()
    r = g.get(f"/painel/ponto/foto/{bid}")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert f"/painel/ponto/foto/{bid}" in g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{HOJE}/originais").text
    assert ana.get(f"/painel/ponto/foto/{bid}").status_code == 303  # funcionário não vê
    h = novo()
    posta(h, "/cadastro", csrf(h, "/cadastro"), aceito="1", empresa="Loja B", nome="Hugo", email="hugo@b.com",
          senha="segredo2")
    assert h.get(f"/painel/ponto/foto/{bid}").status_code == 303  # outra empresa não vê
    th = csrf(h, "/painel/config")

    # ---- anular: sai das contas, fica no histórico
    espelho = g.get(f"/painel/ponto?de={HOJE}&ate={HOJE}").text
    assert "7h00" in espelho, espelho  # Ana: 08:00-12:00 + 13:00-16:00
    posta(h, f"/painel/ponto/{bid}/apagar", th)  # outra empresa não anula
    assert conta(Batida, Batida.anulada.is_(True)) == 0
    posta(g, f"/painel/ponto/{bid}/apagar", tg)
    assert conta(Batida, Batida.id == bid, Batida.anulada.is_(True)) == 1
    assert "7h00" not in g.get(f"/painel/ponto?de={HOJE}&ate={HOJE}").text
    hist = g.get("/painel/historico").text
    assert "batida anulada" in hist and "Anulou a saída de Ana Souza de 16/11/2026 16:00" in hist
    assert "Batidas anuladas" in hist and "Gil Gestor em 16/11/2026" in hist
    assert "regras do ponto alteradas" in hist and "aparelho autorizado" in hist
    assert "Ana Souza" not in h.get("/painel/historico").text
    assert "Bater saída" in ana.get("/app").text  # a entrada das 13h voltou a ficar aberta

    # ---- pedido de ajuste
    as_("18:00")
    assert "Esqueceu de bater" in ana.get("/app/ajustes").text
    posta(ana, "/app/ajustes", ta, tipo="saida", data=HOJE.isoformat(), hora="16:00", motivo="x")
    assert "Explique o motivo" in ana.get("/app/ajustes").text
    posta(ana, "/app/ajustes", ta, tipo="saida", data=HOJE.isoformat(), hora="19:00",
          motivo="saí mais tarde")
    assert "ainda não chegou" in ana.get("/app/ajustes").text
    posta(ana, "/app/ajustes", ta, tipo="saida", data=(HOJE - timedelta(days=9)).isoformat(),
          hora="16:00", motivo="faz tempo")
    assert "até 7 dias" in ana.get("/app/ajustes").text
    posta(ana, "/app/ajustes", ta, tipo="saida", data=HOJE.isoformat(), hora="16:00",
          motivo="celular sem bateria")
    posta(ana, "/app/ajustes", ta, tipo="entrada", data=HOJE.isoformat(), hora="07:00",
          motivo="cheguei mais cedo")
    assert ana.get("/app/ajustes").text.count("Aguardando") == 2
    assert "2 pedido(s) de ajuste" in g.get("/painel").text
    tela = g.get(f"/painel/ponto?de={HOJE}&ate={HOJE}").text
    assert "Pedidos de ajuste (2)" in tela and "celular sem bateria" in tela
    with SessionLocal() as db:
        a1, a2 = [a.id for a in db.scalars(select(Ajuste).order_by(Ajuste.id))]
    posta(h, f"/painel/ajustes/{a1}", th, acao="aprovar")  # outra empresa não decide
    assert conta(Ajuste, Ajuste.situacao == "pendente") == 2
    posta(g, f"/painel/ajustes/{a1}", tg, acao="aprovar", resposta="ok")
    posta(g, f"/painel/ajustes/{a2}", tg, acao="recusar", resposta="a escala começa às 8")
    posta(g, f"/painel/ajustes/{a1}", tg, acao="recusar")  # já decidido: não muda
    tela = g.get(f"/painel/ponto?de={HOJE}&ate={HOJE}").text
    assert "Pedidos de ajuste" not in tela and "7h00" in tela, tela
    assert "Ajuste pedido: celular sem bateria" in g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{HOJE}/originais").text
    meus = ana.get("/app/ajustes").text
    assert "Aprovado" in meus and "Recusado" in meus and "a escala começa às 8" in meus
    hist = g.get("/painel/historico").text
    assert "ajuste aprovado" in hist and "ajuste recusado" in hist
    assert conta(Historico, Historico.acao == "ajuste aprovado") == 1

    # ---- fotos vencidas são apagadas, a batida fica
    posta(g, "/painel/config", tg, nome="Padaria A", raio_m="150", tolerancia_min="10",
          ajuste_dias="7", fotos_dias="90")  # acima de 45 dias não é aceito
    assert "fotos de 1 a 45 dias" in g.get("/painel/config").text
    assert "dlg-foto" in g.get("/painel/ponto").text
    as_("09:00", HOJE + timedelta(days=44))
    with SessionLocal() as db:
        assert regras.limpar_fotos(db) == 0 and arquivo.exists()
    as_("09:00", HOJE + timedelta(days=46))
    with SessionLocal() as db:
        assert regras.limpar_fotos(db) == 1
    assert not arquivo.exists() and conta(Batida, Batida.id == bid) == 1
    assert g.get(f"/painel/ponto/foto/{bid}").status_code == 404
    print("OK: fase 1 passou")


if __name__ == "__main__":
    main()
