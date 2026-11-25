"""Direct do Instagram -> CRM (Fase 3).

A Meta avisa cada mensagem nova do direct do @data.worke neste endereço (webhook). Para cada mensagem recebida:
acha ou cria o lead (origem Instagram, sem dono, na fila), guarda o @ de quem mandou e registra o texto no
histórico. O sistema NÃO responde: a resposta automática continua no Meta Business Suite.

Configuração, só em variáveis de ambiente (arquivo .env do servidor, nunca no código):
  META_APP_SECRET    chave secreta do app (Meta for Developers -> Configurações do app -> Básico).
                     Confere a assinatura X-Hub-Signature-256 de cada aviso; sem ela o endereço recusa tudo.
  META_VERIFY_TOKEN  palavra combinada que você inventa e cola também no painel da Meta ao ligar o webhook.
  META_IG_TOKEN      (opcional) token de acesso do Instagram/página, para buscar o @ e o nome de quem escreveu.
                     Sem ele o lead é criado com o número interno do remetente e você completa o @ na ficha.

LGPD: guarda só o @, o nome público, o número interno do remetente e o texto. O lead nasce com
autorizou=False (não autorizou contato fora do Instagram).
"""
import hashlib
import hmac
import json
import logging
import os

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from . import db as banco
from .db import Lead, LeadAtividade, SessionLocal

log = logging.getLogger("instagram")
rota = APIRouter()
TEXTO_MAX = 2000


def _segredo() -> str:
    return os.environ.get("META_APP_SECRET", "").strip()


def _verificacao() -> str:
    return os.environ.get("META_VERIFY_TOKEN", "").strip()


def ligado() -> bool:
    return bool(_segredo() and _verificacao())


def assinatura_ok(corpo: bytes, cabecalho: str) -> bool:
    """Confere o X-Hub-Signature-256 (HMAC-SHA256 do corpo com a chave secreta do app)."""
    segredo = _segredo()
    if not segredo or not cabecalho.startswith("sha256="):
        return False
    esperado = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecalho.removeprefix("sha256="))


@rota.get("/webhooks/instagram")
def verificar(request: Request):
    """A Meta chama uma vez ao ligar o webhook: devolvemos o desafio se a palavra combinada bater."""
    q = request.query_params
    token = _verificacao()
    if token and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    return Response(status_code=403)


@rota.post("/webhooks/instagram")
async def receber(request: Request, tarefas: BackgroundTasks):
    corpo = await request.body()
    if not assinatura_ok(corpo, request.headers.get("x-hub-signature-256", "")):
        return Response(status_code=403)
    try:
        dados = json.loads(corpo or b"{}")
    except ValueError:
        return Response(status_code=400)
    msgs = mensagens(dados)
    if msgs:
        tarefas.add_task(processar, msgs)   # responde já; a Meta reenvia se demorar
    return PlainTextResponse("EVENT_RECEIVED")


def mensagens(dados: dict) -> list[dict]:
    """Só as mensagens recebidas com texto (ignora as enviadas pela própria conta, leituras e reações)."""
    saida = []
    if not isinstance(dados, dict) or dados.get("object") not in ("instagram", "page"):
        return saida
    for ent in dados.get("entry") or []:
        conta = str(ent.get("id", ""))
        for ev in ent.get("messaging") or []:
            msg = ev.get("message") or {}
            remetente = str((ev.get("sender") or {}).get("id", ""))
            if not msg or msg.get("is_echo") or msg.get("is_deleted") or not remetente or remetente == conta:
                continue
            texto = (msg.get("text") or "").strip()
            if not texto and msg.get("attachments"):
                texto = "[enviou um anexo]"
            if texto:
                saida.append({"de": remetente[:40], "mid": str(msg.get("mid", ""))[:120], "texto": texto[:TEXTO_MAX]})
    return saida


def perfil(igsid: str) -> dict:
    """@ e nome público de quem escreveu (precisa do META_IG_TOKEN). Em erro, devolve {}."""
    token = os.environ.get("META_IG_TOKEN", "").strip()
    if not token:
        return {}
    base = "https://graph.instagram.com/v21.0" if token.startswith("IG") else "https://graph.facebook.com/v21.0"
    try:
        r = httpx.get(f"{base}/{igsid}", params={"fields": "username,name", "access_token": token}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {"username": str(d.get("username") or ""), "name": str(d.get("name") or "")}
        log.warning("Instagram: perfil %s não veio (HTTP %s)", igsid, r.status_code)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Instagram: erro ao buscar o perfil: %s", e)
    return {}


def processar(msgs: list[dict]) -> None:
    from . import crm
    agora = banco.agora()
    with SessionLocal() as db:
        for m in msgs:
            if m["mid"] and db.scalar(select(LeadAtividade.id).where(LeadAtividade.ref == m["mid"])):
                continue   # a Meta reenviou o mesmo aviso
            ld = db.scalar(select(Lead).where(Lead.instagram_id == m["de"]))
            novo = ld is None
            if novo:
                p = perfil(m["de"])
                ig = crm.arroba(p.get("username", ""))
                ld = crm._achar(db, instagram=ig) if ig else None
                if ld is None:
                    ld = Lead(origem="instagram", etapa="novo", dono_id=None, autorizou=False,
                              nome=(p.get("name") or (f"@{ig}" if ig else "Instagram (sem @)"))[:80],
                              instagram=ig, criado_em=agora, atualizado_em=agora)
                    db.add(ld)
                else:
                    novo = False
                ld.instagram_id = m["de"]
                db.flush()
                if novo:
                    crm.registrar(db, ld, "Chegou pelo direct do Instagram." if ig else
                                  "Chegou pelo direct do Instagram. Complete o @ na ficha.")
                    crm.retorno_24h(db, ld)
            db.add(LeadAtividade(lead_id=ld.id, tipo="instagram", texto=m["texto"], ref=m["mid"], criado_em=agora))
            ld.atualizado_em = agora
        db.commit()

