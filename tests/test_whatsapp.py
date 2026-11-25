"""WhatsApp dentro do CRM (Cloud API da Meta). Rodar da raiz: python tests/test_whatsapp.py"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import types
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com",
                   "META_APP_SECRET": "segredo-do-app", "META_VERIFY_TOKEN": "palavra-combinada",
                   "WA_TOKEN": "token-de-teste", "WA_PHONE_ID": "1234567890", "WA_MODELO": "retomar_contato"})
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import whatsapp  # noqa: E402
from app.db import Lead, LeadTarefa, SessionLocal, WhatsMensagem  # noqa: E402
from app.main import app  # noqa: E402

enviados = []
resposta = {"erro": ""}


def falso_envio(para, corpo):
    enviados.append((para, corpo))
    if resposta["erro"]:
        return "", resposta["erro"]
    return f"wamid.OUT{len(enviados)}", ""


whatsapp.enviar_api = falso_envio


def valor(**v):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [
        {"field": "messages", "value": {"messaging_product": "whatsapp",
                                        "metadata": {"phone_number_id": "1234567890"}, **v}}]}]}


def recebida(wa, nome, texto, mid):
    return valor(contacts=[{"wa_id": wa, "profile": {"name": nome}}],
                 messages=[{"from": wa, "id": mid, "timestamp": "1", "type": "text", "text": {"body": texto}}])


def postar(cli, dados, segredo="segredo-do-app"):
    corpo = json.dumps(dados).encode()
    sig = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return cli.post("/webhooks/whatsapp", content=corpo,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    # número do WhatsApp -> telefone do CRM (inclusive celular antigo sem o 9)
    assert whatsapp.telefone_do_wa("5598999990002") == "98999990002"
    assert whatsapp.telefone_do_wa("559899990002") == "98999990002"

    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    meta = TestClient(app, follow_redirects=False)

    # verificação e assinatura
    ok = meta.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "palavra-combinada",
                                                "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert postar(meta, recebida("5598999990002", "Bia", "oi", "w0"), segredo="outro").status_code == 403

    # pessoa nova escreve: vira lead WhatsApp na fila, com retorno em 24h
    assert postar(meta, recebida("559899990002", "Bia Lima", "Quero saber o preço", "w1")).text == "EVENT_RECEIVED"
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.telefone == "98999990002"))
        assert ld and ld.origem == "whatsapp" and ld.nome == "Bia Lima" and ld.dono_id is None and ld.autorizou
        assert db.scalar(select(LeadTarefa).where(LeadTarefa.lead_id == ld.id)).titulo == "Responder o lead do WhatsApp"
        bia = ld.id

    # lead que já existia com o telefone recebe a mensagem (sem criar outro)
    tok = csrf(m, "/master/crm")
    m.post("/master/crm/novo", data={"csrf": tok, "nome": "Rui Manual", "telefone": "98999990007", "origem": "manual"})
    postar(meta, recebida("5598999990007", "Rui", "Bom dia", "w2"))
    postar(meta, recebida("5598999990007", "Rui", "Bom dia", "w2"))      # reenvio da Meta: ignora
    with SessionLocal() as db:
        rui = db.scalar(select(Lead).where(Lead.telefone == "98999990007"))
        assert len(db.scalars(select(Lead).where(Lead.telefone == "98999990007")).all()) == 1
        assert len(db.scalars(select(WhatsMensagem).where(WhatsMensagem.lead_id == rui.id)).all()) == 1
        rui = rui.id

    # funil marca "WhatsApp novo"; abrir a conversa marca como vista
    assert m.get("/master/crm?ver=todos").text.count("WhatsApp novo") == 2
    ficha = m.get(f"/master/crm/{bia}").text
    assert "Conversa no WhatsApp" in ficha and "/whatsapp" in ficha
    d = m.get(f"/master/crm/{bia}/whatsapp.json").json()
    assert d["envia"] and d["janela"] and d["mensagens"][0]["texto"] == "Quero saber o preço"
    assert m.get("/master/crm?ver=todos").text.count("WhatsApp novo") == 1

    # responder pela ficha: vai para o número que a pessoa usou; quem respondeu vira o responsável
    tok = csrf(m, f"/master/crm/{bia}")
    assert m.post(f"/master/crm/{bia}/whatsapp", json={"texto": "Oi"}).status_code == 403      # sem CSRF
    r = m.post(f"/master/crm/{bia}/whatsapp", json={"texto": "Oi Bia! O plano começa em R$ 49."},
               headers={"X-CSRF": tok})
    assert r.status_code == 200, r.text
    assert enviados[-1][0] == "559899990002" and enviados[-1][1]["text"]["body"].startswith("Oi Bia")
    with SessionLocal() as db:
        ld = db.get(Lead, bia)
        assert ld.dono_id and db.scalar(select(LeadTarefa).where(LeadTarefa.lead_id == bia)).dono_id == ld.dono_id

    # status da Meta: entregue e lida
    postar(meta, valor(statuses=[{"id": "wamid.OUT1", "status": "delivered"}]))
    postar(meta, valor(statuses=[{"id": "wamid.OUT1", "status": "read"}]))
    postar(meta, valor(statuses=[{"id": "wamid.OUT1", "status": "delivered"}]))   # fora de ordem: não volta
    with SessionLocal() as db:
        assert db.scalar(select(WhatsMensagem).where(WhatsMensagem.mid == "wamid.OUT1")).status == "lida"

    # resposta dada no app do celular (coexistência) também aparece na conversa
    postar(meta, valor(message_echoes=[{"from": "5598981004999", "to": "559899990002", "id": "eco1",
                                        "type": "text", "text": {"body": "Mandei o link pelo celular"}}]))
    d = m.get(f"/master/crm/{bia}/whatsapp.json").json()
    assert d["mensagens"][-1]["texto"] == "Mandei o link pelo celular" and d["mensagens"][-1]["quem"] == "celular"

    postar(meta, valor(message_echoes=[{"from": "5598981004999", "to": "5511988887777", "id": "eco2",
                                        "type": "text", "text": {"body": "Oi fornecedor"}}]))
    with SessionLocal() as db:
        assert not db.scalar(select(Lead).where(Lead.telefone == "11988887777"))      # não vira lead

    # erro da Meta: avisa e não grava
    resposta["erro"] = "Número inválido"
    r = m.post(f"/master/crm/{bia}/whatsapp", json={"texto": "teste"}, headers={"X-CSRF": tok})
    assert r.status_code == 502 and r.json()["erro"] == "Número inválido"
    resposta["erro"] = ""

    # depois de 24h: texto livre bloqueado; só o modelo
    with SessionLocal() as db:
        for x in db.scalars(select(WhatsMensagem).where(WhatsMensagem.lead_id == bia, WhatsMensagem.entrada.is_(True))):
            x.criado_em -= timedelta(hours=25)
        db.commit()
    d = m.get(f"/master/crm/{bia}/whatsapp.json").json()
    assert not d["janela"] and d["modelo"]
    r = m.post(f"/master/crm/{bia}/whatsapp", json={"texto": "ainda aí?"}, headers={"X-CSRF": tok})
    assert r.status_code == 400 and "24 horas" in r.json()["erro"]
    r = m.post(f"/master/crm/{bia}/whatsapp", json={"modelo": True}, headers={"X-CSRF": tok})
    assert r.status_code == 200 and enviados[-1][1]["template"]["name"] == "retomar_contato"
    assert enviados[-1][1]["template"]["language"]["code"] == "pt_BR"

    # Master -> Rastreamento mostra o estado
    t = m.get("/master/rastreamento").text
    assert "Recebendo" in t and "Enviando" in t and "/webhooks/whatsapp" in t and "retomar_contato" in t
    print("ok: WhatsApp no CRM")


if __name__ == "__main__":
    main()
