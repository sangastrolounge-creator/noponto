"""Pagamento bruto (gross pay) de cada funcionário a partir das horas do período fechado.

Centavos de dólar. Impostos e descontos (federal/state withholding, FICA, benefícios) NÃO são
calculados aqui: ficam com o provedor de folha (Gusto, ADP, QuickBooks Payroll...), que recebe
as horas pela planilha. O Claude revisa (veja revisor.py)."""
from decimal import ROUND_HALF_UP, Decimal

SALARIO_MINIMO_FEDERAL = 725      # US$ 7,25/h
LIMITE_ISENTO_SEMANA = 68400      # US$ 684/semana: abaixo disso o "exempt" não vale (FLSA)

TIPOS_LANCAMENTO = {  # tipo -> (rótulo, entra na taxa regular da hora extra)
    "gorjeta_cartao": ("Gorjetas no cartão", False),
    "gorjeta_dinheiro": ("Gorjetas em dinheiro (declaradas)", False),
    "bonus": ("Bônus por meta (não discricionário)", True),
    "comissao": ("Comissão", True),
    "bonus_livre": ("Bônus discricionário (presente, festas)", False),
    "reembolso": ("Reembolso de despesas", False),
}


def dividir_gorjetas(total: int, pesos: dict) -> dict:
    """Divide o total (centavos) proporcionalmente aos pesos (minutos trabalhados, ou 1 cada).
    Os centavos que sobram do arredondamento vão para quem teve a maior fração."""
    pesos = {k: v for k, v in pesos.items() if v > 0}
    soma = sum(pesos.values())
    if total <= 0 or not soma:
        return {}
    exato = {k: Decimal(total) * v / soma for k, v in pesos.items()}
    parte = {k: int(x) for k, x in exato.items()}
    sobra = total - sum(parte.values())
    for k in sorted(exato, key=lambda k: (exato[k] - parte[k], -k if isinstance(k, int) else 0),
                    reverse=True)[:sobra]:
        parte[k] += 1
    return parte


def _c(v) -> int:
    return int(Decimal(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_horas(m: int) -> str:
    return f"{m / 60:.2f} h"


def calcular(taxa: int, isento: bool, salario_periodo: int, lancamentos: list, horas: dict,
             pct_noturno: int, pct_feriado: int) -> dict:
    """`horas`: minutos da linha do fechamento. Devolve as linhas e o bruto."""
    linhas = []

    def p(cod, desc, ref, valor):
        if valor:
            linhas.append(dict(cod=cod, desc=desc, ref=ref, tipo="p", valor=valor))

    r = Decimal(taxa)
    reg, ot, dt = horas.get("regular", 0), horas.get("ot", 0), horas.get("dt", 0)
    trabalhadas = reg + ot + dt
    if isento:
        p("salario", "Salário do período (exempt)", "", salario_periodo)
    else:
        p("regular", "Horas normais", fmt_horas(reg), _c(r * reg / 60))
        p("ot", "Hora extra (1,5x)", fmt_horas(ot), _c(r * ot / 60 * Decimal("1.5")))
        p("dt", "Dobro (2x)", fmt_horas(dt), _c(r * dt / 60 * 2))
        meal = horas.get("meal_premios", 0)
        p("meal", "Meal premium", fmt_horas(meal), _c(r * meal / 60))
        rest = horas.get("descanso_premios", 0)
        p("rest", "Rest break premium", fmt_horas(rest), _c(r * rest / 60))
        prev = horas.get("previsibilidade_min", 0)
        p("previs", "Predictability pay (Fair Workweek)", fmt_horas(prev) if prev else "",
          _c(r * prev / 60) + horas.get("previsibilidade_centavos", 0))
    pto = horas.get("pto_min", 0)
    p("pto", "Licença remunerada (PTO/sick)", fmt_horas(pto), _c(r * pto / 60))
    noturno = horas.get("noturno", 0)
    if pct_noturno:
        p("noturno", f"Diferencial noturno {pct_noturno}%", fmt_horas(noturno),
          _c(r * noturno / 60 * Decimal(pct_noturno) / 100))
    fer = horas.get("feriado_min", 0)
    if pct_feriado:
        p("feriado", f"Prêmio de feriado {pct_feriado}%", fmt_horas(fer),
          _c(r * fer / 60 * Decimal(pct_feriado) / 100))

    entra_taxa = 0
    for i, l in enumerate(lancamentos):
        rotulo, na_taxa = TIPOS_LANCAMENTO.get(l.get("tipo"), (l.get("tipo", ""), False))
        p(f"lanc{i}", l.get("descricao") or rotulo, rotulo if l.get("descricao") else "",
          int(l["centavos"]))
        if na_taxa:
            entra_taxa += int(l["centavos"])
    # FLSA "regular rate": bônus por meta e comissão aumentam o valor da hora extra.
    # Ajuste = (bônus ÷ horas trabalhadas) × 0,5 nas horas extras (× 1 no dobro).
    if entra_taxa and trabalhadas and (ot or dt) and not isento:
        extra_h = Decimal(entra_taxa) / trabalhadas * 60
        p("ajuste_taxa", "Ajuste da hora extra sobre bônus/comissão (regular rate)", "",
          _c(extra_h * ot / 60 * Decimal("0.5") + extra_h * dt / 60))
    bruto = sum(l["valor"] for l in linhas)
    gorjetas = sum(int(l["centavos"]) for l in lancamentos if str(l.get("tipo", "")).startswith("gorjeta"))
    return dict(linhas=linhas, bruto=bruto, gorjetas=gorjetas,
                horas_pagas=reg + ot + dt, trabalhadas=trabalhadas)


def alertas(calc: dict, taxa: int, isento: bool, salario_periodo: int, dias: int,
            gorjeta: bool) -> list[str]:
    """Avisos que o próprio sistema já consegue dar, sem o Claude."""
    a = []
    if isento:
        semanal = Decimal(salario_periodo) * 7 / max(dias, 1)
        if semanal < LIMITE_ISENTO_SEMANA:
            a.append("Salário de exempt abaixo de US$ 684 por semana: pela lei federal, esta "
                     "pessoa tem direito a hora extra.")
        return a
    if not taxa:
        a.append("Sem valor da hora cadastrado: o pagamento ficou zerado.")
        return a
    if calc["trabalhadas"]:
        # Gorjetas contam para o mínimo só com tip credit (funcionário marcado com gorjeta).
        base = calc["bruto"] - (0 if gorjeta else calc["gorjetas"])
        if Decimal(base) * 60 / calc["trabalhadas"] < SALARIO_MINIMO_FEDERAL:
            a.append("Média por hora abaixo do mínimo federal (US$ 7,25). Confira também o "
                     "mínimo do estado e da cidade.")
    if taxa < SALARIO_MINIMO_FEDERAL and not gorjeta:
        a.append("Valor da hora abaixo de US$ 7,25 sem ser funcionário com gorjeta.")
    return a
