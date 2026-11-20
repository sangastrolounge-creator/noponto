"""Regras da CLT usadas no cálculo de férias e do intervalo intrajornada.

Só funções puras (sem banco nem web), para testar sem subir o app.

Férias (CLT arts. 129 a 145):
- a cada 12 meses de contrato (período aquisitivo) a pessoa ganha férias;
- os dias dependem das faltas injustificadas no período (art. 130): até 5 faltas, 30 dias;
  6 a 14, 24 dias; 15 a 23, 18 dias; 24 a 32, 12 dias; mais de 32, perde as férias;
- a empresa tem os 12 meses seguintes (período concessivo) para dar as férias (art. 134);
  o que for gozado depois disso é pago em dobro (art. 137);
- podem ser divididas em até 3 períodos, um com pelo menos 14 dias corridos e os outros com
  pelo menos 5 (art. 134, §1º); não podem começar nos 2 dias antes de feriado ou do repouso
  semanal (art. 134, §3º); a pessoa é avisada por escrito com 30 dias (art. 135);
- a pessoa pode vender 1/3 dos dias (abono pecuniário, art. 143);
- quem fica mais de 6 meses afastado pelo INSS no período aquisitivo perde as férias dele
  (art. 133, IV).

Intervalo intrajornada (art. 71): mais de 6 horas de trabalho, intervalo de pelo menos 1 hora
(pode ser reduzido a 30 minutos por convenção ou acordo coletivo, art. 611-A, III); de 4 a 6 horas,
15 minutos. O que não foi concedido é pago com 50% a mais sobre a hora normal, só o tempo que
faltou e com natureza indenizatória (art. 71, §4º, desde a Lei 13.467/2017): não entra na base de
INSS, FGTS e IRRF."""
from datetime import date, timedelta

# ------------------------------------------------------------------ férias
FAIXAS_FALTAS = ((5, 30), (14, 24), (23, 18), (32, 12))   # até N faltas -> dias de férias
DIAS_FERIAS = 30
MINIMO_PARTE = 5           # art. 134, §1º
MINIMO_PARTE_MAIOR = 14
MAXIMO_PARTES = 3
AFASTAMENTO_PERDE = 180    # art. 133, IV: mais de 6 meses afastado pelo INSS
AVISO_DIAS = 30            # art. 135
SEM_FERIAS = {"pj"}        # prestador PJ não tem férias da CLT


def dias_de_direito(faltas: int) -> int:
    """Dias de férias pelo número de faltas injustificadas no período aquisitivo (art. 130)."""
    for ate, dias in FAIXAS_FALTAS:
        if faltas <= ate:
            return dias
    return 0


def somar_anos(d: date, anos: int) -> date:
    """Mesma data N anos depois; 29/02 vira 28/02 em ano que não é bissexto."""
    try:
        return d.replace(year=d.year + anos)
    except ValueError:
        return d.replace(year=d.year + anos, day=28)


def periodos_aquisitivos(admissao: date, ate: date) -> list[tuple[date, date]]:
    """Períodos aquisitivos desde a admissão, inclusive o que está correndo em `ate`."""
    lista, k = [], 0
    while True:
        ini = somar_anos(admissao, k)
        if ini > ate:
            break
        lista.append((ini, somar_anos(admissao, k + 1) - timedelta(days=1)))
        k += 1
    return lista


def fim_concessivo(fim_aquisitivo: date) -> date:
    """Último dia para a empresa dar as férias daquele período (art. 134)."""
    return somar_anos(fim_aquisitivo + timedelta(days=1), 1) - timedelta(days=1)


def avos(ini: date, ate: date) -> int:
    """Meses completos de ini até ate; fração de 15 dias ou mais conta como mês (art. 146)."""
    if ate < ini:
        return 0
    meses, d = 0, ini
    while True:
        prox = _somar_mes(ini, meses + 1)
        if prox - timedelta(days=1) > ate:
            break
        meses += 1
        d = prox
    if (ate - d).days + 1 >= 15:
        meses += 1
    return min(meses, 12)


def _somar_mes(d: date, n: int) -> date:
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    for dia in (d.day, 30, 29, 28):
        try:
            return date(ano, mes, dia)
        except ValueError:
            continue
    return date(ano, mes, 28)


def dias_corridos(ini: date, fim: date) -> int:
    return (fim - ini).days + 1


def saldo_ferias(admissao: date, hoje: date, faltas: dict[date, int], gozos: list[dict],
                 ajustes: dict[date, int] | None = None, afastamentos: dict[date, int] | None = None,
                 contrato: str = "clt") -> list[dict]:
    """Situação de cada período aquisitivo.

    faltas: {início do período: faltas injustificadas nele}
    gozos: [{"inicio", "fim", "abono": dias vendidos, "id"}] em qualquer ordem (férias aprovadas)
    ajustes: {início do período: dias somados ou tirados pelo gestor}
    afastamentos: {início do período: dias de afastamento pelo INSS nele}

    Os gozos são descontados do período mais antigo que ainda tem saldo; o que for gozado depois
    do fim do período concessivo fica marcado como "em dobro"."""
    ajustes, afastamentos = ajustes or {}, afastamentos or {}
    if contrato in SEM_FERIAS:
        return []
    periodos = []
    for ini, fim in periodos_aquisitivos(admissao, hoje):
        completo = fim < hoje
        nf = faltas.get(ini, 0)
        afast = afastamentos.get(ini, 0)
        if contrato == "estagio":   # recesso de 30 dias (Lei 11.788/2008, art. 13): sem a tabela de faltas
            base = DIAS_FERIAS
        else:
            base = 0 if afast > AFASTAMENTO_PERDE else dias_de_direito(nf)
        direito = max(base + ajustes.get(ini, 0), 0)
        periodos.append(dict(
            inicio=ini, fim=fim, completo=completo, faltas=nf, afastamento=afast,
            perdeu_afastamento=afast > AFASTAMENTO_PERDE and contrato != "estagio",
            direito=direito, ajuste=ajustes.get(ini, 0), concessivo=fim_concessivo(fim),
            proporcional=0 if completo else int(direito * avos(ini, hoje) / 12),
            usado=0, abono=0, em_dobro=0, gozos=[]))
    for g in sorted(gozos, key=lambda g: g["inicio"]):
        abono = g.get("abono") or 0
        restante = dias_corridos(g["inicio"], g["fim"]) + abono
        # Só servem os períodos já completos quando as férias começaram, do mais antigo ao mais novo.
        for p in periodos:
            if restante <= 0:
                break
            livre = p["direito"] - p["usado"]
            if p["fim"] >= g["inicio"] or livre <= 0:
                continue
            parte = min(livre, restante)
            usa_abono = min(abono, parte)
            gozado = parte - usa_abono
            p["usado"] += parte
            p["abono"] += usa_abono
            abono -= usa_abono
            if g["inicio"] > p["concessivo"]:
                p["em_dobro"] += gozado
            elif g["fim"] > p["concessivo"]:   # começou no prazo e terminou depois dele
                p["em_dobro"] += min(gozado, (g["fim"] - p["concessivo"]).days)
            p["gozos"].append(dict(g, dias=parte))
            restante -= parte
        if restante > 0 and periodos:
            # Sem saldo adquirido: férias antecipadas, descontadas do período em que começaram.
            p = next((p for p in periodos if p["inicio"] <= g["inicio"] <= p["fim"]), periodos[0])
            p["usado"] += restante
            p["abono"] += abono
            p["gozos"].append(dict(g, dias=restante, antecipada=True))
    for p in periodos:
        p["saldo"] = p["direito"] - p["usado"]
        if not p["completo"]:
            p["situacao"] = "aquisicao"
        elif p["saldo"] <= 0:
            p["situacao"] = "gozado" if p["direito"] else "sem_direito"
        elif hoje > p["concessivo"]:
            p["situacao"] = "vencido"
        elif (p["concessivo"] - hoje).days <= 60:
            p["situacao"] = "vencendo"
        else:
            p["situacao"] = "a_gozar"
        p["dias_para_vencer"] = (p["concessivo"] - hoje).days
    return periodos


def resumo_ferias(periodos: list[dict]) -> dict:
    """Totais para a linha da pessoa: saldo já adquirido, o pior prazo e o período corrente."""
    completos = [p for p in periodos if p["completo"]]
    abertos = [p for p in completos if p["saldo"] > 0]
    pior = None
    for p in abertos:
        if pior is None or p["concessivo"] < pior["concessivo"]:
            pior = p
    atual = periodos[-1] if periodos and not periodos[-1]["completo"] else None
    return dict(saldo=sum(p["saldo"] for p in abertos), proximo=pior, atual=atual,
                vencidos=[p for p in abertos if p["situacao"] == "vencido"],
                vencendo=[p for p in abertos if p["situacao"] == "vencendo"],
                em_dobro=sum(p["em_dobro"] for p in periodos))


def conferir_ferias(inicio: date, fim: date, abono: int, hoje: date, periodo: dict | None,
                    partes_anteriores: list[int], dias_descanso: set[date]) -> list[str]:
    """Avisos sobre um período de férias que vai ser lançado (não impedem o lançamento).

    periodo: o período aquisitivo de onde os dias vão sair (de saldo_ferias), ou None.
    partes_anteriores: dias de cada parte já gozada desse período.
    dias_descanso: feriados e dias de repouso semanal perto do início."""
    avisos = []
    dias = dias_corridos(inicio, fim)
    if periodo is None:
        avisos.append("A pessoa ainda não completou 12 meses de contrato: estas férias seriam "
                      "antecipadas (a CLT só garante férias depois do período aquisitivo).")
    else:
        if dias + abono > periodo["saldo"]:
            avisos.append(f"São {dias + abono} dias, mas o saldo do período "
                          f"{periodo['inicio']:%d/%m/%Y} a {periodo['fim']:%d/%m/%Y} é de "
                          f"{periodo['saldo']} dias.")
        if abono and abono > periodo["direito"] // 3:
            avisos.append(f"O abono pecuniário é de no máximo 1/3 das férias "
                          f"({periodo['direito'] // 3} dias, art. 143).")
        if fim > periodo["concessivo"]:
            avisos.append(f"O prazo para dar essas férias termina em {periodo['concessivo']:%d/%m/%Y}: "
                          "os dias depois disso são pagos em dobro (art. 137).")
        partes = partes_anteriores + [dias]
        sobra = periodo["saldo"] - dias - abono
        if len(partes) > MAXIMO_PARTES or (len(partes) == MAXIMO_PARTES and sobra > 0):
            avisos.append("As férias podem ser divididas em no máximo 3 períodos (art. 134, §1º).")
        if dias < MINIMO_PARTE:
            avisos.append("Cada período de férias precisa ter pelo menos 5 dias corridos (art. 134, §1º).")
        if max(partes) < MINIMO_PARTE_MAIOR and sobra < MINIMO_PARTE_MAIOR:
            avisos.append("Um dos períodos de férias precisa ter pelo menos 14 dias corridos (art. 134, §1º).")
        elif 0 < sobra < MINIMO_PARTE:
            avisos.append(f"Vão sobrar {sobra} dia(s), menos que os 5 dias do menor período permitido.")
    if inicio < hoje + timedelta(days=AVISO_DIAS):
        avisos.append("As férias devem ser comunicadas por escrito com 30 dias de antecedência (art. 135).")
    if any(inicio + timedelta(days=i) in dias_descanso for i in (1, 2)):
        avisos.append("As férias não podem começar nos 2 dias antes de um feriado ou do repouso "
                      "semanal (art. 134, §3º).")
    return avisos


def valor_ferias(salario_centavos: int, dias: int, abono: int = 0) -> dict:
    """Estimativa: salário ÷ 30 por dia, mais 1/3 (art. 7º, XVII da Constituição).
    Não inclui médias de extras e adicionais nem os descontos (INSS e IRRF)."""
    dia = salario_centavos / 30
    ferias = round(dia * dias)
    terco = round(ferias / 3)
    venda = round(dia * abono)
    venda_terco = round(venda / 3)
    return dict(ferias=ferias, terco=terco, abono=venda, abono_terco=venda_terco,
                total=ferias + terco + venda + venda_terco)


# ------------------------------------------------------------------ intervalo intrajornada
INTERVALO_LONGO = 60     # mais de 6 horas de trabalho
INTERVALO_CURTO = 15     # mais de 4 até 6 horas
REDUZIDO_MINIMO = 30     # menor intervalo que a convenção coletiva pode fixar


def intervalo_exigido(trabalhado: int, minimo_longo: int = INTERVALO_LONGO) -> int:
    """Intervalo mínimo pelo tempo trabalhado no dia (art. 71, caput e §1º)."""
    if trabalhado > 360:
        return max(minimo_longo, REDUZIDO_MINIMO)
    if trabalhado > 240:
        return INTERVALO_CURTO
    return 0


def intervalo_suprimido(trabalhado: int, intervalos: list[int], pre_assinalado: bool,
                        minimo_longo: int = INTERVALO_LONGO) -> int:
    """Minutos de intervalo que faltaram no dia (art. 71, §4º).

    trabalhado: minutos de trabalho, sem os intervalos.
    intervalos: duração de cada intervalo batido (saída e volta).
    pre_assinalado: o intervalo é anotado sem batida (art. 74, §2º) e foi descontado; conta como feito.
    Vale o maior intervalo do dia: dividir o intervalo em pedaços não cumpre a regra."""
    exigido = intervalo_exigido(trabalhado, minimo_longo)
    if not exigido:
        return 0
    if pre_assinalado and not intervalos:
        return 0
    feito = max(intervalos) if intervalos else 0
    return max(exigido - feito, 0)
