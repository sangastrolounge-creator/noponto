"""Cálculo da folha de pagamento (holerite) a partir das horas de um período fechado.

Tudo em centavos. O cálculo é feito aqui, de forma exata e repetível; o Claude só revisa
(veja revisor.py). As tabelas de INSS e IRRF ficam editáveis no Master porque mudam todo ano."""
import json
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from .db import Config

CHAVE_TABELAS = "tabelas_folha"

# Valores de 2026 (INSS: Portaria do salário mínimo de R$ 1.621,00; IRRF: tabela progressiva
# com o redutor da Lei 15.270/2025, isenção até R$ 5.000). Confira com o contador todo ano.
TABELAS_PADRAO = {
    "vigencia": "2026",
    "salario_minimo": 162100,
    # teto da faixa (centavos), alíquota %
    "inss": [[162100, 7.5], [290284, 9], [435427, 12], [847555, 14]],
    # teto da faixa (centavos, 0 = sem teto), alíquota %, parcela a deduzir (centavos)
    "irrf": [[242880, 0, 0], [282665, 7.5, 18216], [375105, 15, 39416],
             [466468, 22.5, 67549], [0, 27.5, 90873]],
    "irrf_dependente": 18959,
    "irrf_simplificado": 60720,
    "irrf_minimo": 1000,          # retenção menor que R$ 10,00 não é feita
    # Redutor: até "isento_ate" zera o imposto (limitado a "redutor_max");
    # até "redutor_ate" o redutor é a − b × rendimento tributável.
    "isento_ate": 500000,
    "redutor_max": 31289,
    "redutor_ate": 735000,
    "redutor_a": 97862,
    "redutor_b": 0.133145,
    "fgts_pct": 8,
    "vt_pct": 6,
}


def tabelas(db: Session) -> dict:
    c = db.get(Config, CHAVE_TABELAS)
    t = dict(TABELAS_PADRAO)
    if c and c.valor:
        try:
            t.update(json.loads(c.valor))
        except ValueError:
            pass
    return t


def salvar_tabelas(db: Session, t: dict) -> None:
    c = db.get(Config, CHAVE_TABELAS)
    if not c:
        c = Config(chave=CHAVE_TABELAS, valor="")
        db.add(c)
    c.valor = json.dumps(t)


def _c(v) -> int:
    """Decimal em centavos -> inteiro, arredondando meio centavo para cima."""
    return int(Decimal(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def inss(base: int, t: dict) -> int:
    total, anterior = Decimal(0), 0
    for teto, pct in t["inss"]:
        if base > anterior:
            total += Decimal(min(base, teto) - anterior) * Decimal(str(pct)) / 100
        anterior = teto
    return _c(total)


def irrf(tributavel: int, inss_desc: int, dependentes: int, t: dict) -> tuple[int, dict]:
    """Imposto de renda retido: tabela progressiva, depois o redutor de 2026."""
    legais = inss_desc + dependentes * t["irrf_dependente"]
    simplificado = t["irrf_simplificado"]
    deducao = max(legais, simplificado)
    base = max(tributavel - deducao, 0)
    imposto = Decimal(0)
    for teto, pct, deduzir in t["irrf"]:
        if not teto or base <= teto:
            imposto = Decimal(base) * Decimal(str(pct)) / 100 - deduzir
            break
    imposto = max(imposto, Decimal(0))
    if tributavel <= t["isento_ate"]:
        redutor = min(imposto, Decimal(t["redutor_max"]))
    elif tributavel <= t["redutor_ate"]:
        redutor = Decimal(t["redutor_a"]) - Decimal(str(t["redutor_b"])) * tributavel
    else:
        redutor = Decimal(0)
    valor = _c(max(imposto - max(redutor, Decimal(0)), Decimal(0)))
    if valor < t.get("irrf_minimo", 0):
        valor = 0
    return valor, dict(base=base, deducao=deducao,
                       modo="simplificado" if simplificado > legais else "legal",
                       imposto_tabela=_c(imposto), redutor=_c(max(redutor, Decimal(0))))


def fmt_horas(m: int) -> str:
    return f"{m // 60}h{m % 60:02d}"


def calcular(salario: int, jornada: int, dependentes: int, vt: int, lancamentos: list,
             horas: dict, pct_normal: int, pct_especial: int, pct_noturno: int,
             controle: str, t: dict) -> dict:
    """Monta o holerite. `horas` traz os minutos da linha do fechamento.

    Devolve as linhas (proventos e descontos), as bases e os totais em centavos."""
    jornada = jornada or 220
    vh = Decimal(salario) / jornada                  # valor da hora normal, em centavos
    def horas_valor(minutos, fator=Decimal(1)):
        return _c(vh * minutos / 60 * fator)

    linhas = []
    def p(cod, desc, ref, valor, trib=True):
        if valor:
            linhas.append(dict(cod=cod, desc=desc, ref=ref, tipo="p", valor=valor, trib=trib))
    def d(cod, desc, ref, valor, reduz_base=False):
        if valor:
            linhas.append(dict(cod=cod, desc=desc, ref=ref, tipo="d", valor=valor,
                               reduz_base=reduz_base))

    p("salario", "Salário base", f"{jornada}h", salario)
    banco = controle == "banco"
    if not banco:
        en, ee = horas.get("extra_normal", 0), horas.get("extra_especial", 0)
        p("extra_normal", f"Horas extras {pct_normal}%", fmt_horas(en),
          horas_valor(en, 1 + Decimal(pct_normal) / 100))
        p("extra_especial", f"Horas extras {pct_especial}% (folga/feriado)", fmt_horas(ee),
          horas_valor(ee, 1 + Decimal(pct_especial) / 100))
        dsr_e = horas.get("dsr_extras", 0)
        p("dsr_extras", "DSR sobre horas extras", fmt_horas(dsr_e), horas_valor(dsr_e))
    noturno = horas.get("noturno", 0)
    p("noturno", f"Adicional noturno {pct_noturno}%", fmt_horas(noturno),
      horas_valor(noturno, Decimal(pct_noturno) / 100))
    # Intervalo intrajornada não concedido (art. 71, §4º): só o tempo que faltou, com pelo menos
    # 50% a mais. É indenização: não entra na base de INSS, FGTS e IRRF. Vale também no banco de horas.
    sup = horas.get("intervalo_suprimido", 0)
    pct_int = max(pct_normal, 50)
    p("intervalo_suprimido", f"Intervalo não concedido (art. 71, §4º) {pct_int}% · indenizatório",
      fmt_horas(sup), horas_valor(sup, 1 + Decimal(pct_int) / 100), trib=False)
    # Sobreaviso (CLT art. 244, §2º; Súmula 428 do TST): 1/3 do valor da hora normal.
    # Tem natureza salarial: entra na base de INSS, FGTS e IRRF.
    sob = horas.get("sobreaviso", 0)
    p("sobreaviso", "Sobreaviso (art. 244, §2º) 1/3 da hora", fmt_horas(sob),
      horas_valor(sob, Decimal(1) / 3))
    for i, l in enumerate(lancamentos):
        if l.get("tipo") == "p":
            p(f"lanc{i}", l["descricao"], "", int(l["centavos"]), bool(l.get("tributavel", True)))

    faltas = horas.get("faltas", 0)
    d("faltas", "Faltas", f"{faltas} dia(s) · {fmt_horas(horas.get('falta_min', 0))}",
      horas_valor(horas.get("falta_min", 0)), True)
    dsr_p = horas.get("dsr_perdido_min", 0)
    d("dsr_perdido", "DSR descontado (semana com falta)",
      f"{horas.get('dsr_perdidos', 0)} dia(s) · {fmt_horas(dsr_p)}", horas_valor(dsr_p), True)
    if not banco:
        deb = horas.get("debito", 0)
        d("atrasos", "Atrasos e saídas antecipadas", fmt_horas(deb), horas_valor(deb), True)

    tributavel = (sum(l["valor"] for l in linhas if l["tipo"] == "p" and l.get("trib"))
                  - sum(l["valor"] for l in linhas if l["tipo"] == "d" and l.get("reduz_base")))
    tributavel = max(tributavel, 0)
    inss_v = inss(tributavel, t)
    teto = t["inss"][-1][0]
    d("inss", "INSS", f"{(Decimal(inss_v) * 100 / tributavel).quantize(Decimal('0.01'))}%"
      if tributavel else "", inss_v)
    ir_v, ir_info = irrf(tributavel, inss_v, dependentes, t)
    d("irrf", "IRRF", f"{dependentes} dep." if dependentes else "", ir_v)
    if vt:
        vt_desc = min(_c(Decimal(salario) * Decimal(str(t["vt_pct"])) / 100), vt)
        d("vt", f"Vale-transporte ({t['vt_pct']}%)", "", vt_desc)
    for i, l in enumerate(lancamentos):
        if l.get("tipo") == "d":
            d(f"lanc{i}", l["descricao"], "", int(l["centavos"]))

    bruto = sum(l["valor"] for l in linhas if l["tipo"] == "p")
    descontos = sum(l["valor"] for l in linhas if l["tipo"] == "d")
    fgts = _c(Decimal(tributavel) * Decimal(str(t["fgts_pct"])) / 100)
    return dict(linhas=linhas, bruto=bruto, descontos=descontos, liquido=bruto - descontos,
                fgts=fgts, base_inss=min(tributavel, teto), base_fgts=tributavel,
                base_irrf=ir_info["base"], irrf_info=ir_info, valor_hora=_c(vh),
                tributavel=tributavel)


def alertas(item_calc: dict, salario: int, jornada: int, dias: int, t: dict) -> list[str]:
    """Avisos que o próprio sistema já consegue dar, sem precisar do Claude."""
    a = []
    if not salario:
        a.append("Sem salário cadastrado: o holerite ficou zerado.")
    else:
        minimo = Decimal(t["salario_minimo"]) * Decimal(jornada or 220) / 220
        if salario < minimo:
            a.append("Salário abaixo do mínimo proporcional à jornada.")
    if item_calc["liquido"] < 0:
        a.append("Líquido negativo: os descontos passam do que há para receber.")
    if not 28 <= dias <= 31:
        a.append(f"O período tem {dias} dias; o salário foi lançado como um mês inteiro.")
    return a
