"""Descanso entre jornadas (interjornada) pela CLT, art. 66: entre o fim de um turno e o início
do seguinte, no mínimo 11 horas de descanso. A grade da escala marca os turnos que começam antes
disso e o gestor recebe o aviso ao salvar.

(O nome do arquivo vem da versão americana, onde estava a lei de "escala justa"; aqui só ficou a
regra brasileira do descanso. As outras funções existem para a tela da escala continuar igual.)"""
from datetime import date, timedelta

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import regras
from .db import Turno

INTERJORNADA_H = 11
rota = APIRouter()


def regra(e) -> dict | None:
    return None


def situacao_semana(db: Session, e, seg: date) -> dict | None:
    """Sem lei de publicação antecipada no Brasil: a tela não mostra o aviso de prazo."""
    return None


def _valor(t: Turno | None) -> str:
    if not t:
        return ""
    return "F" if t.folga else f"{t.inicio}-{t.fim}"


def antes_de_mudar(db: Session, uid: int, dias: list[date]) -> dict:
    """Como estava cada dia antes de salvar (para pedir nova confirmação de quem teve o turno mudado)."""
    return {d: _valor(db.scalar(select(Turno).where(Turno.usuario_id == uid, Turno.data == d))) for d in dias}


def avaliar_mudancas(db: Session, e, uid: int, dias: list[date], antes: dict, autor: str,
                     a_pedido: bool = False) -> list:
    return []


def premios_por_dia(db: Session, e, ini: date, fim: date) -> dict:
    return {}


def descansos_curtos_na_grade(e, turnos: dict, equipe, dias: list[date]) -> set[tuple[int, date]]:
    """Dias da grade em que o turno começa menos de 11 h depois do fim do turno anterior."""
    minimo = timedelta(hours=INTERJORNADA_H)
    marcados = set()
    for f in equipe:
        for d in dias:
            t, ontem = turnos.get((f.id, d)), turnos.get((f.id, d - timedelta(days=1)))
            if t and ontem and not t.folga and not ontem.folga and t.inicio and ontem.fim:
                if regras.intervalo(t)[0] - regras.intervalo(ontem)[1] < minimo:
                    marcados.add((f.id, d))
    return marcados


def curtos_ao_salvar(db: Session, uid: int, dias: list[date]) -> list[date]:
    """Dias (entre os salvos e o seguinte de cada um) com descanso menor que 11 h."""
    datas = sorted({x for d in dias for x in (d - timedelta(days=1), d, d + timedelta(days=1))})
    turnos = {t.data: t for t in db.scalars(select(Turno).where(Turno.usuario_id == uid,
                                                                 Turno.data.between(datas[0], datas[-1])))}
    marcados = descansos_curtos_na_grade(None, {(uid, d): t for d, t in turnos.items()},
                                         [type("P", (), {"id": uid})()], datas)
    return sorted(d for _, d in marcados)
