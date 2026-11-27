"""Apuração do período pelas regras trabalhistas dos EUA. Tudo em minutos.

- Hora extra federal (FLSA): acima de 40 h na semana de trabalho, a 1,5x.
- Alguns estados também contam por dia (ex.: Califórnia: acima de 8 h a 1,5x, acima de 12 h
  a 2x, e o 7º dia seguido na semana). Só as horas normais entram na conta das 40 h.
- As horas extras são atribuídas ao dia em que acontecem, em ordem: o período pode cortar
  uma semana ao meio e cada pedaço fica no período certo.
- Não há banco de horas (comp time é proibido para empresas privadas), DSR nem hora noturna
  reduzida. Diferencial noturno e prêmio de feriado são políticas opcionais da empresa.
- Califórnia: sem refeição de 30 min antes do fim da 5ª hora (ou a 2ª depois da 10ª hora),
  1 hora extra de salário (meal premium) no dia."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import regras
from ...db import (Ausencia, Batida, Empresa, Fechamento, FechamentoLinha, Feriado, Turno,
                 Usuario)
from ...regras import VALIDA

TIPOS_AUSENCIA = {
    "atestado": "Licença médica (sick leave)", "ferias": "Férias / PTO",
    "licenca": "Licença não remunerada", "afastamento": "Licença FMLA",
    "abono": "Falta justificada", "outro": "Outro",
}
AUSENCIA_PAGA = {"atestado", "ferias"}  # contam como horas pagas (PTO) no período

# Regras de hora extra por estado: (extra no dia, dobro no dia, 7º dia, meal premium).
# Minutos; 0 = o estado não tem essa regra. Os demais seguem só a regra federal (40 h/semana).
# Confira com um advogado trabalhista do estado: há exceções por setor e por salário.
REGRAS_ESTADO = {
    "CA": (480, 720, True, True),     # Califórnia
    "AK": (480, 0, False, False),     # Alasca
    "CO": (720, 0, False, False),     # Colorado
    "NV": (480, 0, False, False),     # Nevada (só para quem ganha menos de 1,5x o mínimo)
}
ESTADOS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}


# Fuso principal de cada estado (os que têm dois fusos ficam com o da maior parte da população).
_CT = ("AL", "AR", "IL", "IA", "KS", "LA", "MN", "MS", "MO", "NE", "ND", "OK", "SD", "TN", "TX", "WI")
_MT = ("CO", "ID", "MT", "NM", "UT", "WY")
_PT = ("CA", "NV", "OR", "WA")
FUSO_ESTADO = {**{x: "America/Chicago" for x in _CT}, **{x: "America/Denver" for x in _MT},
               **{x: "America/Los_Angeles" for x in _PT},
               "AZ": "America/Phoenix", "AK": "America/Anchorage", "HI": "Pacific/Honolulu"}


def fuso_do_estado(estado: str) -> str:
    return FUSO_ESTADO.get(estado, "America/New_York")


def aplicar_estado(e: Empresa, estado: str) -> None:
    ot, dt, setimo, meal = REGRAS_ESTADO.get(estado, (0, 0, False, False))
    e.estado, e.ot_semana_min = estado, 2400
    e.ot_dia_min, e.dt_dia_min, e.setimo_dia, e.meal_premium = ot, dt, setimo, meal


def fmt_saldo(m: int) -> str:
    return ("+" if m > 0 else "−" if m < 0 else "") + regras.fmt_min(abs(m))


def minutos_noturnos(e: Empresa, ini: datetime, fim: datetime) -> int:
    """Minutos entre 22h e 5h (horário local) dentro de um período de trabalho (em UTC)."""
    a, b = regras.para_local(e, ini), regras.para_local(e, fim)
    total, d = 0, a.date() - timedelta(days=1)
    while d <= b.date():
        n_ini = datetime.combine(d, datetime.min.time()) + timedelta(hours=22)
        n_fim = n_ini + timedelta(hours=7)
        total += max(0, int((min(b, n_fim) - max(a, n_ini)).total_seconds() // 60))
        d += timedelta(days=1)
    return total


# ------------------------------------------------------------------ feriados federais
def _n_dia_semana(ano, mes, dia_semana, n):
    """n-ésimo dia da semana do mês (n = -1: o último). dia_semana: 0 = segunda."""
    if n > 0:
        d = date(ano, mes, 1)
        d += timedelta(days=(dia_semana - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = (date(ano, mes, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - dia_semana) % 7)


def feriados_nacionais(ano: int) -> list[tuple[date, str]]:
    """Feriados federais (o nome fica em inglês, como no calendário americano)."""
    return sorted([
        (date(ano, 1, 1), "New Year's Day"),
        (_n_dia_semana(ano, 1, 0, 3), "Martin Luther King Jr. Day"),
        (_n_dia_semana(ano, 2, 0, 3), "Presidents' Day"),
        (_n_dia_semana(ano, 5, 0, -1), "Memorial Day"),
        (date(ano, 6, 19), "Juneteenth"),
        (date(ano, 7, 4), "Independence Day"),
        (_n_dia_semana(ano, 9, 0, 1), "Labor Day"),
        (_n_dia_semana(ano, 10, 0, 2), "Columbus Day"),
        (date(ano, 11, 11), "Veterans Day"),
        (_n_dia_semana(ano, 11, 3, 4), "Thanksgiving Day"),
        (date(ano, 12, 25), "Christmas Day"),
    ])


# ------------------------------------------------------------------ períodos de pagamento
def inicio_semana(e: Empresa, d: date) -> date:
    return d - timedelta(days=(d.weekday() - e.semana_inicio) % 7)


def periodo_de(e: Empresa, d: date) -> tuple[date, date]:
    """Período de pagamento que contém o dia, pela frequência da empresa."""
    if e.frequencia == "monthly":
        ini = d.replace(day=1)
        return ini, (ini + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if e.frequencia == "semimonthly":
        if d.day <= 15:
            return d.replace(day=1), d.replace(day=15)
        return d.replace(day=16), (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    dias = 7 if e.frequencia == "weekly" else 14
    ancora = e.periodo_ancora or inicio_semana(e, date(2026, 1, 4))
    ini = ancora + timedelta(days=((d - ancora).days // dias) * dias)
    return ini, ini + timedelta(days=dias - 1)


# ------------------------------------------------------------------ fechamento
def fechado_ate(db: Session, e: Empresa) -> date | None:
    """Último dia já fechado. Nada até essa data pode ser alterado."""
    return db.scalar(select(Fechamento.fim).where(Fechamento.empresa_id == e.id)
                     .order_by(Fechamento.fim.desc()).limit(1))


def esta_fechado(db: Session, e: Empresa, dia: date) -> bool:
    limite = fechado_ate(db, e)
    return bool(limite and dia <= limite)


MSG_FECHADO = ("Este dia está num período de pagamento já fechado. Para alterar, reabra o "
               "período em Fechamento.")


def periodo_aberto(db: Session, e: Empresa) -> tuple[date, date]:
    """Do dia seguinte ao último fechamento (ou do início do período atual) até hoje."""
    hoje = regras.hoje(e)
    limite = fechado_ate(db, e)
    ini = limite + timedelta(days=1) if limite else periodo_de(e, hoje)[0]
    return min(ini, hoje), hoje


# ------------------------------------------------------------------ divisão das horas
def dividir_dia(e: Empresa, minutos: int, setimo: bool, normais_semana: int) -> tuple[int, int, int]:
    """Divide as horas do dia em (normais, extra 1,5x, dobro 2x).

    `normais_semana`: horas normais já feitas antes, na mesma semana de trabalho."""
    if setimo:   # 7º dia seguido (CA): até 8 h a 1,5x, o resto a 2x
        return 0, min(minutos, 480), max(minutos - 480, 0)
    reg, ot, dt = minutos, 0, 0
    if e.ot_dia_min and minutos > e.ot_dia_min:
        reg = e.ot_dia_min
        resto = minutos - reg
        if e.dt_dia_min and minutos > e.dt_dia_min:
            dt = minutos - e.dt_dia_min
            resto -= dt
        ot = resto
    passou = normais_semana + reg - e.ot_semana_min
    if passou > 0:  # acima das 40 h normais da semana
        mover = min(passou, reg)
        reg, ot = reg - mover, ot + mover
    return reg, ot, dt


def falta_refeicao(pares: list, trabalhado: int) -> bool:
    """Califórnia: refeição de 30 min antes do fim da 5ª hora; acima de 10 h, uma segunda."""
    if trabalhado <= 300:
        return False
    completos = [p for p in pares if p[0] and p[1]]
    if len(completos) < 2:
        return True
    feito = 0
    intervalos = []
    for (a, b), (c, _) in zip(completos, completos[1:]):
        feito += int((b.momento - a.momento).total_seconds() // 60)
        intervalos.append((feito, int((c.momento - b.momento).total_seconds() // 60)))
    if not any(ini <= 300 and dur >= 30 for ini, dur in intervalos):
        return True
    if trabalhado > 600 and sum(1 for ini, dur in intervalos if dur >= 30) < 2:
        return True
    return False


# ------------------------------------------------------------------ apuração
def ausencias_por_dia(db: Session, e: Empresa, ini: date, fim: date) -> dict:
    mapa = {}
    for a in db.scalars(select(Ausencia).where(
            Ausencia.empresa_id == e.id, Ausencia.situacao == "aprovado",
            Ausencia.inicio <= fim, Ausencia.fim >= ini)):
        d = max(a.inicio, ini)
        while d <= min(a.fim, fim):
            mapa[(a.usuario_id, d)] = a
            d += timedelta(days=1)
    return mapa


CHAVES = ("previsto", "trabalhado", "regular", "ot", "dt", "meal_premios", "descanso_premios", "previsibilidade_min",
          "previsibilidade_centavos", "noturno",
          "feriado_min", "pto_min", "debito", "faltas", "falta_min", "dias_ausencia")


def apurar(db: Session, e: Empresa, ini: date, fim: date, usuario_id: int | None = None
           ) -> list[dict]:
    """Uma entrada por funcionário, com o dia a dia e os totais do período.

    Dias da semana de trabalho anteriores a `ini` entram só na conta das 40 h."""
    agora_l = regras.agora_local(e)
    fim = min(fim, regras.hoje(e))
    ini0 = inicio_semana(e, ini)
    filtro_u = [Usuario.empresa_id == e.id]
    if usuario_id:
        filtro_u.append(Usuario.id == usuario_id)
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(*filtro_u))}

    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(ini0, fim)))}
    batidas: dict[tuple[int, date], list[Batida]] = {}
    for b in db.scalars(select(Batida).where(
            Batida.empresa_id == e.id, Batida.dia.between(ini0, fim), VALIDA)
            .order_by(Batida.momento)):
        batidas.setdefault((b.usuario_id, b.dia), []).append(b)
    feriados = {f.data: f.nome for f in db.scalars(select(Feriado).where(
        Feriado.empresa_id == e.id, Feriado.data.between(ini0, fim)))}
    ausencias = ausencias_por_dia(db, e, ini0, fim)
    from . import descanso
    pausas_ca = descanso.ativo(e)
    pausas = descanso.por_dia(db, e.id, ini, fim) if pausas_ca else {}
    from . import escala_justa
    fw = escala_justa.regra(e)
    premios_fw = escala_justa.premios_por_dia(db, e, ini, fim) if fw else {}

    com_movimento = {uid for uid, d in list(turnos) + list(batidas) + list(ausencias) if d >= ini}
    resultado = []
    for uid, u in pessoas.items():
        if uid not in com_movimento and not (u.ativo and u.na_escala):
            continue
        tot = {k: 0 for k in CHAVES} | dict(incompletos=0, sem_intervalo=0)
        dias = []
        semana, normais_semana, dias_seguidos = None, 0, 0
        d = ini0
        while d <= fim:
            no_periodo = d >= ini
            if inicio_semana(e, d) != semana:
                semana, normais_semana, dias_seguidos = inicio_semana(e, d), 0, 0
            t, bats = turnos.get((uid, d)), batidas.get((uid, d), [])
            feriado, aus = feriados.get(d), ausencias.get((uid, d))
            pares, aberta, trabalhado, noturno = [], None, 0, 0
            for b in bats:
                if b.tipo == "entrada":
                    if aberta:
                        pares.append((aberta, None))
                    aberta = b
                else:
                    pares.append((aberta, b))
                    if aberta:
                        trabalhado += int((b.momento - aberta.momento).total_seconds() // 60)
                        noturno += minutos_noturnos(e, aberta.momento, b.momento)
                    aberta = None
            andamento = False
            if aberta:
                pares.append((aberta, None))
                andamento = regras.banco.agora() - aberta.momento < timedelta(hours=18)
            incompleto = any(p[0] is None or p[1] is None for p in pares) and not andamento
            obs = []
            trabalha = bool(t and not t.folga) and not feriado and not aus
            intervalo = (t.intervalo_min or 0) if t and not t.folga else 0
            direto = (intervalo and len(pares) == 1 and not incompleto and not andamento
                      and trabalhado > 360)  # mais de 6 h sem bater a refeição
            if direto and e.intervalo_obrigatorio:
                obs.append("refeição não batida")
                if no_periodo:
                    tot["sem_intervalo"] += 1
            if direto and e.intervalo_auto:
                noturno = int(noturno * (trabalhado - intervalo) / trabalhado + 0.5)
                trabalhado -= intervalo
                obs.append(f"refeição de {intervalo} min descontada")
            # divisão em normais / extra / dobro (o dia em andamento ainda não entra)
            conta = 0 if andamento else trabalhado
            if conta:
                dias_seguidos += 1
            setimo = bool(e.setimo_dia and conta and dias_seguidos == 7)
            reg, ot, dt = dividir_dia(e, conta, setimo, normais_semana)
            normais_semana += reg
            meal = bool(e.meal_premium and conta and not incompleto and falta_refeicao(pares, conta))
            resp = pausas.get((uid, d))
            rest = bool(pausas_ca and conta >= descanso.MINIMO_MIN and not incompleto and resp
                        and resp.resposta == "nao_liberado" and not (u.isento or (u.classificacao or "w2").startswith("1099")))
            if not no_periodo:
                d += timedelta(days=1)
                continue
            if ot or dt:
                obs.append(f"extra {regras.fmt_min(ot)}" + (f" + dobro {regras.fmt_min(dt)}" if dt else ""))
            if setimo:
                obs.append("7º dia seguido na semana")
            if meal:
                obs.append("sem refeição no prazo: 1 h de meal premium")
            if rest:
                obs.append("pausas de 10 min não liberadas: 1 h de rest premium")
            prev_min = prev_c = 0
            if fw and not (u.isento or (u.classificacao or "w2").startswith("1099")):
                pm = premios_fw.get((uid, d))
                if pm:
                    prev_min += pm.minutos
                    obs.append(f"escala mudada em cima da hora: {regras.fmt_min(pm.minutos)} de predictability pay")
                ontem = [b for b in batidas.get((uid, d - timedelta(days=1)), []) if b.tipo == "saida"]
                entradas = [b for b in bats if b.tipo == "entrada"]
                if ontem and entradas and conta:
                    m, c = escala_justa.descanso_curto(e, entradas[0].momento, ontem[-1].momento, conta)
                    if m or c:
                        prev_min += m
                        prev_c += c
                        obs.append("descanso curto entre turnos: adicional")
            if noturno and e.pct_noturno:
                obs.append(f"{regras.fmt_min(noturno)} noturnas")
                tot["noturno"] += noturno
            previsto = regras.minutos_turno(t) if trabalha else 0
            falta = bool(trabalha and not bats and regras.intervalo(t)[1] < agora_l)
            debito = 0
            if trabalha and not (falta or andamento or incompleto):
                debito = max(previsto - trabalhado, 0)
                if debito <= e.tolerancia_dia_min:
                    debito = 0
            if feriado:
                obs.append(f"feriado: {feriado}")
                tot["feriado_min"] += conta
            pto = 0
            if aus:
                obs.append(TIPOS_AUSENCIA.get(aus.tipo, "Ausência").lower())
                tot["dias_ausencia"] += 1
                if aus.tipo in AUSENCIA_PAGA and t and not t.folga:
                    pto = regras.minutos_turno(t)
                    tot["pto_min"] += pto
            if falta:
                obs.append("não compareceu")
                tot["faltas"] += 1
                tot["falta_min"] += previsto
            if incompleto:
                obs.append("falta batida")
                tot["incompletos"] += 1
            if andamento:
                obs.append("trabalhando")
            if any(b.manual for b in bats):
                obs.append("batida lançada à mão")
            if not falta:
                tot["previsto"] += previsto
            tot["trabalhado"] += conta
            tot["regular"] += reg
            tot["ot"] += ot
            tot["dt"] += dt
            tot["meal_premios"] += 60 if meal else 0
            tot["descanso_premios"] += 60 if rest else 0
            tot["previsibilidade_min"] += prev_min
            tot["previsibilidade_centavos"] += prev_c
            tot["debito"] += debito
            dias.append(dict(dia=d, turno=t, feriado=feriado, ausencia=aus, pares=pares,
                             trabalhado=trabalhado, previsto=previsto, regular=reg, ot=ot, dt=dt,
                             meal=meal, rest=rest, debito=debito, falta=falta, obs=obs, pto=pto,
                             especial=bool((t and t.folga) or feriado), noturno=noturno))
            d += timedelta(days=1)
        resultado.append(dict(u=u, dias=dias, **tot))
    resultado.sort(key=lambda r: r["u"].nome.lower())
    return resultado


def fechar(db: Session, e: Empresa, ini: date, fim: date, autor: str) -> Fechamento:
    f = Fechamento(empresa_id=e.id, inicio=ini, fim=fim, criado_por=autor,
                   criado_em=regras.banco.agora(), estado=e.estado,
                   pct_noturno=e.pct_noturno, pct_feriado=e.pct_feriado)
    resultado = apurar(db, e, ini, fim)
    for r in resultado:
        f.linhas.append(FechamentoLinha(empresa_id=e.id, usuario_id=r["u"].id,
                                        **{k: r[k] for k in CHAVES}))
    db.add(f)
    db.flush()
    from . import pto  # saldos de sick leave e PTO: uso e ganho do período
    pto.lancar_fechamento(db, e, f, resultado)
    return f
