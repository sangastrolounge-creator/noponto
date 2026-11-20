"""Avaliação do turno: depois de bater a saída, o funcionário dá uma nota de 1 a 5 ao dia
(e um comentário, se quiser). Notas baixas avisam o gestor. Médias saem nos relatórios."""
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import push, regras
from . import db as banco
from .core import avisar, exigir_csrf, gestor_logado, ir, usuario_logado
from .db import AvaliacaoTurno, Batida, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]


def pendente(db: Session, u: Usuario):
    """Dia de trabalho que a pessoa acabou de encerrar e ainda não avaliou (ou None)."""
    if not u.empresa.avaliar_turno:
        return None
    b = regras.ultima_batida(db, u)
    if not b or b.tipo != "saida" or banco.agora() - b.momento > timedelta(hours=20):
        return None
    if db.scalar(select(AvaliacaoTurno.id).where(AvaliacaoTurno.usuario_id == u.id,
                                                 AvaliacaoTurno.dia == b.dia)):
        return None
    return b.dia


@rota.post("/app/avaliar", dependencies=CSRF)
def avaliar(request: Request, tarefas_bg: BackgroundTasks, nota: str = Form(""), comentario: str = Form(""),
            u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    dia = pendente(db, u)
    if dia is None:
        return ir("/app")
    if nota == "pular":
        n = 0
    elif nota.isdigit() and 1 <= int(nota) <= 5:
        n = int(nota)
    else:
        avisar(request, "Escolha de 1 a 5 estrelas.", "erro")
        return ir("/app")
    texto = comentario.strip()[:300]
    db.add(AvaliacaoTurno(empresa_id=u.empresa_id, usuario_id=u.id, dia=dia, nota=n, comentario=texto,
                          criado_em=banco.agora()))
    db.commit()
    if n and n <= 2:
        tarefas_bg.add_task(push.avisar_gestores, u.empresa_id, f"Turno difícil: {u.primeiro_nome}",
                            f"{u.nome} deu {n} de 5 ao turno de {dia:%d/%m}." + (f" “{texto[:100]}”" if texto else ""),
                            "/painel/relatorios", "escala")
    if n:
        avisar(request, "Obrigado! Sua avaliação foi enviada.")
    return ir("/app")


@rota.post("/painel/config/avaliar-turno", dependencies=CSRF)
def regra(request: Request, ligado: str = Form(""), db: Session = Depends(get_db),
          u: Usuario = Depends(gestor_logado)):
    u.empresa.avaliar_turno = bool(ligado)
    regras.registrar(db, u.empresa_id, u.nome, "avaliação do turno",
                     "Ligada: o funcionário avalia o turno ao sair." if ligado else "Desligada.")
    db.commit()
    avisar(request, "Regra salva.")
    return ir("/painel/relatorios")
