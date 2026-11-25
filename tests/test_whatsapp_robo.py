"""Robô de vendas no WhatsApp. Rodar da raiz: python tests/test_whatsapp_robo.py"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com",
                   "META_APP_SECRET": "segredo-do-app", "META_VERIFY_TOKEN": "palavra",
                   "WA_TOKEN": "token-de-teste", "WA_PHONE_ID": "1234567890"})
os.environ.pop("ANTHROPIC_API_KEY", None)

roteiro = []      # respostas que o "Claude" vai dar, em ordem
pedidos = []


class _Msgs:
    def create(self, **kw):
        pedidos.append(kw)
        r = roteiro.pop(0) if roteiro else "Oi!"
        if r == "ERRO":
            raise RuntimeError("fora do ar")
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=r)])


sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: types.SimpleNamespace(messages=_Msgs()))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import revisor, whatsapp  # noqa: E402
from app.db import Config, Lead, LeadTarefa, SessionLocal, WhatsMensagem  # noqa: E402
from app.main import app  # noqa: E402

enviados = []
whatsapp.enviar_api = lambda para, corpo: (enviados.append((para, corpo)) or (f"wamid.R{len(enviados)}", ""))


def recebida(wa, nome, texto, mid):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "1234567890"}, "contacts": [{"wa_id": wa, "profile": {"name": nome}}],
        "messages": [{"from": wa, "id": mid, "type": "text", "text": {"body": texto}}]}}]}]}


def postar(cli, dados):
    corpo = json.dumps(dados).encode()
    sig = "sha256=" + hmac.new(b"segredo-do-app", corpo, hashlib.sha256).hexdigest()
    return cli.post("/webhooks/whatsapp", content=corpo,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    meta = TestClient(app, follow_redirects=False)

    # sem a chave do Claude o botão de ligar fica bloqueado
    assert "Falta a chave do Claude" in m.get("/master/rastreamento").text
    with SessionLocal() as db:
        db.merge(Config(chave=revisor.CHAVE_CONFIG, valor="sk-teste"))
        db.commit()

    # desligado: chega a mensagem, ninguém responde
    postar(meta, recebida("5598999990002", "Bia", "Oi, quanto custa?", "a1"))
    assert enviados == [] and pedidos == []

    # liga o robô
    t = m.get("/master/rastreamento").text
    assert "Robô desligado" in t and "Ligar o robô do WhatsApp" in t
    m.post("/master/rastreamento/whatsapp-robo", data={"csrf": csrf(m, "/master/rastreamento"), "ligar": "1"})
    assert "Robô ligado" in m.get("/master/rastreamento").text

    # responde com o link do teste e guarda empresa e funcionários na ficha
    roteiro.append("Que bom! Para 12 pessoas o plano certo é o Essencial. [TESTE]\n"
                   "#dados empresa=Padaria Sol; funcionarios=12")
    postar(meta, recebida("5598999990002", "Bia", "Tenho uma padaria com 12 funcionários", "a2"))
    assert len(enviados) == 1
    corpo = enviados[0][1]["text"]["body"]
    assert "Essencial" in corpo and "/cadastro?utm_source=whatsapp" in corpo
    assert "#dados" not in corpo and "[TESTE]" not in corpo
    assert pedidos[-1]["messages"][-1]["role"] == "user" and "WHATSAPP" in pedidos[-1]["system"]
    assert "12 funcionários" in pedidos[-1]["messages"][-1]["content"]
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.telefone == "98999990002"))
        assert ld.empresa_nome == "Padaria Sol" and ld.funcionarios == 12 and not ld.robo_pausado
        assert db.scalar(select(WhatsMensagem).where(WhatsMensagem.mid == "wamid.R1")).robo
        lid = ld.id
    d = m.get(f"/master/crm/{lid}/whatsapp.json").json()
    assert d["robo"] and not d["robo_pausado"] and d["mensagens"][-1]["quem"] == "robô"

    # a pessoa pede um humano: o robô avisa, para e deixa tarefa
    roteiro.append("Claro! Vou chamar alguém do time agora. [HUMANO]")
    postar(meta, recebida("5598999990002", "Bia", "Quero falar com uma pessoa", "a3"))
    assert len(enviados) == 2 and "[HUMANO]" not in enviados[1][1]["text"]["body"]
    with SessionLocal() as db:
        assert db.get(Lead, lid).robo_pausado
        assert db.scalar(select(LeadTarefa).where(LeadTarefa.titulo == "Atender no WhatsApp (pediu uma pessoa)"))
    postar(meta, recebida("5598999990002", "Bia", "Alô?", "a4"))
    assert len(enviados) == 2                                    # pausado: fica quieto

    # devolve para o robô pela ficha: volta a responder
    tok = csrf(m, f"/master/crm/{lid}")
    r = m.post(f"/master/crm/{lid}/whatsapp/robo", json={"ligar": True}, headers={"X-CSRF": tok})
    assert r.json()["robo_pausado"] is False
    roteiro.append("Estou aqui! Em que posso ajudar?")
    postar(meta, recebida("5598999990002", "Bia", "Oi de novo", "a5"))
    assert len(enviados) == 3

    # alguém do time responde pelo CRM: o robô para nesta conversa
    m.post(f"/master/crm/{lid}/whatsapp", json={"texto": "Oi Bia, aqui é o Dono."}, headers={"X-CSRF": tok})
    assert len(enviados) == 4
    postar(meta, recebida("5598999990002", "Bia", "Oi Dono!", "a6"))
    assert len(enviados) == 4
    with SessionLocal() as db:
        assert db.get(Lead, lid).robo_pausado

    # Claude fora do ar: não responde e não derruba o recebimento
    roteiro.append("ERRO")
    assert postar(meta, recebida("5598999990009", "Rui", "Bom dia", "b1")).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(Lead).where(Lead.telefone == "98999990009"))
    assert len(enviados) == 4

    # desliga o robô
    m.post("/master/rastreamento/whatsapp-robo", data={"csrf": csrf(m, "/master/rastreamento"), "ligar": ""})
    assert "Robô desligado" in m.get("/master/rastreamento").text
    print("ok: robô do WhatsApp")


if __name__ == "__main__":
    main()
