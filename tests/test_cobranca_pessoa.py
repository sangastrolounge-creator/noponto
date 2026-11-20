"""Cobrança por pessoa (base + por pessoa que trabalhou nos últimos 30 dias, com mínimo) e cerca por plano.
Stripe simulado. Rodar da raiz: .venv/Scripts/python tests/test_cobranca_pessoa.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com", "STRIPE_SECRET_KEY": "sk_test_simulado",
                   "STRIPE_WEBHOOK_SECRET": "whsec_teste_123456"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import cobranca, core  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Empresa, Plano, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": datetime(2026, 11, 16, 13, 0)}
banco.agora = lambda: relogio["agora"]
chamadas = []
estado = {"valor": 2900, "produto": "prod_1"}


class Falso:
    def __init__(self):
        def checkout(params):
            chamadas.append(("checkout", params))
            return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}

        def retrieve(sid):
            return {"id": sid, "items": {"data": [{"id": "si_1", "price": {
                "unit_amount": estado["valor"], "product": estado["produto"]}}]}}

        def update(sid, params):
            chamadas.append(("update", sid, params))
            estado["valor"] = params["items"][0]["price_data"]["unit_amount"]
            return {"id": sid}
        self.v1 = SimpleNamespace(
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=checkout)),
            subscriptions=SimpleNamespace(retrieve=retrieve, update=update))


cobranca.cliente = Falso


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    # ---- conta do valor
    p = Plano(nome="X", preco_centavos=0, base_centavos=2000, por_pessoa_centavos=600, minimo_centavos=2900)
    assert p.por_pessoa and p.valor_mensal(0) == 2900 and p.valor_mensal(1) == 2900 and p.valor_mensal(15) == 11000
    assert Plano(nome="F", preco_centavos=5900).valor_mensal(40) == 5900

    m = novo()
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    tm = csrf(m, "/master/planos")
    # ---- Master: plano por pessoa
    r = m.post("/master/planos", data={"csrf": tm, "nome": "Inicial", "preco": "0", "limite": "15", "limite_locais": "1",
                                       "ordem": "1", "ativo": "1", "com_gps": "1", "com_foto": "1",
                                       "por_pessoa": "4.00", "minimo": "29.00"})
    m.post("/master/planos", data={"csrf": tm, "nome": "Intermediario", "preco": "0", "limite": "0", "limite_locais": "5",
                                   "ordem": "2", "ativo": "1", "com_gps": "1", "com_foto": "1", "com_alertas": "1",
                                   "com_cerca": "1", "base": "20.00", "por_pessoa": "6.00"})
    m.post("/master/planos", data={"csrf": tm, "nome": "Ruim", "preco": "0", "limite": "0", "ordem": "3",
                                   "por_pessoa": "abc"})
    assert "Valores da cobrança por pessoa inválidos" in m.get("/master/planos").text
    with SessionLocal() as db:
        ini = db.scalar(select(Plano).where(Plano.nome == "Inicial"))
        inter = db.scalar(select(Plano).where(Plano.nome == "Intermediario"))
        assert (ini.por_pessoa_centavos, ini.minimo_centavos, ini.preco_centavos, ini.com_cerca) == (400, 2900, 2900, False)
        assert (inter.base_centavos, inter.por_pessoa_centavos, inter.preco_centavos) == (2000, 600, 2600)
        ids = {"ini": ini.id, "inter": inter.id}
    vitrine = novo().get("/").text
    assert "$4.00" in vitrine and "/pessoa/mês" in vitrine and "mínimo $29.00/mês" in vitrine and "+ $20.00/mês de base" in vitrine

    # ---- empresa com 3 pessoas, 2 trabalharam nos últimos 30 dias
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Taco Bar", "nome": "Gil",
                              "email": "gil@t.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bia Reis", "3055550002"), ("Caio Lima", "3055550003")):
        g.post("/painel/equipe", data={"csrf": tg, "nome": nome, "celular": cel})
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        e = db.scalar(select(Empresa).where(Empresa.nome == "Taco Bar"))
        eid = e.id
    for nome, dias in (("Ana", 2), ("Bia", 40)):     # Bia trabalhou há 40 dias: não conta
        d = SEG - timedelta(days=dias)
        relogio["agora"] = datetime(d.year, d.month, d.day, 18, 0)
        g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": us[nome].id, "tipo": "entrada",
                                             "data": d.isoformat(), "hora": "09:00", "obs": "t"})
    relogio["agora"] = datetime(2026, 11, 16, 13, 0)
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        assert cobranca.pessoas_cobradas(db, e) == 1
        inter = db.get(Plano, ids["inter"])
        assert cobranca.valor_atual(db, e, inter) == (2600, 1)

    # ---- checkout cobra o valor de hoje
    t = g.get("/painel/assinatura").text
    assert "hoje daria $26.00/mês" in t and "hoje daria $29.00/mês" in t
    g.post("/painel/assinatura/pagar", data={"csrf": tg, "plano": ids["inter"]})
    ck = [c for c in chamadas if c[0] == "checkout"][-1][1]
    assert ck["line_items"][0]["price_data"]["unit_amount"] == 2600

    # ---- assinatura ativa: ajuste diário do valor
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        e.plano_id, e.stripe_assinatura_id = ids["inter"], "sub_1"
        db.commit()
    estado["valor"] = 2600
    for nome in ("Bia", "Caio"):   # mais duas pessoas trabalham
        relogio["agora"] = datetime(2026, 11, 17, 18, 0)
        g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": us[nome].id, "tipo": "entrada",
                                             "data": "2026-11-17", "hora": "09:00", "obs": "t"})
    with SessionLocal() as db:
        assert cobranca.ajustar_valores(db) == 1
        upd = [c for c in chamadas if c[0] == "update"][-1]
        assert upd[2]["items"][0]["price_data"] == {"currency": "usd", "product": "prod_1", "unit_amount": 3800,
                                                    "recurring": {"interval": "month"}}
        assert upd[2]["proration_behavior"] == "none"
        assert cobranca.ajustar_valores(db) == 0                     # uma vez por dia
        e = db.get(Empresa, eid)
        assert (e.pessoas_cobradas, e.valor_assinatura_centavos) == (3, 3800)
    t = g.get("/painel/assinatura").text
    assert "Pessoas cobradas" in t and "próxima fatura: $38.00" in t

    # ---- cerca por plano: Inicial não inclui
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        e.plano, e.teste_ate, e.pago_ate = db.get(Plano, ids["ini"]), None, datetime(2026, 12, 31)
        e.lat, e.lng = 25.77, -80.19
        db.commit()
        e = db.get(Empresa, eid)
        assert core.situacao(e) == "ativa" and not core.cerca_liberada(e)
    assert "Desenhar cerca" not in g.get("/painel/locais").text
    g.get("/painel/cerca/principal")
    assert "Seu plano não inclui a cerca virtual" in g.get("/painel/locais").text
    print("OK: cobrança por pessoa e cerca por plano passou")


if __name__ == "__main__":
    main()
