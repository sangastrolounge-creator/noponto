"""Assistente de vendas da página inicial: um chat no site, respondido pelo Claude, que tira dúvidas
de quem está conhecendo o Data Worke e passa a conversa para o WhatsApp do time quando a pessoa
quer falar com alguém. As conversas ficam guardadas para o Master ver em Master -> Conversas."""
import hashlib
import json
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from . import email_envio, revisor
from .core import (admin_logado, avisar, celular_limpo, celular_valido, dentro_do_limite, exigir_csrf, ip, ir,
                   plataforma, render)
from .db import ConversaVenda, Plano, SessionLocal, Usuario, get_db

rota = APIRouter()
MODELO = os.environ.get("VENDEDOR_MODELO", "claude-haiku-5-5")
MAX_TURNOS = 16          # mensagens de cada conversa que vão para o modelo
MAX_TEXTO = 600          # caracteres por mensagem da pessoa
MAX_MENSAGENS = 30       # mensagens da pessoa por conversa

SISTEMA = """Você é o assistente de vendas do {marca}, um sistema brasileiro de ponto eletrônico, \
escala e fechamento do mês para pequenas e médias empresas (restaurantes, bares, lojas, clínicas, \
padarias). Você conversa no chat do site com quem está conhecendo o produto.

Seu objetivo: entender a empresa da pessoa (ramo, quantos funcionários, como controla o ponto hoje), \
mostrar como o {marca} resolve o problema dela e levar ao próximo passo: começar o teste grátis de \
{dias_teste} dias em {site}/cadastro, ou falar com uma pessoa do time pelo WhatsApp.

Como responder:
- Escreva no idioma da pessoa. Frases curtas, tom simpático e direto, como um bom vendedor de loja. \
No máximo 4 frases ou uma lista curta por resposta. Nada de markdown pesado: no máximo negrito e listas.
- Faça uma pergunta por vez para entender a necessidade.
- Use só os fatos abaixo. Se não souber, diga que vai confirmar com o time e ofereça o WhatsApp. \
Nunca invente preço, prazo, desconto, integração ou recurso.
- Não dê conselho jurídico. Em dúvidas de lei trabalhista, explique o que o sistema faz e sugira \
confirmar com o contador.
- Se a pessoa pedir para falar com alguém, pedir desconto especial, plano sob medida, ou estiver \
pronta para fechar, diga que ela pode tocar em "Falar no WhatsApp" logo abaixo do chat.
- Não peça CPF, senha, dados de cartão ou documentos. Nome, empresa e número de funcionários bastam.
- Depois de entender a empresa (ou se a pessoa mostrar interesse, perguntar preço de novo ou disser que \
vai pensar), convide uma vez para deixar o nome e o WhatsApp no formulário "Quero que me chamem" que \
aparece no chat, para alguém do time chamar e ajudar a começar. Se ela escrever o número aqui mesmo, \
agradeça e diga que o time vai chamar em horário comercial. Não insista se ela não quiser.

O que o {marca} faz:
- Ponto pelo celular do funcionário, com conferência do local (GPS), foto opcional e \
reconhecimento facial opcional com prova de vida (com termo de consentimento, LGPD). Sem relógio de \
ponto para comprar: também funciona como totem num tablet, com PIN e conferência do rosto.
- Comprovante de cada marcação. Funciona como ponto eletrônico por programa (REP-P, Portaria \
MTP 671/2021): gera os arquivos AFD e AEJ assinados digitalmente, não restringe horário de marcação \
e não marca ponto sozinho. O registro do programa no INPI está em andamento.
- Escala da semana montada no painel e avisada à equipe; trocas de turno; tarefas do dia; mural.
- Alertas de atraso, falta e ponto esquecido; aviso antes de entrar em hora extra.
- Horas trabalhadas, extras, adicional noturno, banco de horas, faltas e DSR calculados sozinhos.
- Fechamento do mês e folha de ponto pronta para imprimir e assinar; relatórios e planilhas.
- Férias e ausências com pedido pelo app; ajuste de ponto com histórico.
- App Android na Play Store; no iPhone funciona pelo navegador (app próprio em breve).
- Atende pelo site {site}. Teste grátis de {dias_teste} dias, sem cartão para começar.
- Não emite holerite oficial nem envia nada ao eSocial; os relatórios ajudam o contador.

Planos e preços (mensais, em reais):
{planos}
{anual}
"""


def _reais(c: int) -> str:
    return f"R$ {c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _texto_planos(db: Session) -> str:
    linhas = []
    for p in db.scalars(select(Plano).where(Plano.ativo.is_(True)).order_by(Plano.ordem, Plano.preco_centavos)):
        if p.por_pessoa_centavos:
            preco = f"{_reais(p.base_centavos)} + {_reais(p.por_pessoa_centavos)} por funcionário"
            if p.minimo_centavos:
                preco += f" (mínimo {_reais(p.minimo_centavos)})"
        else:
            preco = _reais(p.preco_centavos)
        lim = f"até {p.limite_funcionarios} funcionários" if p.limite_funcionarios else "funcionários ilimitados"
        recursos = [n for n, ok in (("ponto com GPS", p.com_gps), ("foto no ponto", p.com_foto),
                                    ("reconhecimento facial", p.com_facial), ("cerca no mapa", p.com_cerca),
                                    ("alertas", p.com_alertas), ("folha de pagamento", p.com_pagamento)) if ok]
        extra = "; ".join(x.strip() for x in (p.descricao or "").splitlines() if x.strip())
        linhas.append(f"- {p.nome}: {preco} por mês, {lim}. Inclui: {', '.join(recursos) or 'o básico'}."
                      + (f" {extra}" if extra else ""))
    return "\n".join(linhas) or "- (os planos estão sendo atualizados; ofereça o WhatsApp para preços)"


def sistema(db: Session, site: str) -> str:
    plat = plataforma()
    meses = plat.get("meses_anual") or 0
    anual = (f"Pagamento anual: paga {meses} mensalidades e usa 12 meses." if meses and meses < 12 else "")
    return (SISTEMA.replace("{marca}", plat["nome"]).replace("{dias_teste}", str(plat.get("dias_teste") or 7))
            .replace("{site}", site).replace("{planos}", _texto_planos(db)).replace("{anual}", anual))


def ligado(db: Session | None = None) -> bool:
    return revisor.configurado(db)


def _limpar(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()[:MAX_TEXTO]


@rota.post("/vendedor/mensagem")
async def mensagem(request: Request, tarefas_bg: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        corpo = await request.json()
    except ValueError:
        return JSONResponse({"erro": "Mensagem inválida."}, status_code=400)
    texto = _limpar(corpo.get("texto"))
    novo_contato = False
    cid = str(corpo.get("conversa") or "")[:40]
    if not texto:
        return JSONResponse({"erro": "Escreva sua pergunta."}, status_code=400)
    quem = ip(request)
    if not dentro_do_limite(f"vendedor:{quem}", 40, 3600):
        return JSONResponse({"erro": "Muitas mensagens seguidas. Fale com a gente pelo WhatsApp."}, status_code=429)
    conv = db.scalar(select(ConversaVenda).where(ConversaVenda.chave == cid)) if cid else None
    if not conv:
        conv = ConversaVenda(chave=hashlib.sha256(os.urandom(16)).hexdigest()[:32], criado_em=banco.agora(),
                             atualizado_em=banco.agora(), mensagens="[]",
                             origem=_limpar(request.headers.get("referer"))[:200],
                             ip_hash=hashlib.sha256(quem.encode()).hexdigest()[:16])
        db.add(conv)
    msgs = json.loads(conv.mensagens or "[]")
    if sum(1 for m in msgs if m["papel"] == "pessoa") >= MAX_MENSAGENS:
        return JSONResponse({"conversa": conv.chave, "resposta": "Para continuar, toque em \"Falar no WhatsApp\" "
                             "que alguém do time responde você.", "whatsapp": True})
    msgs.append({"papel": "pessoa", "texto": texto})
    if not conv.telefone:
        achado = re.search(r"(?:\+?55\s*)?\(?\d{2}\)?\s*9?\s*\d{4}[-\s.]?\d{4}", texto)
        tel = celular_limpo(achado.group(0)) if achado else ""
        if celular_valido(tel):
            conv.telefone, conv.contato_em = tel, banco.agora()   # a própria pessoa escreveu para ser chamada
            novo_contato = True
    api_key = revisor.chave(db)
    if not api_key:
        resposta = "No momento o assistente está fora do ar. Toque em \"Falar no WhatsApp\" que a gente responde."
    else:
        import anthropic
        historico = [{"role": "user" if m["papel"] == "pessoa" else "assistant", "content": m["texto"]}
                     for m in msgs[-MAX_TURNOS:]]
        while historico and historico[0]["role"] != "user":
            historico.pop(0)
        try:
            cli = anthropic.Anthropic(api_key=api_key, timeout=40, max_retries=1)
            r = cli.messages.create(model=MODELO, max_tokens=500,
                                    system=sistema(db, str(request.base_url).rstrip("/")),
                                    messages=historico)
            resposta = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        except Exception:
            resposta = ""
        if not resposta:
            resposta = "Tive um problema para responder agora. Toque em \"Falar no WhatsApp\" que a gente continua por lá."
    msgs.append({"papel": "assistente", "texto": resposta[:3000]})
    conv.mensagens = json.dumps(msgs, ensure_ascii=False)
    conv.atualizado_em = banco.agora()
    db.commit()
    if novo_contato:
        tarefas_bg.add_task(avisar_time, conv.id)
    return {"conversa": conv.chave, "resposta": resposta, "contato": bool(conv.telefone)}


def avisar_time(conv_id: int) -> None:
    """E-mail para o dono da plataforma quando chega um contato novo (se o SMTP estiver configurado)."""
    with SessionLocal() as db:
        c = db.get(ConversaVenda, conv_id)
        if not c or not email_envio.configurado():
            return
        para = os.environ.get("ADMIN_EMAIL", "").strip() or (plataforma().get("email_legal") or "").strip()
        if not para:
            return
        perguntas = [m["texto"] for m in json.loads(c.mensagens or "[]") if m["papel"] == "pessoa"][:5]
        email_envio.enviar(para, f"Novo contato pelo site: {c.nome or c.telefone}",
                           f"Nome: {c.nome or '-'}\nEmpresa: {c.empresa or '-'}\nWhatsApp: {c.telefone}\n"
                           f"Chamar: https://wa.me/55{c.telefone}\n\nPerguntas:\n- " + "\n- ".join(perguntas or ["-"])
                           + "\n\nVeja em Master -> Conversas.")


@rota.post("/vendedor/contato")
async def contato(request: Request, tarefas_bg: BackgroundTasks, db: Session = Depends(get_db)):
    """A pessoa deixa nome e WhatsApp para o time chamar (formulário do chat, com autorização)."""
    try:
        corpo = await request.json()
    except ValueError:
        return JSONResponse({"erro": "Dados inválidos."}, status_code=400)
    if not dentro_do_limite(f"vendedor-contato:{ip(request)}", 5, 3600):
        return JSONResponse({"erro": "Muitas tentativas. Fale com a gente pelo WhatsApp."}, status_code=429)
    nome = _limpar(corpo.get("nome"))[:80]
    empresa = _limpar(corpo.get("empresa"))[:80]
    tel = celular_limpo(str(corpo.get("telefone") or ""))
    if not nome:
        return JSONResponse({"erro": "Informe seu nome."}, status_code=400)
    if not celular_valido(tel):
        return JSONResponse({"erro": "Confira o WhatsApp com DDD, por exemplo (98) 99999-0000."}, status_code=400)
    if not corpo.get("autorizo"):
        return JSONResponse({"erro": "Marque a autorização para a gente poder chamar você."}, status_code=400)
    cid = str(corpo.get("conversa") or "")[:40]
    conv = db.scalar(select(ConversaVenda).where(ConversaVenda.chave == cid)) if cid else None
    if not conv:
        conv = ConversaVenda(chave=hashlib.sha256(os.urandom(16)).hexdigest()[:32], criado_em=banco.agora(),
                             atualizado_em=banco.agora(), mensagens="[]",
                             origem=_limpar(request.headers.get("referer"))[:200],
                             ip_hash=hashlib.sha256(ip(request).encode()).hexdigest()[:16])
        db.add(conv)
    conv.nome, conv.empresa, conv.telefone, conv.autorizou = nome, empresa, tel, True
    conv.contato_em = conv.atualizado_em = banco.agora()
    db.commit()
    tarefas_bg.add_task(avisar_time, conv.id)
    return {"ok": True, "conversa": conv.chave,
            "mensagem": f"Obrigado, {nome.split()[0]}! Alguém do time vai chamar você no WhatsApp em horário comercial."}


SITUACOES = {"": "Novo", "contatado": "Contatado", "fechou": "Fechou", "perdido": "Não fechou"}


@rota.post("/master/conversas/{cid}/situacao", dependencies=[Depends(exigir_csrf)])
def mudar_situacao(cid: int, request: Request, situacao: str = Form(""), u: Usuario = Depends(admin_logado),
                   db: Session = Depends(get_db)):
    c = db.get(ConversaVenda, cid)
    if c and situacao in SITUACOES:
        c.situacao = situacao
        db.commit()
        avisar(request, "Situação do contato atualizada.")
    return ir("/master/conversas#contatos")


@rota.post("/vendedor/whatsapp")
async def foi_whatsapp(request: Request, db: Session = Depends(get_db)):
    """Marca que a conversa passou para o WhatsApp (o link abre no navegador da pessoa)."""
    try:
        cid = str((await request.json()).get("conversa") or "")[:40]
    except ValueError:
        cid = ""
    conv = db.scalar(select(ConversaVenda).where(ConversaVenda.chave == cid)) if cid else None
    if conv:
        conv.foi_whatsapp = True
        db.commit()
    return {"ok": True}


@rota.get("/master/conversas")
def conversas(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    lista = list(db.scalars(select(ConversaVenda).order_by(ConversaVenda.atualizado_em.desc()).limit(300)))
    itens = [{"c": c, "msgs": json.loads(c.mensagens or "[]")} for c in lista]
    contatos = sorted([x for x in itens if x["c"].telefone],
                      key=lambda x: (x["c"].situacao in ("fechou", "perdido"), -(x["c"].contato_em or x["c"].atualizado_em).timestamp()))
    marca = plataforma()["nome"]
    for x in contatos:
        c = x["c"]
        msg = f"Olá{(' ' + c.nome.split()[0]) if c.nome else ''}! Aqui é do {marca}. Vi que você conversou com a gente pelo site. Posso te ajudar a começar?"
        x["zap"] = f"https://wa.me/55{c.telefone}?text=" + quote(msg)
    return render(request, "master/conversas.html", u=u, e=u.empresa, aba="master", sub="conversas",
                  itens=itens, contatos=contatos, SITUACOES=SITUACOES, ligado=ligado(db), modelo=MODELO)


def limpar_antigas(dias: int = 180) -> int:
    """LGPD: conversas sem atividade há mais de `dias` são apagadas (chamado pelo agendador)."""
    from datetime import timedelta
    with SessionLocal() as db:
        n = db.query(ConversaVenda).filter(ConversaVenda.atualizado_em < banco.agora() - timedelta(days=dias)).delete()
        db.commit()
        return n

