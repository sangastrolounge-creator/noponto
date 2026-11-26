"""Trabalho de menores de idade (child labor), para quem tem data de nascimento na ficha.

Federal (FLSA), 14 e 15 anos, fora da agricultura:
- dia de aula: até 3 h; semana com aula: até 18 h;
- dia sem aula: até 8 h; semana sem aula: até 40 h;
- só entre 7h e 19h (até 21h de 1º de junho ao Dia do Trabalho).
16 e 17 anos: a lei federal não limita horas; alguns estados limitam. Aqui estão Califórnia,
Nova York e Flórida. Menores de 14 anos, em geral, não podem trabalhar fora da agricultura.

"Dia de aula" = segunda a sexta, fora das férias de verão (1º de junho ao Dia do Trabalho) e dos
feriados cadastrados. É uma aproximação: o calendário de cada escola varia. Os limites de horário
à noite valem para a noite antes de um dia de aula; antes de um dia sem aula vale o limite maior.
Resumo das regras; confira o site do Departamento do Trabalho do estado."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import regras
from ... import db as banco

# Minutos. inicio/fim_* = minutos desde a meia-noite (mais de 1440 = depois da meia-noite).
# None = sem limite. fim_verao: limite da noite nas férias de verão.
FED_14 = dict(dia_aula=180, semana_aula=1080, dia_livre=480, semana_livre=2400,
              inicio=420, fim_aula=1140, fim_livre=1140, fim_verao=1260)
REGRAS = {
    ("14", ""): FED_14,
    ("14", "FL"): {**FED_14, "semana_aula": 900},
    ("16", "CA"): dict(dia_aula=240, semana_aula=1680, dia_livre=480, semana_livre=2880,
                       inicio=300, fim_aula=1320, fim_livre=1470, fim_verao=1470),
    ("16", "NY"): dict(dia_aula=240, semana_aula=1680, dia_livre=480, semana_livre=2880,
                       inicio=360, fim_aula=1320, fim_livre=1440, fim_verao=1440),
    ("16", "FL"): dict(dia_aula=480, semana_aula=1800, dia_livre=None, semana_livre=None,
                       inicio=390, fim_aula=1380, fim_livre=None, fim_verao=None),
}


def idade(u, d: date) -> int | None:
    n = u.nascimento
    if not n:
        return None
    return d.year - n.year - ((d.month, d.day) < (n.month, n.day))


def regra(e, u, d: date) -> tuple[dict | None, str]:
    """(limites, faixa) para a pessoa no dia. faixa: "" (adulto ou sem data), "<14", "14", "16"."""
    i = idade(u, d)
    if i is None or i >= 18:
        return None, ""
    if i < 14:
        return None, "<14"
    faixa = "14" if i < 16 else "16"
    r = REGRAS.get((faixa, e.estado)) or (REGRAS[("14", "")] if faixa == "14" else None)
    return r, faixa


def _dia_do_trabalho(ano: int) -> date:
    d = date(ano, 9, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def verao(d: date) -> bool:
    return date(d.year, 6, 1) <= d <= _dia_do_trabalho(d.year)


def dia_de_aula(d: date, feriados: set) -> bool:
    return d.weekday() < 5 and not verao(d) and d not in feriados


def _hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def _fim_noite(r: dict, d: date, feriados: set) -> int | None:
    if verao(d) and r.get("fim_verao"):
        return r["fim_verao"]
    return r["fim_aula"] if dia_de_aula(d + timedelta(days=1), feriados) else r["fim_livre"]


def problemas_do_turno(r: dict, t, feriados: set) -> list[str]:
    """Motivos (textos curtos) pelos quais o turno passa dos limites do dia."""
    if not t or t.folga or not t.inicio or not t.fim:
        return []
    ini, fim = _hm(t.inicio), _hm(t.fim)
    if fim <= ini:
        fim += 1440
    dur = fim - ini - (t.intervalo_min or 0)
    sai = []
    aula = dia_de_aula(t.data, feriados)
    limite_dia = r["dia_aula"] if aula else r["dia_livre"]
    if limite_dia is not None and dur > limite_dia:
        sai.append(f"mais de {regras.fmt_min(limite_dia)} no dia de aula" if aula
                   else f"mais de {regras.fmt_min(limite_dia)} no dia")
    if r["inicio"] is not None and ini < r["inicio"]:
        sai.append(f"começa antes das {_hora(r['inicio'])}")
    noite = _fim_noite(r, t.data, feriados)
    if noite is not None and fim > noite:
        sai.append(f"termina depois das {_hora(noite)}")
    return sai


def _hora(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def feriados_de(db: Session, e, ini: date, fim: date) -> set:
    from ...db import Feriado
    return {f.data for f in db.scalars(select(Feriado).where(Feriado.empresa_id == e.id,
                                                             Feriado.data.between(ini, fim)))}


def conferir_grade(db: Session, e, equipe, turnos: dict, dias: list[date]) -> dict:
    """{(uid, dia): [motivos]} e {uid: motivo da semana} para a grade da escala."""
    feriados = feriados_de(db, e, dias[0] - timedelta(days=1), dias[-1] + timedelta(days=1))
    por_dia, por_semana, menores = {}, {}, {}
    for f in equipe:
        r, faixa = regra(e, f, dias[0])
        if not faixa:
            continue
        menores[f.id] = faixa
        if faixa == "<14":
            por_semana[f.id] = "menor de 14 anos: em geral não pode trabalhar fora da agricultura"
            continue
        if not r:
            por_semana[f.id] = "16 ou 17 anos: confira os limites da lei do seu estado"
            continue
        total = 0
        for d in dias:
            t = turnos.get((f.id, d))
            m = problemas_do_turno(r, t, feriados)
            if m:
                por_dia[(f.id, d)] = m
            total += regras.minutos_turno(t) if t else 0
        aula = any(dia_de_aula(d, feriados) for d in dias)
        lim = r["semana_aula"] if aula else r["semana_livre"]
        if lim is not None and total > lim:
            por_semana[f.id] = (f"{regras.fmt_min(total)} na semana: o limite é {regras.fmt_min(lim)} em semana com aula"
                                if aula else f"{regras.fmt_min(total)} na semana: o limite é {regras.fmt_min(lim)}")
    return {"dia": por_dia, "semana": por_semana, "menores": menores}


def problemas_ao_salvar(db: Session, e, u, dias: list[date]) -> list[str]:
    from ...db import Turno
    r, faixa = regra(e, u, dias[0]) if dias else (None, "")
    if faixa == "<14":
        return ["menor de 14 anos"]
    if not r:
        return []
    feriados = feriados_de(db, e, min(dias) - timedelta(days=1), max(dias) + timedelta(days=1))
    sai = []
    for d in dias:
        t = db.scalar(select(Turno).where(Turno.usuario_id == u.id, Turno.data == d))
        from ...i18n import _
        for m in problemas_do_turno(r, t, feriados):
            sai.append(f"{d:%m/%d}: {_(m)}")
    return sai


# ------------------------------------------------------------------ aviso ao vivo (agendador)
def verificar(db: Session) -> list:
    """Menor ainda trabalhando depois do horário permitido ou além do limite do dia: um aviso por
    pessoa por dia para o gestor."""
    from ...custos import minutos_trabalhados
    from ...db import Alerta, Batida, Empresa, Usuario
    from ...core import alertas_liberados
    novos = []
    agora = banco.agora()
    for e in db.scalars(select(Empresa).where(Empresa.ativo.is_(True), Empresa.pais == "US")):
        if not alertas_liberados(e):
            continue
        menores = [u for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                               Usuario.nascimento.is_not(None)))]
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
            dia = aberta.dia
            if db.scalar(select(Alerta.id).where(Alerta.usuario_id == u.id, Alerta.tipo == "menor",
                                                 Alerta.criado_em >= agora - timedelta(hours=20))):
                continue
            feriados = feriados_de(db, e, dia, dia + timedelta(days=1))
            minutos_agora = (al - datetime.combine(dia, datetime.min.time())).total_seconds() // 60
            noite = _fim_noite(r, dia, feriados)
            trabalhado = minutos_trabalhados(db, e, dia).get(u.id, 0)
            lim = r["dia_aula"] if dia_de_aula(dia, feriados) else r["dia_livre"]
            motivo = None
            if noite is not None and minutos_agora > noite:
                motivo = f"ainda está trabalhando depois das {_hora(noite)}"
            elif lim is not None and trabalhado > lim:
                motivo = f"já trabalhou {regras.fmt_min(trabalhado)} hoje (limite {regras.fmt_min(lim)})"
            if motivo:
                a = Alerta(empresa_id=e.id, usuario_id=u.id, tipo="menor", criado_em=agora,
                           texto=f"{u.nome} ({idade(u, dia)} anos) {motivo}: lei de trabalho de menores.")
                db.add(a)
                novos.append(a)
    if novos:
        db.commit()
    return novos
