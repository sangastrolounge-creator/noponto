"""Robô de vendas no WhatsApp: o mesmo assistente do chat do site (vendedor.py), respondendo no número
oficial da plataforma enquanto ninguém do time assumiu a conversa.

Regras:
- Liga e desliga em Master -> Rastreamento (Config "wa_robo"). Começa desligado.
- Responde só a última mensagem recebida (se a pessoa mandar várias seguidas, responde uma vez).
- Para sozinho naquele lead quando alguém do time responde (pelo CRM ou pelo celular), quando a pessoa pede
  para falar com uma pessoa ([HUMANO]) ou depois de MAX_RESPOSTAS no dia. Na ficha há o botão para religar.
- O que a pessoa conta (empresa, funcionários) vai para a ficha do lead.
- Quem já usa o sistema (celular de um usuário, ou lead que virou conta) é atendido como cliente: tira dúvidas
  de uso e, se contar um problema, passa para o time com uma tarefa "Problema no sistema: <resumo>".
- Áudio: chega transcrito (whatsapp_voz). Com a voz ligada em Master -> Rastreamento, áudio curto é respondido
  com mensagem de voz; o link do teste vai em texto logo depois. Se a voz falhar, a resposta vai em texto.
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
- Na sua primeira resposta da conversa, apresente-se como o assistente virtual do {marca} e diga que pode \
chamar uma pessoa do time a qualquer momento. Não repita a apresentação depois.
- Não existem botões. Não fale em "Falar no WhatsApp" nem em formulário: a pessoa já está no WhatsApp.
- Para o teste grátis, escreva [TESTE] sozinho no fim da mensagem: ele vira o link do cadastro.
- Se a pessoa pedir para falar com alguém, pedir desconto especial ou plano sob medida, estiver pronta \
para fechar, ou reclamar, diga que vai chamar alguém do time agora e escreva [HUMANO] sozinho no fim da \
mensagem. Depois disso você não responde mais.
- Mensagens que começam com [áudio] seguidas de texto são áudios que a pessoa mandou, já transcritos: \
responda ao que foi dito, normalmente. [áudio] sem texto, [imagem], [vídeo] ou [documento] são anexos que \
você não consegue abrir: peça com gentileza para a pessoa escrever ou mandar o áudio de novo.
- Fale só do {marca} e de controle de ponto, escala e equipe. Pergunta sobre outro assunto: responda em \
uma frase que você só ajuda com o {marca} e volte ao assunto.
- Mensagens curtas, como no WhatsApp: até 3 frases. Sem negrito com asteriscos duplos (use *assim* se precisar).
- Quando souber o nome da empresa ou quantos funcionários ela tem, acrescente na ÚLTIMA linha, sem \
comentar: #dados empresa=NOME; funcionarios=NÚMERO (só o que souber). Essa linha não chega à pessoa.
"""

CLIENTE = """

QUEM ESTÁ ESCREVENDO já usa o {marca}: {quem}.
- Ajude com dúvidas de uso, com calma e passo a passo curto. Não tente vender de novo.
- Onde fica cada coisa no painel do gestor (menu lateral): Escala; Ponto (Batidas e horas, Ajuste de ponto, \
Problemas de ponto); Ausências (Ausências e pedidos, Férias, Feriados); Relatórios; Fechamento; Pagamento; \
Equipe (Pessoas, Adicionar pessoa, Importar planilha, Setores); Locais (Locais de ponto, Totens); \
Configurações (Empresa e regras, Configuração guiada, Assinatura e plano).
- O funcionário bate o ponto pelo app (ou pelo navegador do celular), na tela inicial, com a localização \
conferida; se a empresa usa totem, bate no tablet com o PIN. Ponto esquecido ou errado: o funcionário pede \
ajuste pelo app, ou o gestor corrige em Ponto -> Ajuste de ponto (fica no histórico).
- Folha de ponto do mês: Fechamento. Relatórios e planilhas: Relatórios.
- Você não vê os dados da empresa e não altera nada no sistema. Não peça senha nem código.
- Se a pessoa contar um problema no sistema (ponto que não registrou, valor ou hora errada, tela com erro, \
não consegue entrar), peça o mínimo para entender (qual tela, qual funcionário, qual dia), diga que vai \
passar para o time, escreva [HUMANO] sozinho no fim e, na ÚLTIMA linha, sem comentar: \
#problema RESUMO EM UMA FRASE. Essa linha não chega à pessoa.
"""

VOZ = """

ESTA RESPOSTA VAI SER FALADA (a pessoa mandou áudio e vai ouvir a sua resposta como mensagem de voz):
- Escreva como se fala: frases curtas e naturais, no máximo 3, sem listas, sem asteriscos, sem emojis, \
sem endereços de site. Valores em reais escritos como se fala ("cinquenta e nove e noventa por mês").
- O link do teste grátis vai numa mensagem de texto separada: use [TESTE] normalmente, sem ler o link.
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


def quem_e(db: Session, ld: Lead, wa_id: str) -> str:
    """Se o número é de alguém que já usa o sistema (ou o lead já virou conta), uma linha sobre quem é.
    Só nome, papel, empresa e situação da conta: nada de dados de ponto ou de funcionários."""
    from . import whatsapp
    from .core import situacao
    from .db import Empresa, Usuario
    tel = whatsapp.telefone_do_wa(wa_id)
    u = db.scalar(select(Usuario).where(Usuario.celular == tel, Usuario.ativo.is_(True)).limit(1)) if tel else None
    e = db.get(Empresa, u.empresa_id) if u else (db.get(Empresa, ld.empresa_id) if ld.empresa_id else None)
    if not e:
        return ""
    sit = {"ativa": "assinatura em dia", "teste": "em teste grátis", "cortesia": "cortesia", "gratis": "plano grátis",
           "vencida": "assinatura vencida", "bloqueada": "conta bloqueada"}.get(situacao(e), "")
    plano = f"plano {e.plano.nome}" if e.plano else ""
    if u:
        papel = "gestor" if u.papel == "gestor" else "funcionário"
        pessoa = f"{(u.nome or '-').split()[0]}, {papel} da empresa {e.nome}"
    else:
        pessoa = f"contato da empresa {e.nome}"
    return ", ".join(x for x in (pessoa, plano, sit) if x)


def perguntar(db: Session, historico: list[dict], quem: str = "", voz: bool = False) -> str:
    """Chama o Claude com o mesmo texto do vendedor do site e as regras do WhatsApp."""
    from . import revisor, vendedor
    from .core import plataforma
    chave = revisor.chave(db)
    if not chave or not historico:
        return ""
    import anthropic
    marca = plataforma()["nome"]
    sistema = vendedor.sistema(db, SITE) + EXTRA.replace("{marca}", marca)
    if quem:
        sistema += CLIENTE.replace("{marca}", marca).replace("{quem}", quem)
    if voz:
        sistema += VOZ
    try:
        cli = anthropic.Anthropic(api_key=chave, timeout=40, max_retries=1)
        r = cli.messages.create(model=vendedor.MODELO, max_tokens=500, system=sistema, messages=historico)
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        log.warning("Robô do WhatsApp: erro no Claude: %s", e)
        return ""


def preparar(resposta: str) -> tuple[str, bool, dict]:
    """Tira as marcas da resposta: devolve (texto para a pessoa, pediu humano, dados do lead).
    dados pode trazer empresa, funcionarios, problema (resumo) e link (o do teste, à parte do texto)."""
    dados = {}
    p = re.search(r"^\s*#problema\s+(.+)$", resposta, re.M | re.I)
    if p:
        dados["problema"] = p.group(1).strip()[:160]
        resposta = (resposta[:p.start()] + resposta[p.end():]).strip()
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
            dados["link"] = f"Comece o teste grátis aqui: {link}"
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
        from . import whatsapp_voz
        voz = whatsapp_voz.ligado(db) and whatsapp_voz.responder_com_voz(msgs[0])
        bruto = perguntar(db, _historico(list(reversed(msgs))), quem_e(db, ld, msgs[0].wa_id), voz)
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
        para = msgs[0].wa_id
        link = dados.get("link", "")
        if texto and voz:
            mid, erro = whatsapp_voz.responder_em_voz(para, texto)
            if not erro:
                db.add(WhatsMensagem(lead_id=lead_id, wa_id=para, entrada=False, texto=texto, mid=mid, audio=True,
                                     status="enviada", robo=True, lida=True, criado_em=banco.agora()))
                texto = ""          # já foi falado; o link (se houver) segue em texto
            else:
                log.info("Robô do WhatsApp: voz indisponível (%s); resposta em texto", erro)
        partes = [x for x in (texto, link) if x]
        if partes:
            corpo = "\n\n".join(partes)
            mid, erro = whatsapp.enviar_api(para, {"type": "text", "text": {"body": corpo, "preview_url": True}})
            if erro:
                log.warning("Robô do WhatsApp: envio recusado: %s", erro)
                db.commit()
                return
            db.add(WhatsMensagem(lead_id=lead_id, wa_id=para, entrada=False, texto=corpo, mid=mid,
                                 status="enviada", robo=True, lida=True, criado_em=banco.agora()))
        if humano:
            pausar(db, ld, "a pessoa quer falar com alguém do time")
            db.flush()
            from .db import LeadTarefa
            problema = dados.get("problema", "")
            titulo = (f"Problema no sistema: {problema}" if problema else "Atender no WhatsApp (pediu uma pessoa)")
            db.add(LeadTarefa(lead_id=ld.id, dono_id=ld.dono_id, titulo=titulo[:120],
                              quando=banco.agora(), criado_em=banco.agora()))
            if problema:
                crm.registrar(db, ld, f"Problema relatado no WhatsApp: {problema}")
        ld.atualizado_em = banco.agora()
        db.commit()
