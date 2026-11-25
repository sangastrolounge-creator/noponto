"""Fase 3: direct do Instagram vira lead no CRM (webhook da Meta). Rodar da raiz: python tests/test_instagram_direct.py"""
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
                   "META_APP_SECRET": "segredo-do-app-de-teste", "META_VERIFY_TOKEN": "palavra-combinada"})
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("META_IG_TOKEN", None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import instagram  # noqa: E402
from app.db import Lead, LeadAtividade, LeadTarefa, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

CONTA = "17841400000000001"   # o @data.worke
consultas = []


def falso_perfil(igsid):
    consultas.append(igsid)
    return {"6001": {"username": "Bar.Do.Ze", "name": "Zé do Bar"}}.get(igsid, {})


instagram.perfil = falso_perfil


def aviso(remetente, texto, mid, eco=False):
    msg = {"mid": mid, "text": texto}
    if eco:
        msg["is_echo"] = True
    return {"object": "instagram", "entry": [{"id": CONTA, "time": 1, "messaging": [
        {"sender": {"id": remetente}, "recipient": {"id": CONTA}, "timestamp": 1, "message": msg}]}]}


def postar(cli, dados, segredo="segredo-do-app-de-teste"):
    corpo = json.dumps(dados).encode()
    sig = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return cli.post("/webhooks/instagram", content=corpo,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    meta = TestClient(app, follow_redirects=False)

    # verificação do webhook: devolve o desafio só com a palavra combinada
    ok = meta.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "palavra-combinada",
                                                 "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    assert meta.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "errada",
                                                   "hub.challenge": "x"}).status_code == 403

    # assinatura errada ou ausente: recusa e não cria nada
    assert postar(meta, aviso("6001", "oi", "m0"), segredo="outro").status_code == 403
    assert meta.post("/webhooks/instagram", json=aviso("6001", "oi", "m0")).status_code == 403
    with SessionLocal() as db:
        assert not db.scalars(select(Lead)).all()

    # pessoa comenta ESCALA, recebe o direct do Business Suite (eco: ignorado) e responde
    assert postar(meta, aviso("6001", "Oi! Vi seu comentário ESCALA", "m1", eco=True)).status_code == 200
    r = postar(meta, aviso("6001", "tenho 12 funcionários", "m2"))
    assert r.status_code == 200 and r.text == "EVENT_RECEIVED"
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.instagram_id == "6001"))
        assert ld and ld.origem == "instagram" and ld.instagram == "bar.do.ze" and ld.nome == "Zé do Bar"
        assert ld.dono_id is None and ld.etapa == "novo" and not ld.autorizou
        textos = [a.texto for a in db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == ld.id))]
        assert "tenho 12 funcionários" in textos and not any("ESCALA" in t for t in textos)
        tarefa = db.scalar(select(LeadTarefa).where(LeadTarefa.lead_id == ld.id))
        assert tarefa and tarefa.dono_id is None and tarefa.quando > ld.criado_em
        lid = ld.id

    # a Meta reenvia o mesmo aviso: não duplica; nova mensagem entra no mesmo lead sem nova consulta do perfil
    postar(meta, aviso("6001", "tenho 12 funcionários", "m2"))
    postar(meta, aviso("6001", "quanto custa?", "m3"))
    with SessionLocal() as db:
        assert len(db.scalars(select(Lead)).all()) == 1
        textos = [a.texto for a in db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == lid,
                                                                          LeadAtividade.tipo == "instagram"))]
        assert textos == ["tenho 12 funcionários", "quanto custa?"], textos
    assert consultas == ["6001"]

    # lead já cadastrado à mão com o mesmo @: a mensagem vai para ele
    tok = csrf(m, "/master/crm")
    m.post("/master/crm/novo", data={"csrf": tok, "nome": "Ana", "instagram": "@ana.cafe", "origem": "instagram"})
    instagram.perfil = lambda igsid: {"username": "ana.cafe", "name": "Ana"}
    postar(meta, aviso("6002", "quero testar", "m4"))
    with SessionLocal() as db:
        ana = db.scalar(select(Lead).where(Lead.instagram == "ana.cafe"))
        assert ana.instagram_id == "6002" and len(db.scalars(select(Lead)).all()) == 2

    # sem o @ (sem token): o lead nasce mesmo assim, pedindo para completar na ficha
    instagram.perfil = lambda igsid: {}
    postar(meta, aviso("6003", "bom dia", "m5"))
    with SessionLocal() as db:
        x = db.scalar(select(Lead).where(Lead.instagram_id == "6003"))
        assert x and x.instagram == "" and x.nome == "Instagram (sem @)"

    # aparece no CRM, na fila e filtrado por Instagram; a ficha mostra a mensagem
    tela = m.get("/master/crm?ver=fila&origem=instagram").text
    assert "Zé do Bar" in tela
    ficha = m.get(f"/master/crm/{lid}").text
    assert "quanto custa?" in ficha and "Direct do Instagram" in ficha
    # Master -> Rastreamento mostra que está ligado e o endereço do webhook
    r = m.get("/master/rastreamento").text
    assert "Ligado" in r and "/webhooks/instagram" in r
    print("ok: direct do Instagram no CRM")


if __name__ == "__main__":
    main()
