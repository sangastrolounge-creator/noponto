"""Trabalho de menores de idade pela CLT, para quem tem data de nascimento na ficha.

- Menor de 14 anos: não pode trabalhar (Constituição, art. 7º, XXXIII).
- 14 e 15 anos: só como aprendiz (CLT art. 403).
- Menor de 18: proibido o trabalho noturno, das 22h às 5h (CLT art. 404).
- Menor de 18: jornada de até 8 horas por dia e 44 por semana, sem hora extra, salvo
  compensação ou força maior (CLT art. 411 e 413).
- Aprendiz: até 6 horas por dia; até 8 horas se já terminou o ensino fundamental, contando as
  horas de aprendizagem teórica (CLT art. 432). O sistema usa 6 horas para aprendiz.
Não confere atividades insalubres ou perigosas nem o horário da escola. É um resumo da lei:
confira os casos especiais com o contador."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import regras
from . import db as banco

NOITE_INI, NOITE_FIM = 22 * 60, 5 * 60   # 22h às 5h
DIA_MAX, DIA_APRENDIZ, SEMANA_MAX = 480, 360, 2640


def idade(u, d: date) -> int | None:
    n = u.nascimento
    if not n:
        return None
    return d.year - n.year - ((d.month, d.day) < (n.month, n.day))


def aprendiz(u) -> bool:
    return (u.classificacao or "") == "aprendiz"


def regra(e, u, d: date) -> tuple[dict | None, str]:
    """(limites, faixa) para a pessoa no dia. faixa: "" (adulto ou sem data), "<14", "14", "16"."""
    i = idade(u, d)
    if i is None or i >= 18:
        return None, ""
    if i < 14:
        return None, "<14"
    r = {"dia": DIA_APRENDIZ if aprendiz(u) else DIA_MAX, "semana": SEMANA_MAX}
    return r, "14" if i < 16 else "16"


def _hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def _hora(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _noturno(ini: int, fim: int) -> bool:
    """O trecho [ini, fim) em minutos (fim pode passar de 1440) pega alguma parte das 22h às 5h?"""
    for base in (-1440, 0, 1440):
        if ini < base + 1440 + NOITE_FIM and fim > base + NOITE_INI:
            return True
    return False


def problemas_do_turno(r: dict, t, feriados: set = frozenset()) -> list[str]:
    """Motivos (textos curtos) pelos quais o turno passa dos limites da lei para menores."""
    if not t or t.folga or not t.inicio or not t.fim:
        return []
    ini, fim = _hm(t.inicio), _hm(t.fim)
    if fim <= ini:
        fim += 1440
    dur = fim - ini - (t.intervalo_min or 0)
    sai = []
    if dur > r["dia"]:
        sai.append(f"mais de {regras.fmt_min(r['dia'])} no dia")
    if _noturno(ini, fim):
        sai.append("trabalho entre 22h e 5h (proibido para menores)")
    return sai


def feriados_de(db: Session, e, ini: date, fim: date) -> set:
    from .db import Feriado
    return {f.data for f in db.scalars(select(Feriado).where(Feriado.empresa_id == e.id,
                                                             Feriado.data.between(ini, fim)))}


def _motivo_semana(u, faixa: str) -> str | None:
    if faixa == "<14":
        return "menor de 14 anos: não pode trabalhar"
    if faixa == "14" and not aprendiz(u):
        return "14 ou 15 anos: só pode trabalhar como aprendiz (marque na ficha)"
    return None


def conferir_grade(db: Session, e, equipe, turnos: dict, dias: list[date]) -> dict:
    """{(uid, dia): [motivos]} e {uid: motivo da semana} para a grade da escala."""
    por_dia, por_semana, menores = {}, {}, {}
    for f in equipe:
        r, faixa = regra(e, f, dias[0])
        if not faixa:
            continue
        menores[f.id] = faixa
        motivo = _motivo_semana(f, faixa)
        if motivo:
            por_semana[f.id] = motivo
        if not r:
            continue
        total = 0
        for d in dias:
            t = turnos.get((f.id, d))
            m = problemas_do_turno(r, t)
            if m:
                por_dia[(f.id, d)] = m
            total += regras.minutos_turno(t) if t else 0
        if total > r["semana"] and f.id not in por_semana:
            por_semana[f.id] = f"{regras.fmt_min(total)} na semana: o limite é {regras.fmt_min(r['semana'])}"
    return {"dia": por_dia, "semana": por_semana, "menores": menores}


def problemas_ao_salvar(db: Session, e, u, dias: list[date]) -> list[str]:
    from .db import Turno
    from .i18n import _
    r, faixa = regra(e, u, dias[0]) if dias else (None, "")
    motivo = _motivo_semana(u, faixa)
    sai = [_(motivo)] if motivo else []
    if not r:
        return sai
    for d in dias:
        t = db.scalar(select(Turno).where(Turno.usuario_id == u.id, Turno.data == d))
        for m in problemas_do_turno(r, t):
            sai.append(f"{d:%d/%m}: {_(m)}")
    return sai


# ------------------------------------------------------------------ aviso ao vivo (agendador)
def verificar(db: Session) -> list:
    """Menor ainda trabalhando depois das 22h ou além do limite do dia: um aviso por pessoa por
    dia para o gestor."""
    from .custos import minutos_trabalhados
    from .db import Alerta, Empresa, Usuario
    from .core import alertas_liberados
    novos = []
    agora = banco.agora()
    for e in db.scalars(select(Empresa).where(Empresa.ativo.is_(True))):
        if not alertas_liberados(e):
            continue
        menores = list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                        Usuario.nascimento.is_not(None))))
        if not menores:
            continue
        al = regras.agora_local(e)
        for u in menores:
            aberta = regras.ponto_aberto(db, u)
            if not aberta:
                continue
            r, faixa = regra(e, u, aberta.dia)
            if not r:
                continue
            if db.scalar(select(Alerta.id).where(Alerta.usuario_id == u.id, Alerta.tipo == "menor",
                                                 Alerta.criado_em >= agora - timedelta(hours=20))):
                continue
            agora_min = al.hour * 60 + al.minute
            trabalhado = minutos_trabalhados(db, e, aberta.dia).get(u.id, 0)
            motivo = None
            if agora_min >= NOITE_INI or agora_min < NOITE_FIM:
                motivo = "ainda está trabalhando depois das 22h (trabalho noturno é proibido para menores)"
            elif trabalhado > r["dia"]:
                motivo = f"já trabalhou {regras.fmt_min(trabalhado)} hoje (limite {regras.fmt_min(r['dia'])})"
            if motivo:
                a = Alerta(empresa_id=e.id, usuario_id=u.id, tipo="menor", criado_em=agora,
                           texto=f"{u.nome} ({idade(u, aberta.dia)} anos) {motivo}.")
                db.add(a)
                novos.append(a)
    if novos:
        db.commit()
    return novos


