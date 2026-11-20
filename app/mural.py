"""Mural da equipe: avisos do gestor (com notificação no celular) e recados dos funcionários."""
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import push, regras
from . import db as banco
from .core import avisar, dentro_do_limite, exigir_csrf, gestor_logado, ir, render, usuario_logado
from .db import LeituraMensagem, Mensagem, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MAX_TEXTO = 1000


def novas(u: Usuario) -> int:
    """Mensagens de outras pessoas desde a última visita ao mural (para o selo do menu)."""
    with SessionLocal() as db:
        filtros = [Mensagem.empresa_id == u.empresa_id, Mensagem.autor_id != u.id]
        if u.mural_visto_em:
            filtros.append(Mensagem.criado_em > u.mural_visto_em)
        return db.scalar(select(func.count()).select_from(Mensagem).where(*filtros)) or 0


def _mensagens(db: Session, empresa_id: int) -> list[Mensagem]:
    return list(db.scalars(select(Mensagem).where(Mensagem.empresa_id == empresa_id)
                           .order_by(Mensagem.fixada.desc(), Mensagem.criado_em.desc()).limit(100)))


def marcar_lidas(db: Session, u: Usuario, lista: list[Mensagem]) -> None:
    ja = set(db.scalars(select(LeituraMensagem.mensagem_id).where(
        LeituraMensagem.usuario_id == u.id, LeituraMensagem.mensagem_id.in_([m.id for m in lista]))))
    for m in lista:
        if m.autor_id != u.id and m.id not in ja:
            db.add(LeituraMensagem(mensagem_id=m.id, usuario_id=u.id, lida_em=banco.agora()))


def leituras(db: Session, empresa_id: int, lista: list[Mensagem]) -> dict:
    """Para as mensagens dos gestores: {id: {"lidas", "total", "faltam": [nomes]}}.
    Conta a equipe ativa (funcionários)."""
    equipe = list(db.scalars(select(Usuario).where(Usuario.empresa_id == empresa_id, Usuario.ativo.is_(True),
                                                   Usuario.papel == "funcionario")))
    ids = [m.id for m in lista if m.autor and m.autor.gestor]
    lidas: dict[int, set] = {}
    for mid, uid in db.execute(select(LeituraMensagem.mensagem_id, LeituraMensagem.usuario_id).where(
            LeituraMensagem.mensagem_id.in_(ids))):
        lidas.setdefault(mid, set()).add(uid)
    sai = {}
    for m in lista:
        if m.id not in ids:
            continue
        alvo = [f for f in equipe if f.id != m.autor_id]
        ok = lidas.get(m.id, set())
        sai[m.id] = {"lidas": sum(1 for f in alvo if f.id in ok), "total": len(alvo),
                     "faltam": [f.nome for f in alvo if f.id not in ok]}
    return sai


def _ver(request: Request, u: Usuario, db: Session, modelo: str, aba: str):
    lista = _mensagens(db, u.empresa_id)
    visto = u.mural_visto_em
    u.mural_visto_em = banco.agora()
    marcar_lidas(db, u, lista)
    db.commit()
    return render(request, modelo, u=u, e=u.empresa, aba=aba, mensagens=lista, visto=visto,
                  pode_postar=u.gestor or u.empresa.mural_equipe,
                  leituras=leituras(db, u.empresa_id, lista) if u.gestor else {})


def _publicar(request: Request, tarefas_bg: BackgroundTasks, u: Usuario, db: Session,
              texto: str, fixar: bool, avisar_equipe: bool) -> None:
    texto = texto.strip()[:MAX_TEXTO]
    if not texto:
        avisar(request, "Escreva a mensagem.", "erro")
        return
    if not dentro_do_limite(f"mural:{u.id}", 20, 3600):
        avisar(request, "Muitas mensagens seguidas. Aguarde um pouco.", "erro")
        return
    db.add(Mensagem(empresa_id=u.empresa_id, autor_id=u.id, texto=texto, fixada=fixar,
                    criado_em=banco.agora()))
    u.mural_visto_em = banco.agora()
    db.commit()
    resumo = texto if len(texto) <= 120 else texto[:117] + "..."
    if avisar_equipe:
        ids = [f.id for f in db.scalars(select(Usuario).where(
            Usuario.empresa_id == u.empresa_id, Usuario.ativo.is_(True), Usuario.id != u.id,
            Usuario.papel == "funcionario"))]
        tarefas_bg.add_task(push.avisar_usuarios,
                            [(i, f"Aviso de {u.primeiro_nome}", resumo, "/app/mural") for i in ids])
    if not u.gestor:
        tarefas_bg.add_task(push.avisar_gestores, u.empresa_id,
                            f"Mural: {u.primeiro_nome}", resumo, "/painel/mural", "mural")
    avisar(request, "Mensagem publicada.")


# ------------------------------------------------------------------ gestor
@rota.get("/painel/mural")
def mural_painel(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    return _ver(request, u, db, "painel_mural.html", "mural")


@rota.post("/painel/mural", dependencies=CSRF)
def mural_painel_novo(request: Request, tarefas_bg: BackgroundTasks, texto: str = Form(""),
                      fixar: str = Form(""), notificar: str = Form(""),
                      u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    _publicar(request, tarefas_bg, u, db, texto, bool(fixar), bool(notificar))
    return ir("/painel/mural")


@rota.post("/painel/mural/regra", dependencies=CSRF)
def mural_regra(request: Request, equipe: str = Form(""), u: Usuario = Depends(gestor_logado),
                db: Session = Depends(get_db)):
    u.empresa.mural_equipe = bool(equipe)
    regras.registrar(db, u.empresa_id, u.nome, "regra do mural",
                     "Funcionários podem publicar no mural." if equipe
                     else "Funcionários não podem publicar no mural.")
    db.commit()
    avisar(request, "Regra do mural salva.")
    return ir("/painel/mural")


@rota.post("/painel/mural/{mid}", dependencies=CSRF)
def mural_painel_acao(request: Request, mid: int, acao: str = Form(...),
                      u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    m = db.get(Mensagem, mid)
    if m and m.empresa_id == u.empresa_id:
        if acao == "apagar":
            regras.registrar(db, u.empresa_id, u.nome, "mensagem apagada do mural",
                             f"De {m.autor.nome}: {m.texto[:200]}")
            for x in db.scalars(select(LeituraMensagem).where(LeituraMensagem.mensagem_id == m.id)):
                db.delete(x)
            db.delete(m)
        elif acao in ("fixar", "soltar"):
            m.fixada = acao == "fixar"
        db.commit()
    return ir("/painel/mural")


# ------------------------------------------------------------------ funcionário
@rota.get("/app/mural")
def mural_app(request: Request, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    return _ver(request, u, db, "app_mural.html", "mural")


@rota.post("/app/mural", dependencies=CSRF)
def mural_app_novo(request: Request, tarefas_bg: BackgroundTasks, texto: str = Form(""),
                   u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    if not (u.gestor or u.empresa.mural_equipe):
        avisar(request, "Só o gestor publica no mural desta empresa.", "erro")
    else:
        _publicar(request, tarefas_bg, u, db, texto, False, False)
    return ir("/app/mural")


@rota.post("/app/mural/{mid}/apagar", dependencies=CSRF)
def mural_app_apagar(request: Request, mid: int, u: Usuario = Depends(usuario_logado),
                     db: Session = Depends(get_db)):
    m = db.get(Mensagem, mid)
    if m and m.empresa_id == u.empresa_id and m.autor_id == u.id:
        for x in db.scalars(select(LeituraMensagem).where(LeituraMensagem.mensagem_id == m.id)):
            db.delete(x)
        db.delete(m)
        db.commit()
    return ir("/app/mural")
