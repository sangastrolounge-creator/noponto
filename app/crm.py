"""CRM da plataforma (Master -> CRM): funil de vendas, fila e dono de cada lead, histórico de contatos,
tarefas de retorno, leads automáticos (chat do site e contas de teste) e chat ao vivo, em que um
atendente acompanha as conversas do robô e assume no lugar dele. Também cuida da equipe do Master:
cada pessoa recebe só as áreas que o dono liberar."""
import json
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import (MASTER_AREAS, admin_logado, avisar, celular_limpo, celular_valido, email_valido,
                   exigir_csrf, ir, plataforma, render, situacao)
from . import whatsapp
from .db import ConversaVenda, Empresa, Lead, LeadAtividade, LeadTarefa, SessionLocal, Usuario, WhatsMensagem, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

ETAPAS = [("novo", "Novo"), ("contato", "Em contato"), ("proposta", "Proposta"), ("teste", "Em teste"),
          ("cliente", "Cliente"), ("perdido", "Perdido")]
NOME_ETAPA = dict(ETAPAS)
ORIGENS = {"chat": "Chat do site", "teste": "Conta de teste", "manual": "Cadastro manual",
           "indicacao": "Indicação", "whatsapp": "WhatsApp", "email": "E-mail", "instagram": "Instagram",
           "espera": "Lista de espera (EUA)"}
PAISES_LEAD = {"BR": "Brasil", "US": "Estados Unidos"}
MANUAIS = ["manual", "instagram", "indicacao", "whatsapp"]   # origens que a equipe escolhe ao cadastrar
TIPOS = {"nota": "Anotação", "ligacao": "Ligação", "whatsapp": "WhatsApp", "email": "E-mail",
         "etapa": "Etapa", "sistema": "Sistema", "instagram": "Direct do Instagram"}
AO_VIVO_MIN = 30   # conversas com atividade nos últimos X minutos aparecem no chat ao vivo


# ------------------------------------------------------------------ apoio
def _agora():
    return banco.agora()


def empresa_plataforma_id(db: Session) -> int | None:
    return db.scalar(select(Usuario.empresa_id).where(Usuario.is_admin.is_(True)).limit(1))


def equipe(db: Session) -> list[Usuario]:
    """Quem tem acesso ao CRM (para escolher o dono do lead e acompanhar)."""
    pid = empresa_plataforma_id(db)
    if not pid:
        return []
    gente = db.scalars(select(Usuario).where(Usuario.empresa_id == pid, Usuario.ativo.is_(True))
                       .order_by(Usuario.nome))
    return [x for x in gente if "crm" in x.areas_master]


def supervisor(u: Usuario) -> bool:
    return "supervisao" in u.areas_master


def pode_ver(u: Usuario, ld: Lead) -> bool:
    return supervisor(u) or ld.dono_id in (None, u.id)


def _lead(db: Session, u: Usuario, lid: int) -> Lead | None:
    ld = db.get(Lead, lid)
    return ld if ld and pode_ver(u, ld) else None


def registrar(db: Session, ld: Lead, texto: str, tipo: str = "sistema", autor: Usuario | None = None) -> None:
    db.add(LeadAtividade(lead_id=ld.id, autor_id=autor.id if autor else None, tipo=tipo, texto=texto[:4000],
                         criado_em=_agora()))
    ld.atualizado_em = _agora()


def mudar_etapa(db: Session, ld: Lead, etapa: str, autor: Usuario | None = None, motivo: str = "") -> bool:
    if etapa not in NOME_ETAPA or etapa == ld.etapa:
        return False
    antes = ld.etapa
    ld.etapa = etapa
    if etapa == "perdido":
        ld.motivo_perda = motivo[:120]
    registrar(db, ld, f"{NOME_ETAPA.get(antes, antes)} → {NOME_ETAPA[etapa]}" + (f" ({motivo})" if motivo else ""),
              "etapa", autor)
    return True


def arroba(texto: str) -> str:
    """Perfil do Instagram sem o @, em minúsculas. Aceita "@perfil", "perfil" ou o link do perfil.
    Devolve "" se não parecer um perfil válido."""
    t = (texto or "").strip()
    m = re.search(r"instagram\.com/([^/?#\s]+)", t, re.I)
    if m:
        t = m.group(1)
    t = t.lstrip("@").strip().lower()
    return t if re.fullmatch(r"[a-z0-9._]{1,30}", t) else ""


def _achar(db: Session, telefone: str = "", email: str = "", instagram: str = "") -> Lead | None:
    conds = []
    if telefone:
        conds.append(Lead.telefone == telefone)
    if email:
        conds.append(func.lower(Lead.email) == email.lower())
    if instagram:
        conds.append(Lead.instagram == instagram)
    if not conds:
        return None
    return db.scalar(select(Lead).where(or_(*conds)).order_by(Lead.id.desc()).limit(1))


def retorno_24h(db: Session, ld: Lead, canal: str = "Instagram") -> None:
    """Lead novo sem dono (Instagram, WhatsApp): tarefa de retorno em até 24h (passa para quem pegar o lead)."""
    if ld.dono_id is None:
        db.add(LeadTarefa(lead_id=ld.id, dono_id=None, titulo=f"Responder o lead do {canal}",
                          quando=_agora() + timedelta(hours=24), criado_em=_agora()))


def tarefas_sem_dono_para(db: Session, ld: Lead) -> None:
    """Quando o lead ganha dono, os retornos que estavam sem dono passam para ele."""
    if ld.dono_id:
        for t in db.scalars(select(LeadTarefa).where(LeadTarefa.lead_id == ld.id, LeadTarefa.dono_id.is_(None),
                                                     LeadTarefa.feita_em.is_(None))):
            t.dono_id = ld.dono_id


# ------------------------------------------------------------------ leads automáticos
def lead_da_conversa(db: Session, conv: ConversaVenda) -> Lead | None:
    """Quem deixou o WhatsApp no chat do site vira lead (ou atualiza o lead que já existia)."""
    if not conv.telefone:
        return None
    ld = db.scalar(select(Lead).where(Lead.conversa_id == conv.id)) or _achar(db, conv.telefone)
    novo = ld is None
    if novo:
        ld = Lead(origem="chat", etapa="novo", criado_em=_agora(), atualizado_em=_agora())
        db.add(ld)
    ld.nome = ld.nome or conv.nome
    ld.empresa_nome = ld.empresa_nome or conv.empresa
    ld.telefone = ld.telefone or conv.telefone
    ld.conversa_id = ld.conversa_id or conv.id
    ld.autorizou = ld.autorizou or conv.autorizou
    ld.utm = ld.utm or (conv.utm or "")
    db.flush()
    if novo:
        perguntas = [m["texto"] for m in json.loads(conv.mensagens or "[]") if m["papel"] == "pessoa"][:3]
        registrar(db, ld, "Chegou pelo chat do site." + (" Perguntou: " + " / ".join(perguntas) if perguntas else ""))
    return ld


def lead_do_cadastro(db: Session, e: Empresa, gestor: Usuario) -> None:
    """Quem cria conta de teste vira lead na etapa "Em teste" (sem derrubar o cadastro se algo falhar)."""
    try:
        if e.id == empresa_plataforma_id(db):
            return
        tel = celular_limpo(gestor.celular or "")
        ld = db.scalar(select(Lead).where(Lead.empresa_id == e.id)) or _achar(db, tel, gestor.email or "")
        novo = ld is None
        if novo:
            from .rastreio import canal
            veio_do_ig = canal(e.origem or "").startswith("Instagram ·")
            ld = Lead(origem="instagram" if veio_do_ig else "teste", etapa="novo", criado_em=_agora(),
                      atualizado_em=_agora())
            db.add(ld)
        ld.nome = ld.nome or gestor.nome
        ld.empresa_nome = ld.empresa_nome or e.nome
        ld.email = ld.email or (gestor.email or "")
        ld.telefone = ld.telefone or tel
        ld.empresa_id = e.id
        ld.pais = e.pais or "BR"
        ld.utm = ld.utm or (e.origem or "")
        db.flush()
        registrar(db, ld, f"Criou a conta de teste \"{e.nome}\".")
        if ld.etapa in ("novo", "contato", "proposta"):
            mudar_etapa(db, ld, "teste")
        if novo and ld.origem == "instagram":
            retorno_24h(db, ld)
        db.commit()
    except Exception:
        db.rollback()


def lead_da_lista_espera(db: Session, nome: str, empresa: str, email: str, telefone: str,
                         funcionarios: int, pais: str = "US") -> Lead:
    """Quem entra na lista de espera do site americano vira lead (ou atualiza o que já existia)."""
    ld = _achar(db, telefone, email)
    novo = ld is None
    if novo:
        ld = Lead(origem="espera", etapa="novo", criado_em=_agora(), atualizado_em=_agora())
        db.add(ld)
    ld.nome, ld.empresa_nome = ld.nome or nome, ld.empresa_nome or empresa
    ld.email, ld.telefone = ld.email or email, ld.telefone or telefone
    ld.funcionarios = ld.funcionarios or funcionarios
    ld.pais, ld.autorizou = pais, True
    db.flush()
    registrar(db, ld, "Entrou na lista de espera do site dos EUA." if novo else "Entrou de novo na lista de espera dos EUA.")
    db.commit()
    return ld


def sincronizar() -> None:
    """Chamado pelo agendador: conversas com telefone viram lead; contas que assinaram viram cliente."""
    with SessionLocal() as db:
        ligadas = select(Lead.conversa_id).where(Lead.conversa_id.is_not(None))
        for conv in db.scalars(select(ConversaVenda).where(ConversaVenda.telefone != "",
                                                           ConversaVenda.id.not_in(ligadas)).limit(50)):
            lead_da_conversa(db, conv)
        for ld in db.scalars(select(Lead).where(Lead.empresa_id.is_not(None),
                                                Lead.etapa.not_in(("cliente", "perdido")))):
            e = db.get(Empresa, ld.empresa_id)
            if e and situacao(e) == "ativa":
                mudar_etapa(db, ld, "cliente")
                registrar(db, ld, "Assinou um plano.")
        db.commit()


# ------------------------------------------------------------------ funil
def _visiveis(db: Session, u: Usuario, ver: str, q: str, origem: str = "", pais: str = ""):
    sel = select(Lead)
    if origem in ORIGENS:
        sel = sel.where(Lead.origem == origem)
    if pais in PAISES_LEAD:
        sel = sel.where(Lead.pais == pais)
    if ver == "meus":
        sel = sel.where(Lead.dono_id == u.id)
    elif ver == "fila":
        sel = sel.where(Lead.dono_id.is_(None))
    elif ver.isdigit() and supervisor(u):
        sel = sel.where(Lead.dono_id == int(ver))
    elif not (ver == "todos" and supervisor(u)):
        sel = sel.where(or_(Lead.dono_id == u.id, Lead.dono_id.is_(None)))
    termo = q.strip()
    if termo:
        tel = re.sub(r"\D", "", termo)
        conds = [Lead.nome.ilike(f"%{termo}%"), Lead.empresa_nome.ilike(f"%{termo}%"),
                 Lead.email.ilike(f"%{termo}%"), Lead.instagram.contains(termo.lstrip("@").lower())]
        if len(tel) >= 4:
            conds.append(Lead.telefone.contains(tel))
        sel = sel.where(or_(*conds))
    return list(db.scalars(sel.order_by(Lead.atualizado_em.desc()).limit(600)))


@rota.get("/master/crm")
def funil(request: Request, ver: str = "", q: str = "", origem: str = "", pais: str = "",
          u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    ver = ver or ("todos" if supervisor(u) else "meus_fila")
    origem = origem if origem in ORIGENS else ""
    pais = pais if pais in PAISES_LEAD else ""
    leads = _visiveis(db, u, ver, q, origem, pais)
    ids = [x.id for x in leads]
    pend = dict(db.execute(select(LeadTarefa.lead_id, func.min(LeadTarefa.quando)).where(
        LeadTarefa.lead_id.in_(ids or [0]), LeadTarefa.feita_em.is_(None)).group_by(LeadTarefa.lead_id)).all())
    colunas = [(k, v, [x for x in leads if x.etapa == k]) for k, v in ETAPAS]
    from .whatsapp import nao_lidas
    zap_novas = nao_lidas(db, ids)
    time = equipe(db)
    nomes = {x.id: x.nome for x in time}
    fila = db.scalar(select(func.count(Lead.id)).where(Lead.dono_id.is_(None),
                                                      Lead.etapa.not_in(("cliente", "perdido")))) or 0
    return render(request, "master/crm.html", u=u, e=u.empresa, aba="master", sub="crm", colunas=colunas,
                  pend=pend, agora=_agora(), nomes=nomes, time=time, ver=ver, q=q, fila=fila,
                  supervisor=supervisor(u), ORIGENS=ORIGENS, MANUAIS=MANUAIS, origem=origem, total=len(leads),
                  PAISES_LEAD=PAISES_LEAD, pais=pais, zap_novas=zap_novas,
                  valor=sum(x.valor_centavos for x in leads if x.etapa not in ("perdido",)))


@rota.post("/master/crm/novo", dependencies=CSRF)
def novo(request: Request, nome: str = Form(""), empresa_nome: str = Form(""), telefone: str = Form(""),
         email: str = Form(""), instagram: str = Form(""), funcionarios: str = Form(""),
         origem: str = Form("manual"), u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    tel = celular_limpo(telefone)
    ig = arroba(instagram)
    if instagram.strip() and not ig:
        avisar(request, "Confira o @ do Instagram (só letras, números, ponto e sublinhado).", "erro")
        return ir("/master/crm")
    if not (nome.strip() or ig) or (telefone and not celular_valido(tel)) or (email and not email_valido(email.strip())):
        avisar(request, "Informe o nome (ou o @ do Instagram) e confira o telefone (com DDD) e o e-mail.", "erro")
        return ir("/master/crm")
    if tel or email.strip() or ig:
        ja = _achar(db, tel, email.strip(), ig)
        if ja:
            avisar(request, "Já existe um lead com este telefone, e-mail ou Instagram.", "erro")
            return ir(f"/master/crm/{ja.id}" if pode_ver(u, ja) else "/master/crm")
    if origem not in ORIGENS:
        origem = "instagram" if ig else "manual"
    ld = Lead(nome=(nome.strip() or f"@{ig}")[:80], empresa_nome=empresa_nome.strip()[:80], telefone=tel,
              email=email.strip()[:120], instagram=ig, origem=origem, etapa="novo",
              funcionarios=max(0, min(int(funcionarios), 100000)) if funcionarios.strip().isdigit() else 0,
              dono_id=u.id, criado_em=_agora(), atualizado_em=_agora())
    db.add(ld)
    db.flush()
    registrar(db, ld, "Lead cadastrado à mão.", "sistema", u)
    db.commit()
    return ir(f"/master/crm/{ld.id}")


@rota.post("/master/crm/pegar", dependencies=CSRF)
def pegar_proximo(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    ld = db.scalar(select(Lead).where(Lead.dono_id.is_(None), Lead.etapa.not_in(("cliente", "perdido")))
                   .order_by(Lead.criado_em).limit(1).with_for_update())
    if not ld:
        avisar(request, "A fila está vazia.")
        return ir("/master/crm")
    ld.dono_id = u.id
    tarefas_sem_dono_para(db, ld)
    registrar(db, ld, f"{u.nome} pegou o lead da fila.", "sistema", u)
    db.commit()
    return ir(f"/master/crm/{ld.id}")


@rota.post("/master/crm/{lid}/etapa", dependencies=CSRF)
async def etapa(lid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if "application/json" in request.headers.get("content-type", ""):
        dados = await request.json()
    else:
        dados = dict(await request.form())
    ld = _lead(db, u, lid)
    if not ld:
        return JSONResponse({"erro": "Lead não encontrado."}, status_code=404)
    if ld.dono_id is None:
        ld.dono_id = u.id
        tarefas_sem_dono_para(db, ld)
    mudar_etapa(db, ld, str(dados.get("etapa", "")), u, str(dados.get("motivo", "")))
    db.commit()
    if "application/json" in request.headers.get("content-type", ""):
        return {"ok": True, "etapa": ld.etapa}
    return ir(f"/master/crm/{lid}")


# ------------------------------------------------------------------ ficha do lead
@rota.get("/master/crm/tarefas")
def tarefas(request: Request, todos: int = 0, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    sel = select(LeadTarefa).where(LeadTarefa.feita_em.is_(None))
    if not (todos and supervisor(u)):
        sel = sel.where(LeadTarefa.dono_id == u.id)
    lista = list(db.scalars(sel.order_by(LeadTarefa.quando).limit(300)))
    leads = {x.id: x for x in db.scalars(select(Lead).where(Lead.id.in_([t.lead_id for t in lista] or [0])))}
    nomes = {x.id: x.nome for x in equipe(db)}
    return render(request, "master/crm_tarefas.html", u=u, e=u.empresa, aba="master", sub="tarefas",
                  lista=lista, leads=leads, nomes=nomes, agora=_agora(), todos=todos, supervisor=supervisor(u))


@rota.get("/master/crm/ao-vivo")
def ao_vivo(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/crm_ao_vivo.html", u=u, e=u.empresa, aba="master", sub="aovivo",
                  supervisor=supervisor(u))


@rota.get("/master/crm/ao-vivo.json")
def ao_vivo_dados(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    desde = _agora() - timedelta(minutes=AO_VIVO_MIN)
    nomes = {x.id: x.nome for x in equipe(db)}
    nomes.update({u.id: u.nome})
    convs = []
    for c in db.scalars(select(ConversaVenda).where(ConversaVenda.atualizado_em >= desde)
                        .order_by(ConversaVenda.atualizado_em.desc()).limit(60)):
        msgs = json.loads(c.mensagens or "[]")
        convs.append({"id": c.id, "atualizado": regras.para_local(u.empresa, c.atualizado_em).strftime("%H:%M"),
                      "segundos": int((_agora() - c.atualizado_em).total_seconds()),
                      "nome": c.nome or "", "telefone": c.telefone or "",
                      "atendente": nomes.get(c.atendente_id, "") if c.atendente_id else "",
                      "atendente_id": c.atendente_id, "meu": c.atendente_id == u.id,
                      "mensagens": msgs[-40:], "total": len(msgs)})
    resp = {"conversas": convs, "eu": u.id}
    if supervisor(u):
        hoje = regras.para_utc(u.empresa, datetime.combine(regras.agora_local(u.empresa).date(), datetime.min.time()))
        ativ = dict(db.execute(select(LeadAtividade.autor_id, func.count()).where(
            LeadAtividade.criado_em >= hoje, LeadAtividade.autor_id.is_not(None))
            .group_by(LeadAtividade.autor_id)).all())
        ultima = dict(db.execute(select(LeadAtividade.autor_id, func.max(LeadAtividade.criado_em))
                                 .group_by(LeadAtividade.autor_id)).all())
        abertos = dict(db.execute(select(Lead.dono_id, func.count()).where(
            Lead.etapa.not_in(("cliente", "perdido"))).group_by(Lead.dono_id)).all())
        atrasadas = dict(db.execute(select(LeadTarefa.dono_id, func.count()).where(
            LeadTarefa.feita_em.is_(None), LeadTarefa.quando < _agora()).group_by(LeadTarefa.dono_id)).all())
        resp["equipe"] = [{"nome": x.nome, "chats": sum(1 for c in convs if c["atendente_id"] == x.id),
                           "atividades_hoje": ativ.get(x.id, 0), "leads_abertos": abertos.get(x.id, 0),
                           "tarefas_atrasadas": atrasadas.get(x.id, 0),
                           "ultima": regras.para_local(u.empresa, ultima[x.id]).strftime("%d/%m %H:%M")
                           if ultima.get(x.id) else ""}
                          for x in equipe(db)]
        resp["fila"] = abertos.get(None, 0)
    return resp


def _conversa(db: Session, cid: int) -> ConversaVenda | None:
    return db.get(ConversaVenda, cid)


@rota.post("/master/crm/conversa/{cid}/assumir", dependencies=CSRF)
def assumir(cid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    c = _conversa(db, cid)
    if not c:
        return JSONResponse({"erro": "Conversa não encontrada."}, status_code=404)
    if c.atendente_id and c.atendente_id != u.id and not supervisor(u):
        return JSONResponse({"erro": "Outro atendente já está nesta conversa."}, status_code=409)
    c.atendente_id, c.atendente_em = u.id, _agora()
    msgs = json.loads(c.mensagens or "[]")
    msgs.append({"papel": "sistema", "texto": f"{u.primeiro_nome} entrou na conversa."})
    c.mensagens = json.dumps(msgs, ensure_ascii=False)
    c.atualizado_em = _agora()
    ld = db.scalar(select(Lead).where(Lead.conversa_id == c.id))
    if ld and ld.dono_id is None:
        ld.dono_id = u.id
    db.commit()
    return {"ok": True}


@rota.post("/master/crm/conversa/{cid}/devolver", dependencies=CSRF)
def devolver(cid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    c = _conversa(db, cid)
    if not c:
        return JSONResponse({"erro": "Conversa não encontrada."}, status_code=404)
    c.atendente_id = None
    msgs = json.loads(c.mensagens or "[]")
    msgs.append({"papel": "sistema", "texto": "O assistente voltou a responder."})
    c.mensagens = json.dumps(msgs, ensure_ascii=False)
    db.commit()
    return {"ok": True}


@rota.post("/master/crm/conversa/{cid}/mensagem", dependencies=CSRF)
async def responder(cid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    c = _conversa(db, cid)
    texto = re.sub(r"[ \t]+", " ", str((await request.json()).get("texto") or "")).strip()[:2000]
    if not c or not texto:
        return JSONResponse({"erro": "Escreva a mensagem."}, status_code=400)
    if c.atendente_id != u.id:
        if c.atendente_id and not supervisor(u):
            return JSONResponse({"erro": "Outro atendente está nesta conversa."}, status_code=409)
        c.atendente_id, c.atendente_em = u.id, _agora()
    msgs = json.loads(c.mensagens or "[]")
    msgs.append({"papel": "atendente", "texto": texto, "nome": u.primeiro_nome})
    c.mensagens = json.dumps(msgs, ensure_ascii=False)
    c.atualizado_em = _agora()
    db.commit()
    return {"ok": True}


@rota.get("/master/crm/{lid}")
def ficha(lid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    ld = _lead(db, u, lid)
    if not ld:
        avisar(request, "Lead não encontrado.", "erro")
        return ir("/master/crm")
    ativ = list(db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == ld.id)
                           .order_by(LeadAtividade.criado_em.desc()).limit(300)))
    tar = list(db.scalars(select(LeadTarefa).where(LeadTarefa.lead_id == ld.id)
                          .order_by(LeadTarefa.feita_em.is_not(None), LeadTarefa.quando)))
    conv = db.get(ConversaVenda, ld.conversa_id) if ld.conversa_id else None
    time = equipe(db)
    nomes = {x.id: x.nome for x in time}
    marca = plataforma()["nome"]
    msg = f"Olá{(' ' + ld.nome.split()[0]) if ld.nome else ''}! Aqui é {u.primeiro_nome}, do {marca}."
    zap = f"https://wa.me/55{ld.telefone}?text={quote(msg)}" if ld.telefone else ""
    emp = db.get(Empresa, ld.empresa_id) if ld.empresa_id else None
    from .db import CaixaEmail
    emails = list(db.scalars(select(CaixaEmail).where(CaixaEmail.lead_id == ld.id)
                             .order_by(CaixaEmail.data.desc()).limit(30)))
    return render(request, "master/crm_lead.html", u=u, e=u.empresa, aba="master", sub="crm", ld=ld,
                  ativ=ativ, tar=tar, conv=conv, msgs=json.loads(conv.mensagens or "[]") if conv else [],
                  time=time, nomes=nomes, zap=zap, emp=emp, ETAPAS=ETAPAS, ORIGENS=ORIGENS, MANUAIS=MANUAIS, TIPOS=TIPOS,
                  supervisor=supervisor(u), agora=_agora(), emails=emails, wa_envia=whatsapp.envia(),
                  wa_tem=bool(db.scalar(select(func.count(WhatsMensagem.id)).where(WhatsMensagem.lead_id == ld.id))))


@rota.post("/master/crm/{lid}", dependencies=CSRF)
def salvar(lid: int, request: Request, nome: str = Form(""), empresa_nome: str = Form(""),
           telefone: str = Form(""), email: str = Form(""), funcionarios: int = Form(0),
           valor: str = Form(""), dono_id: str = Form(""), instagram: str = Form(""), origem: str = Form(""),
           u: Usuario = Depends(admin_logado),
           db: Session = Depends(get_db)):
    ld = _lead(db, u, lid)
    if not ld:
        return ir("/master/crm")
    tel = celular_limpo(telefone)
    if telefone and not celular_valido(tel):
        avisar(request, "Confira o telefone com DDD.", "erro")
        return ir(f"/master/crm/{lid}")
    ig = arroba(instagram)
    if instagram.strip() and not ig:
        avisar(request, "Confira o @ do Instagram (só letras, números, ponto e sublinhado).", "erro")
        return ir(f"/master/crm/{lid}")
    if ig and ig != ld.instagram:
        outro = _achar(db, instagram=ig)
        if outro and outro.id != ld.id:
            avisar(request, "Já existe outro lead com este Instagram.", "erro")
            return ir(f"/master/crm/{lid}")
    ld.instagram = ig
    if origem in ORIGENS and origem != ld.origem:
        registrar(db, ld, f"Origem: {ORIGENS.get(ld.origem, ld.origem)} → {ORIGENS[origem]}.", "sistema", u)
        ld.origem = origem
    ld.nome, ld.empresa_nome, ld.telefone = nome.strip()[:80], empresa_nome.strip()[:80], tel
    ld.email, ld.funcionarios = email.strip()[:120], max(0, min(funcionarios, 100000))
    try:
        ld.valor_centavos = max(0, int(round(float(valor.replace(".", "").replace(",", ".") or 0) * 100)))
    except ValueError:
        pass
    if supervisor(u) and dono_id != "":
        novo_dono = int(dono_id) if dono_id.isdigit() else None
        if novo_dono != ld.dono_id:
            ld.dono_id = novo_dono
            tarefas_sem_dono_para(db, ld)
            nome_dono = db.get(Usuario, novo_dono).nome if novo_dono else "a fila"
            registrar(db, ld, f"Passado para {nome_dono} por {u.nome}.", "sistema", u)
    ld.atualizado_em = _agora()
    db.commit()
    avisar(request, "Lead salvo.")
    return ir(f"/master/crm/{lid}")


@rota.post("/master/crm/{lid}/atividade", dependencies=CSRF)
def atividade(lid: int, request: Request, tipo: str = Form("nota"), texto: str = Form(""),
              u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    ld = _lead(db, u, lid)
    if ld and texto.strip():
        if ld.dono_id is None:
            ld.dono_id = u.id
        registrar(db, ld, texto.strip(), tipo if tipo in ("nota", "ligacao", "whatsapp", "email") else "nota", u)
        if ld.etapa == "novo" and tipo in ("ligacao", "whatsapp", "email"):
            mudar_etapa(db, ld, "contato", u)
        db.commit()
    return ir(f"/master/crm/{lid}#historico")


@rota.post("/master/crm/{lid}/tarefa", dependencies=CSRF)
def nova_tarefa(lid: int, request: Request, titulo: str = Form(""), quando: str = Form(""),
                u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    ld = _lead(db, u, lid)
    try:
        dt = regras.para_utc(u.empresa, datetime.fromisoformat(quando))
    except ValueError:
        dt = None
    if not ld or not titulo.strip() or not dt:
        avisar(request, "Informe o que fazer e a data e hora do retorno.", "erro")
        return ir(f"/master/crm/{lid}")
    db.add(LeadTarefa(lead_id=ld.id, dono_id=ld.dono_id or u.id, titulo=titulo.strip()[:120], quando=dt,
                      criado_em=_agora()))
    registrar(db, ld, f"Tarefa marcada para {regras.para_local(u.empresa, dt):%d/%m %H:%M}: {titulo.strip()[:120]}",
              "sistema", u)
    db.commit()
    return ir(f"/master/crm/{lid}")


@rota.post("/master/crm/tarefa/{tid}/feita", dependencies=CSRF)
def tarefa_feita(tid: int, request: Request, volta: str = Form(""), u: Usuario = Depends(admin_logado),
                 db: Session = Depends(get_db)):
    t = db.get(LeadTarefa, tid)
    ld = _lead(db, u, t.lead_id) if t else None
    if t and ld and not t.feita_em:
        t.feita_em = _agora()
        registrar(db, ld, f"Tarefa feita: {t.titulo}", "sistema", u)
        db.commit()
    return ir(volta if volta.startswith("/master/") else (f"/master/crm/{t.lead_id}" if t else "/master/crm"))


# ------------------------------------------------------------------ equipe do Master
def _ag(db: Session, u: Usuario):
    return empresa_plataforma_id(db) or u.empresa_id


@rota.get("/master/equipe")
def equipe_tela(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    pid = _ag(db, u)
    gente = [x for x in db.scalars(select(Usuario).where(Usuario.empresa_id == pid).order_by(Usuario.nome))
             if x.acesso_master]
    return render(request, "master/equipe.html", u=u, e=u.empresa, aba="master", sub="equipe", gente=gente,
                  AREAS=MASTER_AREAS, link=request.session.pop("convite_link", ""))


def _areas_do_form(form) -> str:
    marcadas = {a for a in MASTER_AREAS if form.get(f"area_{a}")}
    if "supervisao" in marcadas:
        marcadas.add("crm")   # quem supervisiona o CRM também usa o CRM
    return ",".join(a for a in MASTER_AREAS if a in marcadas)


@rota.post("/master/equipe", dependencies=CSRF)
async def equipe_nova(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    form = await request.form()
    nome = str(form.get("nome", "")).strip()[:80]
    email = str(form.get("email", "")).strip().lower()[:200]
    areas = _areas_do_form(form)
    if not nome or not email_valido(email) or not areas:
        avisar(request, "Informe o nome, um e-mail válido e pelo menos uma área.", "erro")
        return ir("/master/equipe")
    ja = db.scalar(select(Usuario).where(func.lower(Usuario.email) == email))
    pid = _ag(db, u)
    if ja and ja.empresa_id != pid:
        avisar(request, "Este e-mail já é usado por uma conta de cliente. Use outro e-mail para a equipe.", "erro")
        return ir("/master/equipe")
    alvo = ja or Usuario(empresa_id=pid, nome=nome, email=email, papel="funcionario",
                         na_escala=bool(form.get("na_escala")))
    if not ja:
        db.add(alvo)
    alvo.master_areas, alvo.ativo = areas, True
    if not alvo.senha_hash:
        alvo.convite_token = secrets.token_urlsafe(24)
    db.commit()
    if alvo.convite_token:
        request.session["convite_link"] = f"{str(request.base_url).rstrip('/')}/convite/{alvo.convite_token}"
    avisar(request, f"{nome} adicionado(a) à equipe do Master.")
    return ir("/master/equipe")


@rota.post("/master/equipe/{uid}", dependencies=CSRF)
async def equipe_editar(uid: int, request: Request, u: Usuario = Depends(admin_logado),
                        db: Session = Depends(get_db)):
    form = await request.form()
    alvo = db.get(Usuario, uid)
    if not alvo or alvo.empresa_id != _ag(db, u) or alvo.is_admin:
        avisar(request, "Não é possível mudar o acesso desta pessoa.", "erro")
        return ir("/master/equipe")
    alvo.master_areas = "" if form.get("remover") else _areas_do_form(form)
    if not alvo.master_areas:
        for ld in db.scalars(select(Lead).where(Lead.dono_id == alvo.id, Lead.etapa.not_in(("cliente", "perdido")))):
            ld.dono_id = None   # os leads abertos voltam para a fila
            registrar(db, ld, f"Voltou para a fila: {alvo.nome} saiu da equipe.", "sistema", u)
    db.commit()
    avisar(request, "Acesso atualizado." if alvo.master_areas else f"{alvo.nome} não acessa mais o Master.")
    return ir("/master/equipe")
