"""WhatsApp dentro do CRM (API oficial da Meta, WhatsApp Cloud API).

- A Meta avisa cada mensagem recebida no número da plataforma em /webhooks/whatsapp. A mensagem vai para o lead
  com aquele telefone (ou cria um lead novo, origem WhatsApp, na fila) e aparece na ficha, na conversa.
- A equipe responde pela ficha do lead. Respostas livres só dentro de 24 horas da última mensagem da pessoa
  (regra da Meta); depois disso, só a mensagem modelo aprovada (WA_MODELO), que é cobrada pela Meta.
- Com a "coexistência" o mesmo número continua no app WhatsApp Business do celular: o que for respondido pelo
  celular também chega aqui (campo smb_message_echoes) e aparece na conversa.

Configuração, só em variáveis de ambiente (.env do servidor):
  WA_TOKEN          token permanente do usuário do sistema (Gerenciador de Negócios -> Usuários do sistema)
  WA_PHONE_ID       identificação do número de telefone (WhatsApp -> Configuração da API)
  WA_APP_SECRET     chave secreta do app da Meta (Configurações do app -> Básico). Vazio = usa META_APP_SECRET
  WA_VERIFY_TOKEN   palavra combinada do webhook. Vazio = usa META_VERIFY_TOKEN
  WA_MODELO         (opcional) nome da mensagem modelo aprovada para retomar conversa, sem variáveis
  WA_MODELO_IDIOMA  (opcional) idioma do modelo, padrão pt_BR
"""
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import db as banco
from .core import admin_logado, exigir_csrf
from .db import Lead, SessionLocal, Usuario, WhatsMensagem, get_db

log = logging.getLogger("whatsapp")
rota = APIRouter()
API = "https://graph.facebook.com/v21.0"
JANELA = timedelta(hours=24)
TEXTO_MAX = 4096
STATUS = {"sent": "enviada", "delivered": "entregue", "read": "lida", "failed": "falhou"}
TIPOS = {"image": "[imagem]", "audio": "[áudio]", "video": "[vídeo]", "document": "[documento]",
         "sticker": "[figurinha]", "location": "[localização]", "contacts": "[contato]"}


def _env(nome: str, reserva: str = "") -> str:
    return os.environ.get(nome, "").strip() or (os.environ.get(reserva, "").strip() if reserva else "")


def recebe() -> bool:
    return bool(_env("WA_APP_SECRET", "META_APP_SECRET") and _env("WA_VERIFY_TOKEN", "META_VERIFY_TOKEN"))


def envia() -> bool:
    return bool(_env("WA_TOKEN") and _env("WA_PHONE_ID"))


def modelo() -> str:
    return _env("WA_MODELO")


def telefone_do_wa(wa_id: str) -> str:
    """Número do WhatsApp (55 + DDD + número) no formato do CRM: DDD + 9 dígitos, sem o 55.
    Alguns celulares antigos vêm do WhatsApp sem o 9 da frente: devolve com o 9."""
    d = re.sub(r"\D", "", wa_id or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) == 10 and d[2] in "6789":
        d = d[:2] + "9" + d[2:]
    return d


def assinatura_ok(corpo: bytes, cabecalho: str) -> bool:
    segredo = _env("WA_APP_SECRET", "META_APP_SECRET")
    if not segredo or not cabecalho.startswith("sha256="):
        return False
    esperado = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecalho.removeprefix("sha256="))


# ------------------------------------------------------------------ webhook
@rota.get("/webhooks/whatsapp")
def verificar(request: Request):
    q = request.query_params
    token = _env("WA_VERIFY_TOKEN", "META_VERIFY_TOKEN")
    if token and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    return Response(status_code=403)


@rota.post("/webhooks/whatsapp")
async def receber(request: Request, tarefas: BackgroundTasks):
    corpo = await request.body()
    if not assinatura_ok(corpo, request.headers.get("x-hub-signature-256", "")):
        return Response(status_code=403)
    try:
        dados = json.loads(corpo or b"{}")
    except ValueError:
        return Response(status_code=400)
    eventos = ler_aviso(dados)
    if any(eventos.values()):
        tarefas.add_task(processar, eventos)
    return PlainTextResponse("EVENT_RECEIVED")


def _texto(m: dict) -> str:
    tipo = m.get("type", "")
    if tipo == "text":
        return ((m.get("text") or {}).get("body") or "").strip()[:TEXTO_MAX]
    if tipo in ("button", "interactive"):
        b = m.get("button") or {}
        i = m.get("interactive") or {}
        return (b.get("text") or (i.get("button_reply") or {}).get("title")
                or (i.get("list_reply") or {}).get("title") or "[resposta]")[:TEXTO_MAX]
    legenda = ((m.get(tipo) or {}).get("caption") or "").strip() if isinstance(m.get(tipo), dict) else ""
    return (TIPOS.get(tipo, "[mensagem]") + (f" {legenda}" if legenda else ""))[:TEXTO_MAX]


def ler_aviso(dados: dict) -> dict:
    """Separa o aviso da Meta em: recebidas, enviadas pelo celular (eco) e mudanças de status."""
    out = {"recebidas": [], "ecos": [], "status": []}
    if not isinstance(dados, dict) or dados.get("object") != "whatsapp_business_account":
        return out
    meu = _env("WA_PHONE_ID")
    for ent in dados.get("entry") or []:
        for ch in ent.get("changes") or []:
            v = ch.get("value") or {}
            if meu and str((v.get("metadata") or {}).get("phone_number_id", "")) not in ("", meu):
                continue
            nomes = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in v.get("contacts") or []}
            for m in v.get("messages") or []:
                de = str(m.get("from", ""))
                if de:
                    out["recebidas"].append({"wa": de, "nome": str(nomes.get(de, ""))[:80], "mid": str(m.get("id", "")),
                                             "texto": _texto(m)})
            for m in v.get("message_echoes") or []:
                para = str(m.get("to", ""))
                if para:
                    out["ecos"].append({"wa": para, "mid": str(m.get("id", "")), "texto": _texto(m)})
            for s in v.get("statuses") or []:
                erro = ((s.get("errors") or [{}])[0].get("title") or "") if s.get("status") == "failed" else ""
                out["status"].append({"mid": str(s.get("id", "")), "status": STATUS.get(s.get("status"), ""),
                                      "erro": str(erro)[:200]})
    return out


def _lead_do_numero(db: Session, wa: str, nome: str = "", criar: bool = True) -> tuple[Lead | None, bool]:
    from . import crm
    tel = telefone_do_wa(wa)
    ld = db.scalar(select(Lead).join(WhatsMensagem, WhatsMensagem.lead_id == Lead.id)
                   .where(WhatsMensagem.wa_id == wa).order_by(WhatsMensagem.id.desc()).limit(1))
    ld = ld or crm._achar(db, tel)
    if ld or not criar:
        return ld, False
    agora = banco.agora()
    ld = Lead(origem="whatsapp", etapa="novo", dono_id=None, autorizou=True, nome=(nome or tel)[:80],
              telefone=tel, criado_em=agora, atualizado_em=agora)
    db.add(ld)
    db.flush()
    crm.registrar(db, ld, "Chegou pelo WhatsApp da plataforma.")
    crm.retorno_24h(db, ld, "WhatsApp")
    return ld, True


def processar(eventos: dict) -> None:
    agora = banco.agora()
    novos: list[int] = []
    with SessionLocal() as db:
        for m in eventos["recebidas"] + eventos["ecos"]:
            if m["mid"] and db.scalar(select(WhatsMensagem.id).where(WhatsMensagem.mid == m["mid"])):
                continue   # reenvio da Meta, ou eco de uma resposta dada pelo próprio CRM
            entrada = "nome" in m
            # o que a equipe manda pelo celular só entra se a pessoa já for lead (fornecedor, amigo etc. ficam fora)
            ld, _ = _lead_do_numero(db, m["wa"], m.get("nome", ""), criar=entrada)
            if ld is None:
                continue
            db.add(WhatsMensagem(lead_id=ld.id, wa_id=m["wa"], entrada=entrada, texto=m["texto"], mid=m["mid"],
                                 status="" if entrada else "enviada", pelo_celular=not entrada, criado_em=agora))
            ld.atualizado_em = agora
            if entrada and not ld.nome and m.get("nome"):
                ld.nome = m["nome"]
            if entrada and ld.id not in novos:
                novos.append(ld.id)
            if not entrada:
                from .whatsapp_robo import pausar
                pausar(db, ld, "alguém do time respondeu pelo celular")
        db.flush()
        for s in eventos["status"]:
            if s["mid"] and s["status"]:
                x = db.scalar(select(WhatsMensagem).where(WhatsMensagem.mid == s["mid"]))
                if x and not x.entrada:
                    ordem = ["", "enviada", "entregue", "lida"]
                    if s["status"] == "falhou" or ordem.index(s["status"]) > ordem.index(x.status if x.status in ordem else ""):
                        x.status, x.erro = s["status"], s["erro"] or x.erro
        db.commit()
    from . import whatsapp_robo
    for lid in novos:
        try:
            whatsapp_robo.responder(lid)
        except Exception as e:   # o robô nunca derruba o recebimento
            log.warning("Robô do WhatsApp falhou no lead %s: %s", lid, e)


# ------------------------------------------------------------------ envio
def enviar_api(para: str, corpo: dict) -> tuple[str, str]:
    """Manda pela Cloud API. Devolve (id da mensagem, erro)."""
    try:
        r = httpx.post(f"{API}/{_env('WA_PHONE_ID')}/messages", timeout=15,
                       headers={"Authorization": f"Bearer {_env('WA_TOKEN')}"},
                       json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": para, **corpo})
        d = r.json() if r.content else {}
        if r.status_code == 200 and d.get("messages"):
            return str(d["messages"][0].get("id", "")), ""
        erro = (d.get("error") or {})
        log.warning("WhatsApp: envio recusado (%s): %s", r.status_code, erro)
        return "", str(erro.get("error_user_msg") or erro.get("message") or f"HTTP {r.status_code}")[:200]
    except (httpx.HTTPError, ValueError) as e:
        log.warning("WhatsApp: erro no envio: %s", e)
        return "", "Sem conexão com o WhatsApp. Tente de novo."


def janela_aberta(db: Session, ld: Lead) -> bool:
    ultima = db.scalar(select(WhatsMensagem.criado_em).where(WhatsMensagem.lead_id == ld.id,
                                                             WhatsMensagem.entrada.is_(True))
                       .order_by(WhatsMensagem.criado_em.desc()).limit(1))
    return bool(ultima and banco.agora() - ultima < JANELA)


def _numero(db: Session, ld: Lead) -> str:
    wa = db.scalar(select(WhatsMensagem.wa_id).where(WhatsMensagem.lead_id == ld.id, WhatsMensagem.entrada.is_(True))
                   .order_by(WhatsMensagem.id.desc()).limit(1))
    return wa or (f"55{ld.telefone}" if ld.telefone else "")


def _lead(db: Session, u: Usuario, lid: int) -> Lead | None:
    from . import crm
    return crm._lead(db, u, lid)


@rota.get("/master/crm/{lid}/whatsapp.json")
def conversa(lid: int, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    ld = _lead(db, u, lid)
    if not ld:
        return JSONResponse({"erro": "Lead não encontrado."}, status_code=404)
    msgs = list(db.scalars(select(WhatsMensagem).where(WhatsMensagem.lead_id == ld.id)
                           .order_by(WhatsMensagem.criado_em, WhatsMensagem.id).limit(500)))
    db.execute(update(WhatsMensagem).where(WhatsMensagem.lead_id == ld.id, WhatsMensagem.lida.is_(False),
                                           WhatsMensagem.entrada.is_(True)).values(lida=True))
    db.commit()
    nomes = {x.id: x.nome for x in db.scalars(select(Usuario).where(
        Usuario.id.in_({m.autor_id for m in msgs if m.autor_id} or {0})))}
    from . import whatsapp_robo
    return {"envia": envia(), "janela": janela_aberta(db, ld), "modelo": bool(modelo()),
            "robo": whatsapp_robo.ligado(db), "robo_pausado": ld.robo_pausado,
            "mensagens": [{"id": m.id, "entrada": m.entrada, "texto": m.texto, "status": m.status, "erro": m.erro,
                           "quem": "celular" if m.pelo_celular else "robô" if m.robo else nomes.get(m.autor_id, ""),
                           "hora": m.criado_em.strftime("%d/%m %H:%M")} for m in msgs]}


@rota.post("/master/crm/{lid}/whatsapp", dependencies=[Depends(exigir_csrf)])
async def responder(lid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    from . import crm
    ld = _lead(db, u, lid)
    if not ld:
        return JSONResponse({"erro": "Lead não encontrado."}, status_code=404)
    if not envia():
        return JSONResponse({"erro": "O WhatsApp da plataforma ainda não está ligado."}, status_code=400)
    dados = await request.json()
    para = _numero(db, ld)
    if not para:
        return JSONResponse({"erro": "Este lead não tem WhatsApp."}, status_code=400)
    if dados.get("modelo"):
        if not modelo():
            return JSONResponse({"erro": "Nenhuma mensagem modelo configurada."}, status_code=400)
        texto = f"[modelo: {modelo()}]"
        corpo = {"type": "template", "template": {"name": modelo(),
                                                  "language": {"code": _env("WA_MODELO_IDIOMA") or "pt_BR"}}}
    else:
        texto = str(dados.get("texto", "")).strip()[:TEXTO_MAX]
        if not texto:
            return JSONResponse({"erro": "Escreva a mensagem."}, status_code=400)
        if not janela_aberta(db, ld):
            return JSONResponse({"erro": "Passaram 24 horas da última mensagem da pessoa. Pela regra do WhatsApp, "
                                         "só dá para mandar a mensagem modelo."}, status_code=400)
        corpo = {"type": "text", "text": {"body": texto, "preview_url": False}}
    mid, erro = enviar_api(para, corpo)
    if erro:
        return JSONResponse({"erro": erro}, status_code=502)
    db.add(WhatsMensagem(lead_id=ld.id, wa_id=para, entrada=False, texto=texto, mid=mid, status="enviada",
                         autor_id=u.id, lida=True, criado_em=banco.agora()))
    if ld.dono_id is None:
        ld.dono_id = u.id
        crm.tarefas_sem_dono_para(db, ld)
    from .whatsapp_robo import pausar
    pausar(db, ld, f"{u.nome} assumiu a conversa")
    ld.atualizado_em = banco.agora()
    db.commit()
    return {"ok": True}


@rota.post("/master/crm/{lid}/whatsapp/robo", dependencies=[Depends(exigir_csrf)])
async def robo_do_lead(lid: int, request: Request, u: Usuario = Depends(admin_logado),
                       db: Session = Depends(get_db)):
    """Pausa ou devolve a conversa para o robô."""
    from . import crm
    ld = _lead(db, u, lid)
    if not ld:
        return JSONResponse({"erro": "Lead não encontrado."}, status_code=404)
    ligar = bool((await request.json()).get("ligar"))
    if ligar == ld.robo_pausado:
        ld.robo_pausado = not ligar
        crm.registrar(db, ld, f"{u.nome} {'devolveu a conversa para o robô' if ligar else 'pausou o robô'}.",
                      "sistema", u)
        db.commit()
    return {"ok": True, "robo_pausado": ld.robo_pausado}


def nao_lidas(db: Session, ids: list[int]) -> set[int]:
    """Leads (entre ids) com mensagem do WhatsApp ainda não vista pela equipe."""
    if not ids:
        return set()
    return set(db.scalars(select(WhatsMensagem.lead_id).where(WhatsMensagem.lead_id.in_(ids),
                                                              WhatsMensagem.entrada.is_(True),
                                                              WhatsMensagem.lida.is_(False)).distinct()))
