"""Saldos de sick leave e PTO (paid time off).

Cada funcionário tem dois saldos, guardados como extrato (MovimentoPTO):
- ganha 1 h a cada N horas trabalhadas, lançado quando o período de ponto é fechado
  (ex.: Califórnia, 1 h de sick leave a cada 30 h);
- usa as horas das ausências pagas do período (licença médica -> sick; férias/PTO -> pto);
- o gestor pode ajustar (saldo inicial, concessão anual, correção).
Reabrir o período apaga o que ele lançou (o extrato está ligado ao fechamento)."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ... import db as banco
from ... import regras
from ...core import avisar, da_empresa, exigir_csrf, gestor_logado, ir
from ...db import Ausencia, Empresa, Fechamento, MovimentoPTO, Turno, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
BANCOS = {"sick": "Sick leave", "pto": "PTO"}
DO_TIPO = {"atestado": "sick", "ferias": "pto"}   # tipo de ausência -> saldo que ela usa
MOTIVOS = {"acumulo": "Ganho no período", "uso": "Usado", "ajuste": "Ajuste do gestor"}


def ligado(e: Empresa) -> bool:
    return bool(e.sick_por_horas or e.pto_por_horas)


def regra(e: Empresa, b: str) -> tuple[int, int]:
    """(horas trabalhadas para ganhar 1 h, saldo máximo em minutos)."""
    return (e.sick_por_horas, e.sick_limite_min) if b == "sick" else (e.pto_por_horas, e.pto_limite_min)


def saldos(db: Session, empresa_id: int, usuario_id: int | None = None) -> dict:
    """{(usuario_id, banco): minutos}"""
    filtros = [MovimentoPTO.empresa_id == empresa_id]
    if usuario_id:
        filtros.append(MovimentoPTO.usuario_id == usuario_id)
    return {(uid, b): m for uid, b, m in db.execute(
        select(MovimentoPTO.usuario_id, MovimentoPTO.banco, func.sum(MovimentoPTO.minutos))
        .where(*filtros).group_by(MovimentoPTO.usuario_id, MovimentoPTO.banco))}


def minutos_previstos(db: Session, a: Ausencia) -> int:
    """Horas de escala que a ausência cobre (o que ela vai usar do saldo)."""
    return sum(regras.minutos_turno(t) for t in db.scalars(select(Turno).where(
        Turno.usuario_id == a.usuario_id, Turno.data.between(a.inicio, a.fim))))


def lancar_fechamento(db: Session, e: Empresa, f: Fechamento, resultado: list[dict]) -> None:
    """Chamado ao fechar o período: desconta o que foi usado e acrescenta o ganho."""
    atuais = saldos(db, e.id)
    for r in resultado:
        uid = r["u"].id
        usado = {"sick": 0, "pto": 0}
        for d in r["dias"]:
            aus = d.get("ausencia")
            if aus and d.get("pto") and aus.tipo in DO_TIPO:
                usado[DO_TIPO[aus.tipo]] += d["pto"]
        trabalhado = r["regular"] + r["ot"] + r["dt"]
        for b in BANCOS:
            saldo = atuais.get((uid, b), 0)
            if usado[b]:
                db.add(MovimentoPTO(empresa_id=e.id, usuario_id=uid, banco=b, minutos=-usado[b],
                                    motivo="uso", fechamento_id=f.id, criado_por=f.criado_por,
                                    obs=f"{f.inicio:%m/%d}–{f.fim:%m/%d}"))
                saldo -= usado[b]
            por_horas, limite = regra(e, b)
            if not por_horas or r["u"].papel != "funcionario":
                continue
            ganho = trabalhado // por_horas   # minutos: 1 h a cada N h = trabalhado / N
            if limite:
                ganho = max(0, min(ganho, limite - saldo))
            if ganho:
                db.add(MovimentoPTO(empresa_id=e.id, usuario_id=uid, banco=b, minutos=ganho,
                                    motivo="acumulo", fechamento_id=f.id, criado_por=f.criado_por,
                                    obs=f"{f.inicio:%m/%d}–{f.fim:%m/%d}, {regras.fmt_min(trabalhado)}"))


def apagar_do_fechamento(db: Session, fechamento_id: int) -> None:
    db.execute(delete(MovimentoPTO).where(MovimentoPTO.fechamento_id == fechamento_id))


def extrato(db: Session, usuario_id: int, limite: int = 30) -> list[MovimentoPTO]:
    return list(db.scalars(select(MovimentoPTO).where(MovimentoPTO.usuario_id == usuario_id)
                           .order_by(MovimentoPTO.criado_em.desc(), MovimentoPTO.id.desc()).limit(limite)))


def _horas(txt: str) -> int | None:
    """'40' ou '7.5' horas -> minutos. Aceita negativo."""
    try:
        v = Decimal(txt.strip().replace(",", ".") or "0")
    except InvalidOperation:
        return None
    if abs(v) > 2000:
        return None
    return int((v * 60).to_integral_value())


# ------------------------------------------------------------------ gestor
@rota.post("/painel/pto/regra", dependencies=CSRF)
def pto_regra(request: Request, sick_por_horas: str = Form("0"), sick_limite: str = Form("0"),
              pto_por_horas: str = Form("0"), pto_limite: str = Form("0"),
              u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    valores = [sick_por_horas, pto_por_horas]
    limites = [_horas(sick_limite), _horas(pto_limite)]
    if (not all(v.strip().isdigit() and int(v) <= 1000 for v in valores)
            or any(l is None or l < 0 for l in limites)):
        avisar(request, "Use números inteiros de horas (0 = desligado).", "erro")
        return ir("/painel/ausencias#saldos")
    e.sick_por_horas, e.pto_por_horas = int(sick_por_horas), int(pto_por_horas)
    e.sick_limite_min, e.pto_limite_min = limites
    regras.registrar(db, e.id, u.nome, "regra de saldos",
                     f"Sick leave: 1 h a cada {e.sick_por_horas} h (máx. {regras.fmt_min(e.sick_limite_min)}); "
                     f"PTO: 1 h a cada {e.pto_por_horas} h (máx. {regras.fmt_min(e.pto_limite_min)}).")
    db.commit()
    avisar(request, "Regra dos saldos salva. Os ganhos são lançados a cada período fechado.")
    return ir("/painel/ausencias#saldos")


@rota.post("/painel/pto/ajuste", dependencies=CSRF)
def pto_ajuste(request: Request, usuario_id: int = Form(...), banco_: str = Form("pto", alias="banco"),
               horas: str = Form(""), obs: str = Form(""), u: Usuario = Depends(gestor_logado),
               db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    minutos = _horas(horas)
    obs = obs.strip()[:200]
    if banco_ not in BANCOS or not minutos:
        avisar(request, "Informe as horas (ex.: 8 para somar, -4 para tirar).", "erro")
        return ir("/painel/ausencias#saldos")
    if not obs:
        avisar(request, "Diga o motivo do ajuste (ex.: saldo inicial).", "erro")
        return ir("/painel/ausencias#saldos")
    db.add(MovimentoPTO(empresa_id=u.empresa_id, usuario_id=f.id, banco=banco_, minutos=minutos,
                        motivo="ajuste", obs=obs, criado_por=u.nome, criado_em=banco.agora()))
    regras.registrar(db, u.empresa_id, u.nome, "ajuste de saldo",
                     f"{f.nome}, {BANCOS[banco_]}: {'+' if minutos > 0 else '-'}{regras.fmt_min(abs(minutos))} ({obs}).")
    db.commit()
    avisar(request, f"Saldo de {f.primeiro_nome} ajustado.")
    return ir("/painel/ausencias#saldos")
