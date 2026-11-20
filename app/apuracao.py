"""Apuração do período: horas previstas e trabalhadas, extras, atrasos, faltas, ausências,
banco de horas e fechamento. Tudo em minutos."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clt, regras
from .db import (AjusteBanco, Ausencia, Batida, Empresa, Fechamento, FechamentoLinha,
                 Feriado, MotivoAjuste, Sobreaviso, Turno, Usuario)
from .regras import VALIDA

TIPOS_AUSENCIA = {
    "atestado": "Atestado médico", "ferias": "Férias", "licenca": "Licença",
    "afastamento": "Afastamento (INSS)", "abono": "Falta abonada", "outro": "Outro",
}


def fmt_saldo(m: int) -> str:
    return ("+" if m > 0 else "−" if m < 0 else "") + regras.fmt_min(abs(m))


def com_acrescimo(normal: int, especial: int, pct_normal: int, pct_especial: int) -> int:
    """Minutos de hora extra já com o acréscimo: 2h a 50% valem 3h de hora normal."""
    return int(normal * (100 + pct_normal) / 100 + especial * (100 + pct_especial) / 100 + 0.5)


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


# ------------------------------------------------------------------ feriados
def pascoa(ano: int) -> date:
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    mes = (h + m - 7 * n + 114) // 31
    dia = (h + m - 7 * n + 114) % 31 + 1
    return date(ano, mes, dia)


def feriados_nacionais(ano: int) -> list[tuple[date, str]]:
    fixos = [(1, 1, "Confraternização Universal"), (4, 21, "Tiradentes"),
             (5, 1, "Dia do Trabalho"), (9, 7, "Independência do Brasil"),
             (10, 12, "Nossa Senhora Aparecida"), (11, 2, "Finados"),
             (11, 15, "Proclamação da República"), (11, 20, "Consciência Negra"),
             (12, 25, "Natal")]
    lista = [(date(ano, m, d), nome) for m, d, nome in fixos]
    lista.append((pascoa(ano) - timedelta(days=2), "Sexta-feira Santa"))
    return sorted(lista)


# ------------------------------------------------------------------ fechamento
def fechado_ate(db: Session, e: Empresa) -> date | None:
    """Último dia já fechado. Nada até essa data pode ser alterado."""
    return db.scalar(select(Fechamento.fim).where(Fechamento.empresa_id == e.id)
                     .order_by(Fechamento.fim.desc()).limit(1))


def esta_fechado(db: Session, e: Empresa, dia: date) -> bool:
    limite = fechado_ate(db, e)
    return bool(limite and dia <= limite)


MSG_FECHADO = ("Este dia está num período de ponto já fechado. Para alterar, reabra o "
               "período em Fechamento.")


def saldo_anterior(db: Session, e: Empresa, usuario_id: int, antes_de: date) -> int:
    """Saldo do banco de horas que vem do último período fechado antes da data."""
    if e.controle != "banco":
        return 0
    return db.scalar(select(FechamentoLinha.saldo_final).join(Fechamento).where(
        FechamentoLinha.usuario_id == usuario_id, Fechamento.fim < antes_de)
        .order_by(Fechamento.fim.desc()).limit(1)) or 0


def periodo_aberto(db: Session, e: Empresa) -> tuple[date, date]:
    """Do dia seguinte ao último fechamento (ou do dia 1º do mês) até hoje."""
    hoje = regras.hoje(e)
    limite = fechado_ate(db, e)
    ini = limite + timedelta(days=1) if limite else hoje.replace(day=1)
    return min(ini, hoje), hoje


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


def apurar(db: Session, e: Empresa, ini: date, fim: date, usuario_id: int | None = None
           ) -> list[dict]:
    """Uma entrada por funcionário, com o dia a dia e os totais do período.

    Regras: previsto = duração do turno menos o intervalo (zero em folga, feriado e ausência).
    Diferença do dia dentro da tolerância não conta. Dia de turno sem nenhuma batida é falta
    (fica em coluna própria, fora do saldo). Trabalho em dia marcado como Folga ou em feriado
    é extra à parte; trabalho em dia sem escala fica pendente (não vira extra) até o gestor
    definir o dia. Sábado e domingo não têm regra especial: valem pela escala.
    Entre 22h e 5h é hora noturna: com a hora reduzida, cada 52min30s contam como 1 hora."""
    agora_l = regras.agora_local(e)
    fim = min(fim, regras.hoje(e))
    filtro_u = [Usuario.empresa_id == e.id]
    if usuario_id:
        filtro_u.append(Usuario.id == usuario_id)
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(*filtro_u))}

    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(ini, fim)))}
    batidas: dict[tuple[int, date], list[Batida]] = {}
    for b in db.scalars(select(Batida).where(
            Batida.empresa_id == e.id, Batida.dia.between(ini, fim), VALIDA)
            .order_by(Batida.momento)):
        batidas.setdefault((b.usuario_id, b.dia), []).append(b)
    feriados = {f.data: f.nome for f in db.scalars(select(Feriado).where(
        Feriado.empresa_id == e.id, Feriado.data.between(ini, fim)))}
    ausencias = ausencias_por_dia(db, e, ini, fim)
    ajustes: dict[int, list[AjusteBanco]] = {}
    for a in db.scalars(select(AjusteBanco).where(
            AjusteBanco.empresa_id == e.id, AjusteBanco.data.between(ini, fim))
            .order_by(AjusteBanco.data)):
        ajustes.setdefault(a.usuario_id, []).append(a)
    sobreavisos: dict[tuple[int, date], list[Sobreaviso]] = {}
    for s in db.scalars(select(Sobreaviso).where(
            Sobreaviso.empresa_id == e.id, Sobreaviso.dia.between(ini, fim)).order_by(Sobreaviso.inicio)):
        sobreavisos.setdefault((s.usuario_id, s.dia), []).append(s)
    motivos: dict[tuple[int, date], list[MotivoAjuste]] = {}
    for m in db.scalars(select(MotivoAjuste).where(
            MotivoAjuste.empresa_id == e.id, MotivoAjuste.dia.between(ini, fim))
            .order_by(MotivoAjuste.criado_em)):
        motivos.setdefault((m.usuario_id, m.dia), []).append(m)

    com_movimento = ({uid for uid, _ in list(turnos) + list(batidas) + list(ausencias)
                      + list(sobreavisos)} | set(ajustes))
    resultado = []
    for uid, u in pessoas.items():
        if uid not in com_movimento and not (u.ativo and u.na_escala):
            continue
        tot = dict(previsto=0, trabalhado=0, extra_normal=0, extra_especial=0, debito=0,
                   faltas=0, falta_min=0, dias_ausencia=0, incompletos=0, noturno=0,
                   sem_intervalo=0, dsr_perdidos=0, dsr_perdido_min=0, dsr_extras=0,
                   sem_escala=0, sem_escala_min=0, intervalo_suprimido=0, sobreaviso=0,
                   ajustes=sum(a.minutos for a in ajustes.get(uid, [])))
        # DSR: o descanso de cada um é a Folga marcada na escala dele, mais os feriados.
        # Sábado e domingo são dias como os outros.
        uteis = descansos = 0
        dias = []
        semanas_com_falta: dict[date, int] = {}  # segunda-feira da semana -> horas do dia faltado
        d = ini
        while d <= fim:
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
            bruto = trabalhado   # antes de descontar o intervalo não batido
            andamento = False
            if aberta:
                pares.append((aberta, None))
                andamento = regras.banco.agora() - aberta.momento < timedelta(hours=18)
            incompleto = any(p[0] is None or p[1] is None for p in pares) and not andamento
            obs = []
            trabalha = bool(t and not t.folga) and not feriado and not aus
            if (t and t.folga) or feriado:
                descansos += 1
            else:
                uteis += 1
            # Trabalhou num dia sem escala (nem horário, nem Folga): não vira extra sozinho.
            # Fica pendente até o gestor pôr o dia na escala (dia normal) ou marcar Folga (extra).
            sem_escala = t is None and not feriado and not aus and bool(bats)
            intervalo = (t.intervalo_min or 0) if t and not t.folga else 0
            direto = (intervalo and len(pares) == 1 and not incompleto and not andamento
                      and trabalhado > 360)  # mais de 6 h sem bater o intervalo
            if direto and e.intervalo_obrigatorio:
                obs.append("intervalo não batido")
                tot["sem_intervalo"] += 1
            if direto and e.intervalo_auto:
                noturno = int(noturno * (trabalhado - intervalo) / trabalhado + 0.5)
                trabalhado -= intervalo
                obs.append(f"intervalo de {intervalo} min descontado")
            if intervalo and len(pares) == 2 and not incompleto and pares[0][1] and pares[1][0]:
                feito = int((pares[1][0].momento - pares[0][1].momento).total_seconds() // 60)
                if feito < intervalo - e.tolerancia_dia_min:
                    obs.append(f"intervalo de {feito} min (previsto {intervalo})")
            # Intervalo intrajornada que faltou (art. 71, §4º): indenizado com 50% a mais.
            suprimido = 0
            if bats and not incompleto and not andamento and (u.classificacao or "clt") not in ("pj", "estagio"):
                feitos = [int((pares[i + 1][0].momento - pares[i][1].momento).total_seconds() // 60)
                          for i in range(len(pares) - 1) if pares[i][1] and pares[i + 1][0]]
                if not feitos and direto and e.intervalo_auto:
                    feitos = [intervalo]   # pré-assinalado: descontado sem batida (art. 74, §2º)
                suprimido = clt.intervalo_suprimido(bruto, feitos, False, e.intervalo_minimo_min or 60)
                if suprimido:
                    obs.append(f"intervalo suprimido: {regras.fmt_min(suprimido)} (art. 71, §4º)")
                    tot["intervalo_suprimido"] += suprimido
            if noturno:
                bonus = int(noturno / 7 + 0.5) if e.noturno_reduzida else 0
                trabalhado += bonus
                noturno += bonus
                obs.append(f"{regras.fmt_min(noturno)} noturnas"
                           + (f" (hora reduzida: +{bonus} min)" if bonus else ""))
                tot["noturno"] += noturno
            previsto = regras.minutos_turno(t) if trabalha else 0
            falta = False
            if trabalha and not bats and regras.intervalo(t)[1] < agora_l:
                falta = True
            dif = 0 if (falta or andamento or incompleto or sem_escala) else trabalhado - previsto
            if sem_escala and not andamento:
                obs.append("trabalhou sem escala: defina o dia na Escala")
                tot["sem_escala"] += 1
                tot["sem_escala_min"] += trabalhado
            if abs(dif) <= e.tolerancia_dia_min:
                dif = 0
            extra = max(dif, 0)
            debito = max(-dif, 0)
            if feriado:
                obs.append(f"feriado: {feriado}")
            if aus:
                obs.append(TIPOS_AUSENCIA.get(aus.tipo, "Ausência").lower())
                tot["dias_ausencia"] += 1
            if falta:
                obs.append("falta")
                tot["faltas"] += 1
                tot["falta_min"] += previsto
                semanas_com_falta.setdefault(d - timedelta(days=d.weekday()), previsto)
            if incompleto:
                obs.append("falta batida")
                tot["incompletos"] += 1
            if andamento:
                obs.append("trabalhando")
            if any(b.manual for b in bats):
                obs.append("batida lançada à mão")
            # Sobreaviso (art. 244, §2º): à disposição fora do expediente, pago a 1/3 da hora.
            sob = sum(s.minutos for s in sobreavisos.get((uid, d), []))
            if sob:
                obs.append(f"sobreaviso: {regras.fmt_min(sob)}")
                tot["sobreaviso"] += sob
            mots = motivos.get((uid, d), [])
            if not falta:
                tot["previsto"] += previsto
            tot["trabalhado"] += trabalhado
            tot["extra_normal" if trabalha else "extra_especial"] += extra
            tot["debito"] += debito
            dias.append(dict(dia=d, turno=t, feriado=feriado, ausencia=aus, pares=pares,
                             trabalhado=trabalhado, previsto=previsto, extra=extra,
                             debito=debito, falta=falta, obs=obs,
                             especial=not trabalha and not sem_escala, sem_escala=sem_escala,
                             noturno=noturno, intervalo_suprimido=suprimido,
                             sobreaviso=sob, sobreavisos=sobreavisos.get((uid, d), []),
                             motivos=mots))
            d += timedelta(days=1)
        if e.dsr_faltas:  # falta sem justificativa: perde o descanso remunerado daquela semana
            tot["dsr_perdidos"] = len(semanas_com_falta)
            tot["dsr_perdido_min"] = sum(semanas_com_falta.values())
        if e.dsr_extras and e.controle == "extra" and uteis:  # reflexo das extras pagas no descanso
            tot["dsr_extras"] = int(com_acrescimo(
                tot["extra_normal"], tot["extra_especial"], e.pct_extra_normal,
                e.pct_extra_especial) * descansos / uteis + 0.5)
        tot["saldo_periodo"] = (tot["extra_normal"] + tot["extra_especial"] - tot["debito"]
                                + tot["ajustes"])
        tot["saldo_anterior"] = saldo_anterior(db, e, uid, ini)
        tot["saldo_final"] = tot["saldo_anterior"] + tot["saldo_periodo"]
        resultado.append(dict(u=u, dias=dias, ajustes_itens=ajustes.get(uid, []),
                              dias_uteis=uteis, dias_descanso=descansos, **tot))
    resultado.sort(key=lambda r: r["u"].nome.lower())
    return resultado


def fechar(db: Session, e: Empresa, ini: date, fim: date, autor: str) -> Fechamento:
    f = Fechamento(empresa_id=e.id, inicio=ini, fim=fim, criado_por=autor,
                   criado_em=regras.banco.agora(), pct_normal=e.pct_extra_normal,
                   pct_especial=e.pct_extra_especial, pct_noturno=e.pct_noturno)
    for r in apurar(db, e, ini, fim):
        f.linhas.append(FechamentoLinha(
            empresa_id=e.id, usuario_id=r["u"].id,
            **{k: r[k] for k in ("previsto", "trabalhado", "extra_normal", "extra_especial", "noturno",
                                 "debito", "faltas", "falta_min", "dias_ausencia", "ajustes",
                                 "dsr_perdidos", "dsr_perdido_min", "dsr_extras", "intervalo_suprimido",
                                 "sobreaviso", "saldo_anterior", "saldo_final")}))
    db.add(f)
    return f


# ------------------------------------------------------------------ semana (avisos de escala)
SEMANA_CLT_MIN = 44 * 60   # CLT art. 58 / Constituição art. 7º, XIII: 44 horas por semana


def inicio_semana(e, d: date) -> date:
    """Segunda-feira da semana de `d` (a grade da escala começa na segunda)."""
    return d - timedelta(days=d.weekday())
