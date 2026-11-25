"""Robô de vendas no WhatsApp: o mesmo assistente do chat do site (vendedor.py), respondendo no número
oficial da plataforma enquanto ninguém do time assumiu a conversa.

Regras:
- Liga e desliga em Master -> Rastreamento (Config "wa_robo"). Começa desligado.
- Responde só a última mensagem recebida (se a pessoa mandar várias seguidas, responde uma vez).
- Para sozinho naquele lead quando alguém do time responde (pelo CRM ou pelo celular), quando a pessoa pede
  para falar com uma pessoa ([HUMANO]) ou depois de MAX_RESPOSTAS no dia. Na ficha há o botão para religar.
- O que a pessoa conta (empresa, funcionários) vai para a ficha do lead.
"""
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import db as banco
from .db import Config, Lead, SessionLocal, WhatsMensagem

log = logging.getLogger("whatsapp_robo")
CHAVE = "wa_robo"
MAX_TURNOS = 16
MAX_RESPOSTAS = 25          # respostas do robô por lead em 24h; depois passa para o time
SITE = "https://dataworke.com.br"

EXTRA = """

ATENÇÃO, você está respondendo no WHATSAPP oficial do {marca}, não no chat do site. Isso muda algumas regras:
- Não existem botões. Não fale em "Falar no WhatsApp" nem em formulário: a pessoa já está no WhatsApp.
- Para o teste grátis, escreva [TESTE] sozinho no fim da mensagem: ele vira o link do cadastro.
- Se a pessoa pedir para falar com alguém, pedir desconto especial ou plano sob medida, estiver pronta \
para fechar, reclamar, ou for cliente com problema no sistema, diga que vai chamar alguém do time agora e \
escreva [HUMANO] sozinho no fim da mensagem. Depois disso você não responde mais.
- Mensagens como [áudio], [imagem] ou [vídeo] são anexos que você não consegue abrir: peça com gentileza \
para a pessoa escrever.
- Mensagens curtas, como no WhatsApp: até 3 frases. Sem negrito com asteriscos duplos (use *assim* se precisar).
- Quando souber o nome da empresa ou quantos funcionários ela tem, acrescente na ÚLTIMA linha, sem \
comentar: #dados empresa=NOME; funcionarios=NÚMERO (só o que souber). Essa linha não chega à pessoa.
"""


def ligado(db: Session) -> bool:
    c = db.get(Config, CHAVE)
    return bool(c and c.valor == "1")


def ligar(db: Session, sim: bool) -> None:
    c = db.get(Config, CHAVE) or Config(chave=CHAVE, valor="")
    c.valor = "1" if sim else ""
    db.merge(c)


def _historico(msgs: list[WhatsMensagem]) -> list[dict]:
    juntos: list[dict] = []
    for m in msgs:
        papel = "user" if m.entrada else "assistant"
        if juntos and juntos[-1]["role"] == papel:
            juntos[-1]["content"] += "\n\n" + m.texto
        else:
            juntos.append({"role": papel, "content": m.texto})
    while juntos and juntos[0]["role"] != "user":
        juntos.pop(0)
    return juntos


def perguntar(db: Session, historico: list[dict]) -> str:
    """Chama o Claude com o mesmo texto do vendedor do site e as regras do WhatsApp."""
    from . import revisor, vendedor
    from .core import plataforma
    chave = revisor.chave(db)
    if not chave or not historico:
        return ""
    import anthropic
    sistema = vendedor.sistema(db, SITE) + EXTRA.replace("{marca}", plataforma()["nome"])
    try:
        cli = anthropic.Anthropic(api_key=chave, timeout=40, max_retries=1)
        r = cli.messages.create(model=vendedor.MODELO, max_tokens=500, system=sistema, messages=historico)
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        log.warning("Robô do WhatsApp: erro no Claude: %s", e)
        return ""


def preparar(resposta: str) -> tuple[str, bool, dict]:
    """Tira as marcas da resposta: devolve (texto para a pessoa, pediu humano, dados do lead)."""
    dados = {}
    m = re.search(r"^\s*#dados\s+(.+)$", resposta, re.M | re.I)
    if m:
        for parte in m.group(1).split(";"):
            k, _, v = parte.partition("=")
            k, v = k.strip().lower(), v.strip()
            if k == "empresa" and v:
                dados["empresa"] = v[:80]
            elif k.startswith("funcion"):
                n = re.sub(r"\D", "", v)
                if n:
                    dados["funcionarios"] = min(int(n), 100000)
        resposta = (resposta[:m.start()] + resposta[m.end():]).strip()
    humano = "[HUMANO]" in resposta
    from .core import plataforma
    link = f"{SITE}/cadastro?utm_source=whatsapp&utm_medium=robo" if plataforma()["cadastro_aberto"] else ""
    resposta = re.sub(r"\(?\s*(?:https?://)?[\w.-]*(?:\.[a-z]{2,})?/cadastro\S*\s*\)?", " [TESTE] ", resposta)
    if "[TESTE]" in resposta:
        resposta = re.sub(r"[ \t]*\[TESTE\][ \t]*", " ", resposta).strip()
        if link:
            resposta += f"\n\nComece o teste grátis aqui: {link}"
    resposta = resposta.replace("[HUMANO]", "").replace("**", "*")
    resposta = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", resposta)).strip()
    return resposta[:4000], humano, dados


def pausar(db: Session, ld: Lead, motivo: str) -> None:
    from . import crm
    if not ld.robo_pausado:
        ld.robo_pausado = True
        crm.registrar(db, ld, f"Robô do WhatsApp parou: {motivo}.")


def responder(lead_id: int) -> None:
    """Responde a última mensagem do lead, se for a vez do robô."""
    from . import crm, whatsapp
    with SessionLocal() as db:
        if not ligado(db) or not whatsapp.envia():
            return
        ld = db.get(Lead, lead_id)
        if not ld or ld.robo_pausado:
            return
        msgs = list(db.scalars(select(WhatsMensagem).where(WhatsMensagem.lead_id == lead_id)
                               .order_by(WhatsMensagem.criado_em.desc(), WhatsMensagem.id.desc()).limit(MAX_TURNOS)))
        if not msgs or not msgs[0].entrada:
            return   # já respondido (ou a última foi do time)
        desde = banco.agora() - timedelta(hours=24)
        feitas = db.scalar(select(func.count(WhatsMensagem.id)).where(
            WhatsMensagem.lead_id == lead_id, WhatsMensagem.robo.is_(True), WhatsMensagem.criado_em >= desde)) or 0
        if feitas >= MAX_RESPOSTAS:
            pausar(db, ld, "muitas mensagens no dia; passe para alguém do time")
            crm.retorno_24h(db, ld, "WhatsApp")
            db.commit()
            return
        bruto = perguntar(db, _historico(list(reversed(msgs))))
        if not bruto:
            return   # sem Claude ou erro: fica para o time (o lead já está na fila)
        texto, humano, dados = preparar(bruto)
        if dados.get("empresa") and not ld.empresa_nome:
            ld.empresa_nome = dados["empresa"]
        if dados.get("funcionarios") and not ld.funcionarios:
            ld.funcionarios = dados["funcionarios"]
        # se chegou mensagem nova enquanto o Claude pensava, a próxima chamada responde por tudo
        ultima = db.scalar(select(WhatsMensagem.id).where(WhatsMensagem.lead_id == lead_id)
                           .order_by(WhatsMensagem.criado_em.desc(), WhatsMensagem.id.desc()).limit(1))
        if ultima != msgs[0].id:
            db.commit()
            return
        if texto:
            mid, erro = whatsapp.enviar_api(msgs[0].wa_id, {"type": "text", "text": {"body": texto,
                                                                                      "preview_url": True}})
            if erro:
                log.warning("Robô do WhatsApp: envio recusado: %s", erro)
                db.commit()
                return
            db.add(WhatsMensagem(lead_id=lead_id, wa_id=msgs[0].wa_id, entrada=False, texto=texto, mid=mid,
                                 status="enviada", robo=True, lida=True, criado_em=banco.agora()))
        if humano:
            pausar(db, ld, "a pessoa quer falar com alguém do time")
            db.flush()
            from .db import LeadTarefa
            db.add(LeadTarefa(lead_id=ld.id, dono_id=ld.dono_id, titulo="Atender no WhatsApp (pediu uma pessoa)",
                              quando=banco.agora(), criado_em=banco.agora()))
        ld.atualizado_em = banco.agora()
        db.commit()
