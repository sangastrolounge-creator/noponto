"""Disponibilidade da equipe: no app, o funcionário diz em quais dias e horários pode trabalhar.
Na escala, o gestor vê a disponibilidade de cada pessoa e um alerta quando marca um turno fora dela."""
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import push
from . import db as banco
from .core import HORA_RE, avisar, exigir_csrf, ir, usuario_logado
from .db import Disponibilidade, Turno, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]


def _m(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def do_usuario(db: Session, uid: int) -> dict[int, Disponibilidade]:
    return {d.dia_semana: d for d in db.scalars(select(Disponibilidade).where(Disponibilidade.usuario_id == uid))}


def da_empresa(db: Session, empresa_id: int) -> dict[tuple[int, int], Disponibilidade]:
    return {(d.usuario_id, d.dia_semana): d for d in db.scalars(select(Disponibilidade).where(
        Disponibilidade.empresa_id == empresa_id))}


def fora(t: Turno | None, d: Disponibilidade | None) -> bool:
    """O turno cai fora da disponibilidade da pessoa?"""
    if not t or t.folga or not d or not t.inicio or not t.fim:
        return False
    if d.tipo == "nao":
        return True
    a, b = _m(t.inicio), _m(t.fim)
    if b <= a:
        b += 1440
    x, y = _m(d.inicio), _m(d.fim)
    if y <= x:
        y += 1440
    return not (x <= a and b <= y)


@rota.post("/app/disponibilidade", dependencies=CSRF)
async def salvar(request: Request, tarefas_bg: BackgroundTasks, u: Usuario = Depends(usuario_logado),
                 db: Session = Depends(get_db)):
    form = await request.form()
    atuais = do_usuario(db, u.id)
    mudou = False
    for dia in range(7):
        tipo = str(form.get(f"tipo_{dia}", "livre"))
        ini = str(form.get(f"ini_{dia}", "")).strip()
        fim = str(form.get(f"fim_{dia}", "")).strip()
        obs = str(form.get(f"obs_{dia}", "")).strip()[:120]
        d = atuais.get(dia)
        if tipo == "parcial" and not (HORA_RE.match(ini) and HORA_RE.match(fim) and ini != fim):
            avisar(request, "Informe o horário de início e de fim nos dias marcados como “Só neste horário”.", "erro")
            return ir("/app/escala#disponibilidade")
        if tipo not in ("parcial", "nao"):
            if d:
                db.delete(d)
                mudou = True
            continue
        if not d:
            d = Disponibilidade(empresa_id=u.empresa_id, usuario_id=u.id, dia_semana=dia)
            db.add(d)
        novo = (tipo, ini if tipo == "parcial" else "", fim if tipo == "parcial" else "", obs)
        if (d.tipo, d.inicio, d.fim, d.obs) != novo:
            d.tipo, d.inicio, d.fim, d.obs = novo
            d.atualizado_em = banco.agora()
            mudou = True
    db.commit()
    if mudou:
        tarefas_bg.add_task(push.avisar_gestores, u.empresa_id, f"Disponibilidade: {u.primeiro_nome}",
                            f"{u.nome} mudou os dias e horários em que pode trabalhar.", "/painel/escala", "escala")
    avisar(request, "Disponibilidade salva. O gestor vê na escala.")
    return ir("/app/escala#disponibilidade")
