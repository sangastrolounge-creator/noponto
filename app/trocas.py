"""Turnos oferecidos: o gestor publica um turno aberto (sem dono) ou o funcionário repassa o
próprio turno. Outro funcionário pede, o gestor aprova (ou não, se a empresa dispensar a
aprovação) e o turno passa a ser dele."""
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import apuracao, push, regras
from . import db as banco
from .core import (HORA_RE, avisar, da_empresa, data_iso, exigir_csrf, gestor_logado, ir,
                   usuario_logado)
from .db import Ausencia, Batida, Empresa, Local, Oferta, Turno, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
ATIVAS = ("aberta", "pedida")
SITUACOES = {"aberta": "Aberta", "pedida": "Aguardando aprovação", "aprovada": "Aprovada",
             "recusada": "Recusada", "cancelada": "Cancelada"}


# ------------------------------------------------------------------ regras
def _como_turno(o: Oferta, usuario_id: int) -> Turno:
    """Turno de mentira com os horários da oferta (para calcular minutos)."""
    t = Turno(empresa_id=o.empresa_id, usuario_id=usuario_id, data=o.data, inicio=o.inicio,
              fim=o.fim, folga=False, intervalo_min=o.intervalo_min)
    return t


def minutos(o: Oferta) -> int:
    return regras.minutos_turno(_como_turno(o, 0))


def ja_comecou(e: Empresa, o_data: date, inicio: str) -> bool:
    agora = regras.agora_local(e)
    return o_data < agora.date() or (o_data == agora.date() and inicio <= agora.strftime("%H:%M"))


def cargo_serve(o: Oferta, u: Usuario) -> bool:
    if not o.cargo:
        return True
    from .ficha import nomes_de_funcao
    from sqlalchemy.orm import object_session
    db = object_session(u)
    nomes = nomes_de_funcao(db, u) if db else {(u.cargo or "").strip().lower()}
    return o.cargo.strip().lower() in nomes


def impedimento(db: Session, e: Empresa, o: Oferta, u: Usuario) -> str | None:
    """Por que essa pessoa não pode ficar com o turno (None = pode)."""
    if not u.ativo or not u.na_escala or u.empresa_id != e.id:
        return "Esta pessoa não está na escala."
    if o.de_id == u.id:
        return "O turno já é seu."
    if not cargo_serve(o, u):
        return f"Este turno é para o cargo {o.cargo}."
    if ja_comecou(e, o.data, o.inicio):
        return "Este turno já começou ou já passou."
    if apuracao.esta_fechado(db, e, o.data):
        return "Este dia está num período de ponto fechado."
    regras.gerar_da_fixa(db, e, o.data, o.data)
    t = db.scalar(select(Turno).where(Turno.usuario_id == u.id, Turno.data == o.data))
    if t and not t.folga:
        return f"{u.primeiro_nome} já trabalha neste dia ({t.inicio} às {t.fim})."
    if db.scalar(select(Ausencia.id).where(
            Ausencia.usuario_id == u.id, Ausencia.situacao == "aprovado",
            Ausencia.inicio <= o.data, Ausencia.fim >= o.data).limit(1)):
        return f"{u.primeiro_nome} tem uma ausência aprovada neste dia."
    if o.tipo == "repasse":
        t = db.get(Turno, o.turno_id) if o.turno_id else None
        if not t or t.usuario_id != o.de_id or t.folga:
            return "O turno original mudou na escala. Peça para oferecer de novo."
        if db.scalar(select(Batida.id).where(Batida.turno_id == t.id).limit(1)):
            return "O turno original já tem ponto batido."
    return None


def aviso_extra(db: Session, e: Empresa, o: Oferta, u: Usuario) -> dict:
    """Avisa o gestor se, com este turno, a pessoa passa do limite de horas da semana."""
    ini = apuracao.inicio_semana(e, o.data)
    fim = ini + timedelta(days=6)
    regras.gerar_da_fixa(db, e, ini, fim)
    ja = sum(regras.minutos_turno(t) for t in db.scalars(select(Turno).where(
        Turno.usuario_id == u.id, Turno.data.between(ini, fim))))
    total = ja + minutos(o)
    if total > apuracao.SEMANA_CLT_MIN:
        return {"nome": u.primeiro_nome, "total": regras.fmt_min(total),
                "extra": regras.fmt_min(total - apuracao.SEMANA_CLT_MIN)}
    return {}


def aplicar(db: Session, e: Empresa, o: Oferta, autor: str) -> str | None:
    """Passa o turno para o candidato. Devolve o erro, se houver. Quem chama faz o commit."""
    u = o.candidato
    erro = impedimento(db, e, o, u) if u else "Ninguém pediu este turno."
    if erro:
        return erro
    atual = db.scalar(select(Turno).where(Turno.usuario_id == u.id, Turno.data == o.data))
    if o.tipo == "repasse":
        t = db.get(Turno, o.turno_id)
        if atual:  # folga do candidato dá lugar ao turno
            db.delete(atual)
            db.flush()
        de = t.usuario
        t.usuario_id, t.fixa = u.id, False
        t.lembrou_ini = t.lembrou_fim = t.lembrou_int = False
        t.confirmado_em = banco.agora()   # quem pediu o turno já confirmou
        db.flush()
        db.add(Turno(empresa_id=e.id, usuario_id=de.id, data=o.data, folga=True,
                     obs=f"Turno repassado para {u.nome}"[:200]))
    else:
        if not atual:
            atual = Turno(empresa_id=e.id, usuario_id=u.id, data=o.data)
            db.add(atual)
        atual.folga, atual.inicio, atual.fim = False, o.inicio, o.fim
        atual.intervalo_min, atual.intervalo_inicio = o.intervalo_min, o.intervalo_inicio
        atual.local_id, atual.obs, atual.fixa = o.local_id, o.obs, False
        atual.confirmado_em = banco.agora()
    o.situacao, o.decidido_por, o.decidido_em = "aprovada", autor, banco.agora()
    regras.registrar(db, e.id, autor, "turno oferecido aprovado",
                     f"{o.data:%d/%m} {o.inicio}–{o.fim}: de {o.de.nome} para {u.nome}."
                     if o.tipo == "repasse" else
                     f"{o.data:%d/%m} {o.inicio}–{o.fim}: turno aberto para {u.nome}.")
    return None


def _rotulo(o: Oferta) -> str:
    return f"{o.data:%d/%m}, {o.inicio}–{o.fim}"


# ------------------------------------------------------------------ dados para as telas
def do_painel(db: Session, e: Empresa) -> list[Oferta]:
    hoje = regras.hoje(e)
    return list(db.scalars(select(Oferta).where(
        Oferta.empresa_id == e.id, Oferta.situacao.in_(ATIVAS), Oferta.data >= hoje)
        .order_by(Oferta.situacao.desc(), Oferta.data, Oferta.inicio)))


def avisos_extra(db: Session, e: Empresa, ofertas: list[Oferta]) -> dict[int, dict]:
    return {o.id: aviso_extra(db, e, o, o.candidato) for o in ofertas
            if o.situacao == "pedida" and o.candidato}


def do_app(db: Session, u: Usuario) -> dict:
    e, hoje = u.empresa, regras.hoje(u.empresa)
    ativas = list(db.scalars(select(Oferta).where(
        Oferta.empresa_id == e.id, Oferta.data >= hoje, Oferta.situacao.in_(ATIVAS))
        .order_by(Oferta.data, Oferta.inicio)))
    disponiveis = [o for o in ativas if o.situacao == "aberta" and o.de_id != u.id
                   and cargo_serve(o, u) and not ja_comecou(e, o.data, o.inicio)]
    meus_pedidos = [o for o in ativas if o.candidato_id == u.id]
    minhas = {o.turno_id: o for o in ativas if o.de_id == u.id and o.turno_id}
    return {"disponiveis": disponiveis, "meus_pedidos": meus_pedidos, "minhas": minhas}


# ------------------------------------------------------------------ gestor
@rota.post("/painel/ofertas", dependencies=CSRF)
def oferta_nova(request: Request, tarefas_bg: BackgroundTasks, data: str = Form(""),
                inicio: str = Form(""), fim: str = Form(""), intervalo: str = Form("0"),
                cargo: str = Form(""), local_id: str = Form(""), obs: str = Form(""),
                quantos: str = Form("1"), u: Usuario = Depends(gestor_logado),
                db: Session = Depends(get_db)):
    e = u.empresa
    d = data_iso(data, None)
    pausa = int(intervalo) if intervalo.strip().isdigit() else 0
    n = int(quantos) if quantos.strip().isdigit() else 1
    local = da_empresa(db, Local, int(local_id), u) if local_id.isdigit() else None
    volta = f"/painel/escala?semana={d.isoformat()}" if d else "/painel/escala"
    erro = None
    if not d or not (HORA_RE.match(inicio) and HORA_RE.match(fim)) or inicio == fim:
        erro = "Informe a data e o horário de início e de fim (ex.: 08:00 e 17:00)."
    elif ja_comecou(e, d, inicio):
        erro = "Escolha um turno que ainda não começou."
    elif apuracao.esta_fechado(db, e, d):
        erro = "Este dia está num período de ponto fechado."
    elif not 0 <= pausa <= 240 or not 1 <= n <= 10:
        erro = "O intervalo vai de 0 a 240 minutos, e dá para abrir de 1 a 10 vagas."
    if erro:
        avisar(request, erro, "erro")
        return ir(volta)
    for _ in range(n):
        db.add(Oferta(empresa_id=e.id, tipo="aberto", data=d, inicio=inicio, fim=fim,
                      intervalo_min=pausa, local_id=local.id if local else None,
                      cargo=cargo.strip()[:80], obs=obs.strip()[:200], criado_por=u.nome))
    regras.registrar(db, e.id, u.nome, "turno aberto publicado",
                     f"{n} vaga(s) em {d:%d/%m} {inicio}–{fim}, cargo {cargo.strip()}." if cargo.strip()
                     else f"{n} vaga(s) em {d:%d/%m} {inicio}–{fim}.")
    db.commit()
    quem = [f for f in db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True),
        Usuario.papel == "funcionario"))
        if not cargo.strip() or cargo_serve(Oferta(cargo=cargo.strip()), f)]
    texto = f"{d:%d/%m}, {inicio}–{fim}. Toque para pedir."
    tarefas_bg.add_task(push.avisar_usuarios,
                        [(f.id, "Turno disponível", texto, "/app/escala") for f in quem])
    avisar(request, f"Turno publicado. {len(quem)} pessoa(s) da equipe foram avisadas.")
    return ir(volta)


@rota.post("/painel/ofertas/regra", dependencies=CSRF)
def oferta_regra(request: Request, aprovacao: str = Form(""), u: Usuario = Depends(gestor_logado),
                 db: Session = Depends(get_db)):
    u.empresa.trocas_aprovacao = bool(aprovacao)
    regras.registrar(db, u.empresa_id, u.nome, "regra de trocas",
                     "Trocas de turno precisam de aprovação." if aprovacao
                     else "Trocas de turno valem sem aprovação.")
    db.commit()
    avisar(request, "Regra das trocas de turno salva.")
    return ir("/painel/escala")


@rota.post("/painel/ofertas/{oid}", dependencies=CSRF)
def oferta_decidir(request: Request, tarefas_bg: BackgroundTasks, oid: int,
                   acao: str = Form(...), u: Usuario = Depends(gestor_logado),
                   db: Session = Depends(get_db)):
    e = u.empresa
    o = da_empresa(db, Oferta, oid, u)
    volta = "/painel/escala"
    if o.situacao not in ATIVAS:
        avisar(request, "Esta oferta já foi resolvida.", "erro")
        return ir(volta)
    lembretes = []
    if acao == "aprovar" and o.situacao == "pedida":
        erro = aplicar(db, e, o, u.nome)
        if erro:
            avisar(request, erro, "erro")
            return ir(volta)
        lembretes.append((o.candidato_id, "Turno aprovado", f"O turno de {_rotulo(o)} agora é seu.", "/app/escala"))
        if o.de_id:
            lembretes.append((o.de_id, "Turno repassado", f"{o.candidato.nome} vai cobrir {_rotulo(o)}.", "/app/escala"))
        avisar(request, f"Aprovado: o turno agora é de {o.candidato.nome}.")
    elif acao == "recusar" and o.situacao == "pedida":
        lembretes.append((o.candidato_id, "Pedido de turno recusado",
                          f"O turno de {_rotulo(o)} continua com a escala atual.", "/app/escala"))
        regras.registrar(db, e.id, u.nome, "pedido de turno recusado",
                         f"{o.candidato.nome}, {_rotulo(o)}.")
        o.situacao, o.candidato_id, o.pedido_em = "aberta", None, None
        avisar(request, "Pedido recusado. O turno continua oferecido para a equipe.")
    elif acao == "cancelar":
        if o.candidato_id:
            lembretes.append((o.candidato_id, "Turno cancelado", f"O turno de {_rotulo(o)} não está mais disponível.", "/app/escala"))
        if o.de_id:
            lembretes.append((o.de_id, "Oferta cancelada", f"O seu turno de {_rotulo(o)} continua com você.", "/app/escala"))
        o.situacao, o.decidido_por, o.decidido_em = "cancelada", u.nome, banco.agora()
        regras.registrar(db, e.id, u.nome, "turno oferecido cancelado", _rotulo(o) + ".")
        avisar(request, "Oferta cancelada.")
    db.commit()
    tarefas_bg.add_task(push.avisar_usuarios, lembretes)
    return ir(volta)


# ------------------------------------------------------------------ funcionário
@rota.post("/app/turnos/{tid}/oferecer", dependencies=CSRF)
def oferecer(request: Request, tarefas_bg: BackgroundTasks, tid: int, obs: str = Form(""),
             u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    t = db.get(Turno, tid)
    volta = "/app/escala"
    erro = None
    if not t or t.usuario_id != u.id or t.folga:
        erro = "Turno não encontrado."
    elif ja_comecou(e, t.data, t.inicio):
        erro = "Só dá para oferecer um turno que ainda não começou."
    elif apuracao.esta_fechado(db, e, t.data):
        erro = "Este dia está num período de ponto fechado."
    elif db.scalar(select(Oferta.id).where(Oferta.turno_id == t.id, Oferta.situacao.in_(ATIVAS)).limit(1)):
        erro = "Este turno já está oferecido."
    if erro:
        avisar(request, erro, "erro")
        return ir(volta)
    db.add(Oferta(empresa_id=e.id, tipo="repasse", turno_id=t.id, de_id=u.id, data=t.data,
                  inicio=t.inicio, fim=t.fim, intervalo_min=t.intervalo_min,
                  intervalo_inicio=t.intervalo_inicio, local_id=t.local_id,
                  cargo=(u.cargo or "").strip()[:80], obs=obs.strip()[:200], criado_por=u.nome))
    regras.registrar(db, e.id, u.nome, "turno oferecido", f"{u.nome} ofereceu {t.data:%d/%m} {t.inicio}–{t.fim}.")
    db.commit()
    quem = [f.id for f in db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True),
        Usuario.papel == "funcionario", Usuario.id != u.id))
        if not u.cargo or (f.cargo or "").strip().lower() == u.cargo.strip().lower()]
    texto = f"{u.primeiro_nome} precisa de alguém para {t.data:%d/%m}, {t.inicio}–{t.fim}."
    tarefas_bg.add_task(push.avisar_usuarios, [(i, "Alguém pode cobrir?", texto, "/app/escala") for i in quem])
    avisar(request, "Turno oferecido. Ele continua seu até alguém pegar e o gestor aprovar.")
    return ir(volta)


@rota.post("/app/ofertas/{oid}", dependencies=CSRF)
def oferta_app(request: Request, tarefas_bg: BackgroundTasks, oid: int, acao: str = Form(...),
               u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    o = db.get(Oferta, oid)
    volta = "/app/escala"
    if not o or o.empresa_id != e.id or o.situacao not in ATIVAS:
        avisar(request, "Este turno não está mais disponível.", "erro")
        return ir(volta)
    if acao == "pedir" and o.situacao == "aberta":
        erro = impedimento(db, e, o, u)
        if erro:
            avisar(request, erro, "erro")
            return ir(volta)
        o.candidato_id, o.pedido_em, o.situacao = u.id, banco.agora(), "pedida"
        db.flush()
        db.refresh(o)
        if not e.trocas_aprovacao:
            erro = aplicar(db, e, o, "aprovação automática")
            if erro:
                db.rollback()
                avisar(request, erro, "erro")
                return ir(volta)
            db.commit()
            if o.de_id:
                tarefas_bg.add_task(push.avisar_usuarios, [(o.de_id, "Turno coberto", f"{u.nome} vai cobrir {_rotulo(o)}.", "/app/escala")])
            avisar(request, "Pronto: o turno agora é seu.")
            return ir(volta)
        regras.registrar(db, e.id, u.nome, "turno pedido", f"{u.nome} pediu {_rotulo(o)}.")
        db.commit()
        tarefas_bg.add_task(push.avisar_gestores, e.id, "Pedido de turno",
                            f"{u.nome} quer o turno de {_rotulo(o)}.", "/painel/escala", "escala")
        avisar(request, "Pedido enviado. Você recebe um aviso quando o gestor responder.")
    elif acao == "desistir" and o.candidato_id == u.id and o.situacao == "pedida":
        o.situacao, o.candidato_id, o.pedido_em = "aberta", None, None
        regras.registrar(db, e.id, u.nome, "pedido de turno retirado", f"{u.nome}, {_rotulo(o)}.")
        db.commit()
        avisar(request, "Pedido retirado.")
    elif acao == "cancelar" and o.de_id == u.id:
        if o.candidato_id:
            tarefas_bg.add_task(push.avisar_usuarios, [(o.candidato_id, "Turno cancelado", f"O turno de {_rotulo(o)} não está mais disponível.", "/app/escala")])
        o.situacao, o.decidido_por, o.decidido_em = "cancelada", u.nome, banco.agora()
        regras.registrar(db, e.id, u.nome, "oferta de turno cancelada", f"{u.nome}, {_rotulo(o)}.")
        db.commit()
        avisar(request, "Oferta cancelada. O turno continua seu.")
    return ir(volta)
