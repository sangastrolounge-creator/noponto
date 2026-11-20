"""Aviso perto da hora extra (regra brasileira: a hora extra é o que passa da jornada do dia).

Enquanto a pessoa trabalha, o sistema compara o que ela já trabalhou hoje com a jornada prevista
na escala do dia (turno menos intervalo) e avisa o gestor:
1. quando faltam X minutos para acabar a jornada (empresas.aviso_extra_min);
2. quando ela passa da jornada mais a tolerância do dia e entra em hora extra;
3. quando passa de 2 horas extras no dia, o limite da CLT (art. 59).
Um aviso de cada tipo por pessoa por dia. Não vale para PJ nem para quem está sem escala no dia
(dia sem escala já gera o aviso "sem escala")."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import regras
from . import db as banco
from .db import Alerta, Batida, Empresa, Turno, Usuario

LIMITE_DIA_MIN = 120   # CLT art. 59: até 2 horas extras por dia


def _fmt(m: int) -> str:
    return regras.fmt_min(max(0, m))


def verificar(db: Session) -> list[Alerta]:
    """Chamado a cada minuto. Devolve os alertas novos (o agendador manda o aviso)."""
    from .core import alertas_liberados
    from .custos import minutos_trabalhados
    novos: list[Alerta] = []
    agora = banco.agora()
    for e in db.scalars(select(Empresa).where(Empresa.ativo.is_(True))):
        if not alertas_liberados(e):
            continue
        abertas = {b.usuario_id: b for b in db.scalars(select(Batida).where(
            Batida.empresa_id == e.id, Batida.tipo == "entrada", regras.VALIDA,
            Batida.momento >= agora - timedelta(hours=18)))}
        if not abertas:
            continue
        cache: dict = {}
        for uid in abertas:
            u = db.get(Usuario, uid)
            if not u or (u.classificacao or "clt") == "pj":
                continue
            aberta = regras.ponto_aberto(db, u)
            if aberta is None:
                continue
            dia = aberta.dia
            t = db.scalar(select(Turno).where(Turno.usuario_id == uid, Turno.data == dia))
            if not t or t.folga:
                continue
            if dia not in cache:
                cache[dia] = minutos_trabalhados(db, e, dia)
            m = cache[dia].get(uid, 0)
            previsto = regras.minutos_turno(t)
            if not previsto:
                continue
            desde = agora - timedelta(hours=20)
            ja = {a.tipo for a in db.scalars(select(Alerta).where(
                Alerta.usuario_id == uid, Alerta.tipo.in_(("perto_extra", "hora_extra", "limite_extra")),
                Alerta.criado_em >= desde))}
            novo = None
            if m > previsto + LIMITE_DIA_MIN and "limite_extra" not in ja:
                novo = ("limite_extra", f"{u.nome} já fez {_fmt(m - previsto)} de hora extra hoje: "
                                        "a CLT permite até 2 horas por dia.")
            elif m > previsto + (e.tolerancia_dia_min or 0) and not ja & {"hora_extra", "limite_extra"}:
                novo = ("hora_extra", f"{u.nome} passou da jornada de hoje ({_fmt(previsto)}) e está em hora extra.")
            elif (e.aviso_extra_min and previsto - e.aviso_extra_min <= m <= previsto
                  and not ja):
                novo = ("perto_extra", f"{u.nome} está com {_fmt(m)} hoje: a jornada termina daqui a "
                                       f"{_fmt(previsto - m)}. Depois disso é hora extra.")
            if novo:
                a = Alerta(empresa_id=e.id, usuario_id=uid, tipo=novo[0], criado_em=agora,
                           texto=novo[1], turno_id=t.id)
                db.add(a)
                novos.append(a)
    if novos:
        db.commit()
    return novos
