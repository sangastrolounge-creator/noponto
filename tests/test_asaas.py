"""Cobrança pelo Asaas, com um Asaas de mentira no lugar da API real.
Rodar da raiz: .venv/Scripts/python tests/test_asaas.py"""
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
TOKEN = "t" * 40
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
    "SECRET_KEY": "teste", "ADMIN_EMAIL": "master@a.com",
    "ASAAS_API_KEY": "chave-de-mentira", "ASAAS_WEBHOOK_TOKEN": TOKEN,
    "PUBLIC_URL": "https://turno.exemplo.test", "SEM_AGENDADOR": "1",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import asaas  # noqa: E402
from app.db import Empresa, Pagamento, Plano, SessionLocal, agora  # noqa: E402
from app.main import app  # noqa: E402
from sqlalchemy import select  # noqa: E402

CPF_OK = "529.982.247-25"


class AsaasFalso:
    def __init__(self):
        self.chamadas, self.cobrancas, self.n = [], {}, 0

    def __call__(self, metodo, caminho, corpo=None, params=None):
        self.chamadas.append((metodo, caminho, corpo))
        self.n += 1
        if (metodo, caminho) == ("POST", "/customers"):
            return {"id": f"cus_{self.n}"}
        if (metodo, caminho) == ("POST", "/payments"):
            if corpo and "callback" in corpo:  # como o Asaas real com domínio não cadastrado
                raise asaas.AsaasErro("domínio do retorno não cadastrado", 400)
            p = {"id": f"pay_{self.n}", "invoiceUrl": f"https://asaas.exemplo.test/i/{self.n}",
                 "value": corpo["value"], "status": "PENDING", "dueDate": corpo["dueDate"]}
            self.cobrancas[p["id"]] = p
            return p
        if (metodo, caminho) == ("POST", "/subscriptions"):
            if corpo and "callback" in corpo:
                raise asaas.AsaasErro("domínio do retorno não cadastrado", 400)
            self.assinatura = {"id": f"sub_{self.n}", "corpo": corpo}
            return {"id": self.assinatura["id"]}
        if metodo == "GET" and caminho.endswith("/payments") and "/subscriptions/" in caminho:
            p = {"id": f"pay_{self.n}", "invoiceUrl": f"https://asaas.exemplo.test/i/{self.n}",
                 "value": self.assinatura["corpo"]["value"], "status": "PENDING",
                 "dueDate": self.assinatura["corpo"]["nextDueDate"],
                 "subscription": self.assinatura["id"]}
            self.cobrancas[p["id"]] = p
            return {"data": [p]}
        if metodo == "GET" and caminho.startswith("/payments/"):
            return self.cobrancas[caminho.split("/")[-1]]
        if metodo == "DELETE" and caminho.startswith("/subscriptions/"):
            return {"deleted": True}
        raise AssertionError(f"chamada inesperada: {metodo} {caminho}")


falso = AsaasFalso()
asaas.chamar = falso


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def cadastrar(empresa, email):
    cli = TestClient(app, follow_redirects=False)
    r = cli.post("/cadastro", data={"csrf": csrf(cli, "/cadastro"), "empresa": empresa, "nome": "Fulano",
                                    "email": email, "senha": "segredo123", "plano": "1", "aceito": "1"})
    assert r.status_code == 303, r.text
    return cli


def empresa(eid=2):
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        db.expunge(e)
        return e


def aviso(tipo, pay=None, ass=None, eid=None, token=TOKEN):
    corpo = {"event": tipo}
    if eid:
        corpo["id"] = eid
    if pay:
        corpo["payment"] = pay
    if ass:
        corpo["subscription"] = ass
    return TestClient(app).post("/asaas/webhook", json=corpo, headers={"asaas-access-token": token})


def perto(a, b, minutos=5):
    return abs((a - b).total_seconds()) < minutos * 60


def main():
    assert asaas.cpf_cnpj_valido(CPF_OK) and asaas.cpf_cnpj_valido("11.222.333/0001-81")
    assert not asaas.cpf_cnpj_valido("111.111.111-11") and not asaas.cpf_cnpj_valido("123")
    assert asaas.mais_um_mes("2026-01-31").date().isoformat() == "2026-02-28"
    assert asaas.mais_um_mes("2026-12-15").date().isoformat() == "2027-01-15"

    with SessionLocal() as db:   # os planos de exemplo nascem fora de venda
        for p in db.scalars(select(Plano)):
            p.ativo = True
        db.commit()
    m = cadastrar("Plataforma", "master@a.com")
    b = cadastrar("Clínica B", "dono@b.com")
    tb = csrf(b, "/painel/assinatura")
    tela = b.get("/painel/assinatura").text
    assert "Ir para o pagamento" in tela and "CPF ou CNPJ" in tela and "Cobrança automática" in tela
    assert "Ir para o pagamento" not in m.get("/painel/assinatura").text, "cortesia não paga"

    # --- avulso: exige CPF/CNPJ válido
    r = b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "1", "modo": "avulso", "cpf_cnpj": "111"})
    assert r.headers["location"] == "/painel/assinatura"
    assert "CPF ou CNPJ válido" in b.get("/painel/assinatura").text and not falso.chamadas
    r = b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "2", "modo": "avulso", "cpf_cnpj": CPF_OK})
    assert r.headers["location"].startswith("https://asaas.exemplo.test/i/"), r.headers
    cliente = [c for c in falso.chamadas if c[1] == "/customers"][0][2]
    assert cliente["cpfCnpj"] == "52998224725" and cliente["externalReference"] == "turno:2"
    cobr = [c for c in falso.chamadas if c[1] == "/payments"]
    assert len(cobr) == 2 and "callback" in cobr[0][2] and "callback" not in cobr[1][2], "tenta com retorno, cai para sem"
    assert cobr[1][2]["value"] == 99.9 and cobr[1][2]["billingType"] == "UNDEFINED"
    pid = list(falso.cobrancas)[-1]
    # pedir de novo no mesmo dia reaproveita a cobrança
    n = len(falso.chamadas)
    r2 = b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "2", "modo": "avulso"})
    assert r2.headers["location"] == r.headers["location"] and len(falso.chamadas) == n
    tela = b.get("/painel/assinatura").text
    assert "aguardando pagamento" in tela and "R$ 99,90" in tela
    assert empresa().plano_id == 1 and situacao_de(2) == "teste", "nada muda antes de pagar"

    # --- webhook: token errado, cobrança de outra plataforma, confirmação, repetição
    assert aviso("PAYMENT_CONFIRMED", {"id": pid, "value": 99.9}, token="x" * 40).status_code == 401
    assert aviso("PAYMENT_CONFIRMED", {"id": "pay_do_cmv", "value": 50}, eid="evt_0").status_code == 200
    assert situacao_de(2) == "teste"
    assert aviso("PAYMENT_CONFIRMED", {"id": pid, "value": 99.9, "billingType": "PIX"}, eid="evt_1").status_code == 200
    e = empresa()
    assert situacao_de(2) == "ativa" and e.plano_id == 2, "pago: ativa e no plano escolhido"
    assert perto(e.pago_ate, agora() + timedelta(days=30))
    ate = e.pago_ate
    aviso("PAYMENT_CONFIRMED", {"id": pid, "value": 99.9}, eid="evt_1")   # mesmo aviso de novo
    aviso("PAYMENT_RECEIVED", {"id": pid, "value": 99.9}, eid="evt_2")    # outro aviso, mesma cobrança
    assert empresa().pago_ate == ate, "confirmar duas vezes não soma dias"
    tela = b.get("/painel/assinatura").text
    assert "aguardando pagamento" not in tela and "Pix" in tela and "Ativa" in tela
    assert "R$ 99,90" in m.get("/master").text

    # --- sem webhook: a tela consulta o Asaas e libera
    r = b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "2", "modo": "avulso"})
    pid2 = list(falso.cobrancas)[-1]
    assert pid2 != pid
    falso.cobrancas[pid2]["status"] = "RECEIVED"
    b.get("/painel/assinatura")
    assert perto(empresa().pago_ate, ate + timedelta(days=30)), "soma 30 dias aos já pagos"

    # --- cobrança automática: começa quando acabam os dias já pagos
    ate = empresa().pago_ate
    r = b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "3", "modo": "automatica"})
    assert r.headers["location"].startswith("https://asaas.exemplo.test/i/")
    corpo = falso.assinatura["corpo"]
    assert corpo["cycle"] == "MONTHLY" and corpo["value"] == 199.9
    assert corpo["nextDueDate"] == (ate - timedelta(hours=3)).date().isoformat()
    assert empresa().asaas_assinatura_id == falso.assinatura["id"]
    tela = b.get("/painel/assinatura").text
    assert "Cobrança automática ativa" in tela and "Ir para o pagamento" not in tela
    b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "1", "modo": "avulso"})
    assert "já está ativa" in b.get("/painel/assinatura").text
    assert "Cobrança automática mensal ativa" in m.get("/master/empresas/2").text
    # primeira mensalidade paga: cobre até um mês depois do vencimento e troca o plano
    pid3 = list(falso.cobrancas)[-1]
    venc = falso.cobrancas[pid3]["dueDate"]
    aviso("PAYMENT_RECEIVED", {"id": pid3, "value": 199.9, "subscription": falso.assinatura["id"],
                               "dueDate": venc, "billingType": "CREDIT_CARD"}, eid="evt_3")
    e = empresa()
    assert e.plano_id == 3 and e.pago_ate == asaas.mais_um_mes(venc)
    # mensalidade seguinte chega sem registro prévio
    venc2 = asaas.mais_um_mes(venc).date().isoformat()
    aviso("PAYMENT_OVERDUE", {"id": "pay_mes2", "value": 199.9, "subscription": falso.assinatura["id"],
                              "dueDate": venc2, "invoiceUrl": "https://asaas.exemplo.test/i/mes2"}, eid="evt_4")
    assert "cobrança vencida" in b.get("/painel/assinatura").text
    aviso("PAYMENT_CONFIRMED", {"id": "pay_mes2", "value": 199.9, "subscription": falso.assinatura["id"],
                                "dueDate": venc2}, eid="evt_5")
    assert empresa().pago_ate == asaas.mais_um_mes(venc2)
    assert "cobrança vencida" not in b.get("/painel/assinatura").text
    with SessionLocal() as db:
        pagos = db.scalars(select(Pagamento).where(Pagamento.status == "pago")).all()
        assert len(pagos) == 4 and sum(p.valor_centavos for p in pagos) == 9990 * 2 + 19990 * 2

    # --- encerrar: pela empresa, e pelo aviso do Asaas
    r = b.post("/painel/assinatura/cancelar", data={"csrf": tb})
    assert ("DELETE", f"/subscriptions/{falso.assinatura['id']}", None) in falso.chamadas
    assert empresa().asaas_assinatura_id == "" and situacao_de(2) == "ativa", "acesso continua até a data paga"
    assert "Ir para o pagamento" in b.get("/painel/assinatura").text
    b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "1", "modo": "automatica"})
    sub = empresa().asaas_assinatura_id
    assert sub
    aviso("SUBSCRIPTION_DELETED", ass={"id": sub}, eid="evt_6")
    assert empresa().asaas_assinatura_id == ""
    with SessionLocal() as db:
        assert not db.scalars(select(Pagamento).where(Pagamento.status == "pendente")).all()

    # --- o master encerra a cobrança automática de uma empresa
    b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "1", "modo": "automatica"})
    tm = csrf(m, "/master")
    m.post("/master/empresas/2/cancelar-assinatura", data={"csrf": tm})
    assert empresa().asaas_assinatura_id == ""
    assert "Asaas conectado" in m.get("/master/plataforma").text
    # plano menor que a equipe não pode ser escolhido
    tc = csrf(b, "/painel/config")
    for i in range(11):
        b.post("/painel/equipe", data={"csrf": tc, "nome": f"F {i}", "celular": f"989999901{i:02d}"})
    b.post("/painel/assinatura/pagar", data={"csrf": tb, "plano": "1", "modo": "avulso"})
    assert "permite até 10 funcionários e você tem 11 ativos" in b.get("/painel/assinatura").text
    print("OK: testes do Asaas passaram")


def situacao_de(eid):
    from app.core import situacao
    with SessionLocal() as db:
        return situacao(db.get(Empresa, eid))


if __name__ == "__main__":
    main()
