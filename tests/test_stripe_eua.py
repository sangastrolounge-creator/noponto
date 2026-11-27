"""Empresas dos EUA pagam pelo Stripe (o Brasil continua no Asaas): Checkout, portal, webhook
assinado, liberação do acesso, planos só do país e Master -> Stripe. Stripe simulado.
Rodar da raiz: python tests/test_stripe_eua.py"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com", "STRIPE_SECRET_KEY": "sk_test_simulado",
                   "STRIPE_WEBHOOK_SECRET": "whsec_teste_123456"})
os.environ.pop("PUBLIC_URL", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import paises  # noqa: E402
from app.db import Empresa, Pagamento, Plano, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.paises.us import assinatura as st  # noqa: E402

chamadas = []


class Falso:
    def __init__(self, chave=None):
        def checkout(params):
            chamadas.append(("checkout", params))
            return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}

        def portal(params):
            chamadas.append(("portal", params))
            return {"url": "https://billing.stripe.test/p_1"}

        def cancelar(sid):
            chamadas.append(("cancelar", sid))
            return {"id": sid, "status": "canceled"}
        self.v1 = SimpleNamespace(
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=checkout)),
            billing_portal=SimpleNamespace(sessions=SimpleNamespace(create=portal)),
            subscriptions=SimpleNamespace(cancel=cancelar))


st.cliente = Falso


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def evento(cli, tipo, obj, eid, segredo="whsec_teste_123456"):
    corpo = json.dumps({"id": eid, "type": tipo, "data": {"object": obj}})
    t = int(time.time())
    sig = hmac.new(segredo.encode(), f"{t}.{corpo}".encode(), hashlib.sha256).hexdigest()
    return cli.post("/stripe/webhook", content=corpo,
                    headers={"Stripe-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"})


def main():
    m = TestClient(app, follow_redirects=False)
    posta(m, "/cadastro", csrf(m, "/cadastro"), aceito="1", empresa="Plataforma", nome="Dono",
          email="dono@plataforma.com", senha="segredo1")
    c = TestClient(app, follow_redirects=False)
    posta(c, "/cadastro", csrf(c, "/cadastro"), aceito="1", empresa="Taco Bar", nome="Gil",
          email="gil@t.com", senha="segredo1")
    b = TestClient(app, follow_redirects=False)
    posta(b, "/cadastro", csrf(b, "/cadastro"), aceito="1", empresa="Bar do Zé", nome="Zé",
          email="ze@b.com", senha="segredo1")
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Taco Bar"))
        e.pais = "US"
        paises.aplicar_padroes(e, "US")
        eid = e.id
        db.add_all([Plano(nome="Essentials", preco_centavos=3000, pais="US", ativo=True, ordem=1),
                    Plano(nome="Basic", preco_centavos=0, limite_funcionarios=10, pais="US", ativo=True, ordem=0),
                    Plano(nome="Essencial", preco_centavos=5990, pais="BR", ativo=True, ordem=1)])
        db.commit()
        ess = db.scalar(select(Plano).where(Plano.nome == "Essentials")).id

    # ---- a empresa dos EUA vê só os planos dos EUA, em dólar, e o Stripe; a do Brasil não vê o Stripe
    tela = c.get("/painel/assinatura").text
    assert "página segura do Stripe" in tela and "$30.00" in tela and "Essencial" not in tela, tela[-3000:]
    assert "Asaas" not in tela and "CPF" not in tela
    tela_br = b.get("/painel/assinatura").text
    assert "Stripe" not in tela_br and "Essentials" not in tela_br and "Essencial" in tela_br

    tc = csrf(c, "/painel/assinatura")
    r = posta(c, "/painel/assinatura/pagar", tc, plano=str(ess))
    assert r.headers["location"] == "https://checkout.stripe.test/cs_1", r.headers
    params = chamadas[-1][1]
    assert params["mode"] == "subscription" and params["client_reference_id"] == f"dwus:{eid}"
    assert params["line_items"][0]["price_data"]["unit_amount"] == 3000
    assert params["line_items"][0]["price_data"]["currency"] == "usd"
    assert params["line_items"][0]["price_data"]["recurring"] == {"interval": "month"}
    assert params["customer_email"] == "gil@t.com" and "trial_end" in params["subscription_data"]
    assert params["subscription_data"]["metadata"]["ref"] == f"dwus:{eid}"
    # anual: pague 10 meses, use 12
    posta(c, "/painel/assinatura/pagar", tc, plano=str(ess), ciclo="anual")
    p_anual = chamadas[-1][1]["line_items"][0]["price_data"]
    assert p_anual["recurring"] == {"interval": "year"} and p_anual["unit_amount"] == 30000
    # plano do Brasil não vale para a empresa dos EUA
    with SessionLocal() as db:
        essencial = db.scalar(select(Plano).where(Plano.nome == "Essencial")).id
    n = len(chamadas)
    posta(c, "/painel/assinatura/pagar", tc, plano=str(essencial))
    assert len(chamadas) == n

    # ---- aviso com assinatura errada é recusado
    assert evento(c, "invoice.paid", {}, "evt_x", segredo="whsec_errado").status_code == 400
    # ---- checkout concluído e primeira fatura paga
    assert evento(c, "checkout.session.completed", {
        "client_reference_id": f"dwus:{eid}", "customer": "cus_1", "subscription": "sub_1",
        "metadata": {"plano_id": str(ess), "ciclo": "mensal"}}, "evt_1").status_code == 200
    fim = int((datetime.now(timezone.utc) + timedelta(days=40)).timestamp())
    fatura = {"id": "in_1", "customer": "cus_1", "amount_paid": 3000,
              "hosted_invoice_url": "https://invoice.stripe.test/in_1",
              "parent": {"subscription_details": {"subscription": "sub_1"}},
              "lines": {"data": [{"period": {"start": fim - 86400 * 30, "end": fim}}]}}
    assert evento(c, "invoice.paid", fatura, "evt_2").status_code == 200
    assert evento(c, "invoice.paid", fatura, "evt_2").status_code == 200   # repetido: ignora
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        assert (e.stripe_cliente_id, e.stripe_assinatura_id, e.plano_id) == ("cus_1", "sub_1", ess)
        assert abs((e.pago_ate - datetime.fromtimestamp(fim, timezone.utc).replace(tzinfo=None)).total_seconds()) < 2
        pagos = list(db.scalars(select(Pagamento).where(Pagamento.empresa_id == eid)))
        assert len(pagos) == 1 and pagos[0].status == "pago" and pagos[0].valor_centavos == 3000
        assert pagos[0].forma == "cartao" and pagos[0].stripe_id == "in_1"
    tela = c.get("/painel/assinatura").text
    assert "Assinatura ativa" in tela and "Gerenciar assinatura" in tela and "$30.00" in tela
    r = posta(c, "/painel/assinatura/portal", tc)
    assert r.headers["location"] == "https://billing.stripe.test/p_1" and chamadas[-1][1]["customer"] == "cus_1"
    posta(c, "/painel/assinatura/pagar", tc, plano=str(ess))
    assert "já está ativa" in c.get("/painel/assinatura").text

    # ---- fatura recusada e assinatura cancelada
    evento(c, "invoice.payment_failed", {"id": "in_2", "subscription": "sub_1", "amount_due": 3000,
                                         "hosted_invoice_url": "https://invoice.stripe.test/in_2"}, "evt_3")
    with SessionLocal() as db:
        assert db.scalar(select(Pagamento).where(Pagamento.stripe_id == "in_2")).status == "vencido"
    evento(c, "customer.subscription.deleted", {"id": "sub_1"}, "evt_4")
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        assert e.stripe_assinatura_id == "" and e.pago_ate is not None   # o acesso pago continua
        assert db.scalar(select(Pagamento).where(Pagamento.stripe_id == "in_2")).status == "cancelado"

    # ---- a fatura chega ANTES do "checkout concluído": com os metadados, a empresa é achada na hora
    fim2 = int((datetime.now(timezone.utc) + timedelta(days=70)).timestamp())
    agora_ts = int(time.time())
    adiantada = {"id": "in_3", "customer": "cus_novo", "amount_paid": 3000, "created": agora_ts,
                 "parent": {"subscription_details": {"subscription": "sub_2", "metadata": {"ref": f"dwus:{eid}"}}},
                 "lines": {"data": [{"period": {"start": fim2 - 86400 * 30, "end": fim2}}]}}
    assert evento(c, "invoice.paid", adiantada, "evt_6").status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(Pagamento).where(Pagamento.stripe_id == "in_3")).status == "pago"
    # sem metadados: pede reenvio; depois do checkout, o reenvio vale
    sem_meta = {"id": "in_4", "customer": "cus_3", "amount_paid": 3000, "created": agora_ts,
                "parent": {"subscription_details": {"subscription": "sub_3"}},
                "lines": {"data": [{"period": {"start": fim2 - 86400 * 30, "end": fim2 + 86400}}]}}
    assert evento(c, "invoice.paid", sem_meta, "evt_7").status_code == 409
    evento(c, "checkout.session.completed", {"client_reference_id": f"dwus:{eid}", "customer": "cus_3",
                                             "subscription": "sub_3"}, "evt_8")
    assert evento(c, "invoice.paid", sem_meta, "evt_7").status_code == 200
    # outro sistema na mesma conta Stripe, ou empresa do Brasil: ignorado
    with SessionLocal() as db:
        bid = db.scalar(select(Empresa.id).where(Empresa.nome == "Bar do Zé"))
    assert evento(c, "checkout.session.completed", {"client_reference_id": "outro:1"}, "evt_5").status_code == 200
    evento(c, "checkout.session.completed", {"client_reference_id": f"dwus:{bid}", "customer": "cus_br",
                                             "subscription": "sub_br"}, "evt_10")
    with SessionLocal() as db:
        assert db.get(Empresa, bid).stripe_assinatura_id == ""

    # ---- Master -> Stripe: tela, e o Master encerra a assinatura
    assert "Cobrança pelo Stripe (EUA)" in m.get("/master/stripe").text
    assert 'href="/master/stripe"' in m.get("/master").text
    tm = csrf(m, f"/master/empresas/{eid}")
    assert "Encerrar assinatura no Stripe" in m.get(f"/master/empresas/{eid}").text
    posta(m, f"/master/empresas/{eid}/cancelar-stripe", tm)
    assert chamadas[-1] == ("cancelar", "sub_3")
    with SessionLocal() as db:
        assert db.get(Empresa, eid).stripe_assinatura_id == ""
    # a rota interna dos EUA não abre direto
    assert c.get("/eua/painel/assinatura").status_code == 404
    print("OK: Stripe dos EUA passou")


if __name__ == "__main__":
    main()
