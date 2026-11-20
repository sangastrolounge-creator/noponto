"""Avisos para o dono da plataforma: nova empresa, pagamento confirmado/recusado, cancelamento,
resumo diário dos testes grátis e liga/desliga em Master -> Avisos. E-mail simulado.
Rodar da raiz: .venv/Scripts/python tests/test_avisos_master.py"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
TOKEN = "t" * 40
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com", "PUBLIC_URL": "https://app.example.com",
                   "ASAAS_API_KEY": "chave-de-mentira", "ASAAS_WEBHOOK_TOKEN": TOKEN,
                   "SMTP_HOST": "smtp.example.com", "SMTP_USER": "no-reply@app.example.com",
                   "SMTP_PASSWORD": "x"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import avisos_master, email_envio  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Empresa, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 18, 10, 0)}
banco.agora = lambda: relogio["agora"]
emails = []
email_envio._mandar = lambda para, assunto, texto: emails.append((para, assunto, texto))


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def evento(cli, tipo, eid, pay=None, ass=None):
    corpo = {"id": eid, "event": tipo}
    if pay:
        corpo["payment"] = pay
    if ass:
        corpo["subscription"] = ass
    return cli.post("/asaas/webhook", json=corpo, headers={"asaas-access-token": TOKEN})


def do_master():
    return [e for e in emails if e[0] == "dono@plataforma.com"]


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    assert not do_master()          # a conta do próprio dono (cortesia) não gera aviso

    # ---- nova empresa
    c = TestClient(app, follow_redirects=False)
    c.post("/cadastro", data={"csrf": csrf(c, "/cadastro"), "aceito": "1", "empresa": "Taco Bar", "nome": "Gil Gomes",
                              "email": "gil@t.com", "senha": "segredo1"})
    para, assunto, texto = do_master()[-1]
    assert "Nova empresa: Taco Bar" in assunto and "gil@t.com" in texto and "Gil Gomes" in texto
    assert "Teste grátis até: 25/11/2026" in texto
    b = TestClient(app, follow_redirects=False)   # outra empresa, só no teste grátis
    b.post("/cadastro", data={"csrf": csrf(b, "/cadastro"), "aceito": "1", "empresa": "Burger Hut", "nome": "Bia",
                              "email": "bia@b.com", "senha": "segredo1"})
    with SessionLocal() as db:
        eid = db.scalar(select(Empresa.id).where(Empresa.nome == "Taco Bar"))
        e = db.get(Empresa, eid)
        e.asaas_cliente_id, e.asaas_assinatura_id = "cus_1", "sub_1"
        db.commit()

    # ---- pagamento confirmado (uma vez só), cobrança vencida e assinatura cancelada
    pay = {"customer": "cus_1", "subscription": "sub_1", "dueDate": "2026-11-18", "billingType": "PIX"}
    n = len(do_master())
    assert evento(c, "PAYMENT_CONFIRMED", "evt_1", {**pay, "id": "pay_1", "value": 59}).status_code == 200
    assert "Pagamento confirmado: Taco Bar (R$ 59,00)" in do_master()[-1][1]
    evento(c, "PAYMENT_CONFIRMED", "evt_1", {**pay, "id": "pay_1", "value": 59})     # repetido
    evento(c, "PAYMENT_RECEIVED", "evt_1b", {**pay, "id": "pay_1", "value": 59})     # mesma cobrança
    assert len(do_master()) == n + 1
    evento(c, "PAYMENT_OVERDUE", "evt_2", {**pay, "id": "pay_2", "value": 59, "dueDate": "2026-12-18"})
    assert "Cobrança vencida: Taco Bar" in do_master()[-1][1] and "gil@t.com" in do_master()[-1][2]
    evento(c, "SUBSCRIPTION_DELETED", "evt_3", ass={"id": "sub_1"})
    assert "Assinatura cancelada: Taco Bar" in do_master()[-1][1]

    # ---- resumo diário dos testes grátis
    n = len(do_master())
    assert avisos_master.resumo_testes() == 0 and len(do_master()) == n    # antes das 9h de Brasília
    relogio["agora"] = datetime(2026, 11, 24, 12, 30)
    assert avisos_master.resumo_testes() == 1
    resumo = do_master()[-1]
    assert "1 teste(s) grátis" in resumo[1] and "Burger Hut" in resumo[2] and "bia@b.com" in resumo[2]
    assert "Taco Bar" not in resumo[2]          # já tem dias pagos: não entra
    assert avisos_master.resumo_testes() == 0                                # uma vez por dia

    # ---- Master -> Avisos: desligar um tipo
    tm = csrf(m, "/master/avisos")
    assert "Me avise quando" in m.get("/master/avisos").text
    m.post("/master/avisos", data={"csrf": tm, "t_pago": "1", "t_recusado": "1", "t_cancelado": "1",
                                   "t_teste_fim": "1"})          # cadastro desligado
    n = len(do_master())
    d = TestClient(app, follow_redirects=False)
    d.post("/cadastro", data={"csrf": csrf(d, "/cadastro"), "aceito": "1", "empresa": "Diner", "nome": "Dora",
                              "email": "dora@d.com", "senha": "segredo1"})
    assert len(do_master()) == n
    m.post("/master/avisos", data={"csrf": tm, **{f"t_{t}": "1" for t in avisos_master.TIPOS}})
    m.post("/master/avisos", data={"csrf": tm, "acao": "testar"})
    assert "Teste de aviso" in do_master()[-1][1]
    assert c.get("/master/avisos").status_code == 303     # só o dono da plataforma
    print("OK: avisos do Master passou")


if __name__ == "__main__":
    main()
