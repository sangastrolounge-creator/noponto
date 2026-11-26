"""Faturamento mensal e CMO (custo de mão de obra em % do faturamento) — opcional, para empresas
que vendem produtos ou serviços. O custo vem dos salários mensais da equipe + encargos estimados
(no Brasil o salário é mensal: não é calculado por hora). Quem recebe por hora entra pelas horas
batidas no mês."""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import avisar, exigir_csrf, gestor_logado, ir, ler_centavos, pode
from .db import FaturamentoMes, FuncaoUsuario, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
REGIMES = {"simples": "Simples Nacional", "presumido": "Lucro Presumido ou Real"}
ALIMENTACAO = ("restaurante", "cafe", "fastfood")
SERVICOS = ("servicos", "beleza", "clinica")

# Encargos sobre o salário (estimativa mensal, em fração do salário):
# FGTS 8% + 13º (1/12) + férias com 1/3 (1/12 × 4/3) + FGTS sobre 13º e férias.
_PROVISOES = Decimal(1) / 12 + Decimal(4) / 36                      # 13º + férias com 1/3 ≈ 19,44%
_ENC_SIMPLES = Decimal("0.08") + _PROVISOES * Decimal("1.08")       # ≈ 29,0%
# Lucro Presumido/Real: + INSS patronal 20% + RAT ~2% + terceiros 5,8% sobre salário, 13º e férias.
_ENC_PRESUMIDO = _ENC_SIMPLES + Decimal("0.278") * (1 + _PROVISOES)  # ≈ 62,2%
ENCARGOS = {  # classificação -> regime -> fração
    "clt": {"simples": _ENC_SIMPLES, "presumido": _ENC_PRESUMIDO},
    "intermitente": {"simples": _ENC_SIMPLES, "presumido": _ENC_PRESUMIDO},
    "aprendiz": {"simples": Decimal("0.02") + _PROVISOES * Decimal("1.02"),
                 "presumido": Decimal("0.02") + _PROVISOES * Decimal("1.02") + Decimal("0.278") * (1 + _PROVISOES)},
    "estagio": {"simples": Decimal(0), "presumido": Decimal(0)},      # bolsa-estágio: sem encargos
    "pj": {"simples": Decimal(0), "presumido": Decimal(0)},
}


def _c(v) -> int:
    return int(Decimal(v).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def chave_mes(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def limites_mes(chave: str) -> tuple[date, date]:
    ano, mes = (int(x) for x in chave.split("-"))
    ini = date(ano, mes, 1)
    return ini, (ini + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def nome_mes(chave: str) -> str:
    ano, mes = chave.split("-")
    return f"{MESES[int(mes) - 1]}/{ano}"


def custo_mensal(db: Session, e, chave: str, hoje: date) -> dict:
    """Custo da equipe no mês: salário (ou horas × valor-hora para quem recebe por hora) +
    encargos estimados conforme o regime + vale-transporte. Mês corrente: mês cheio."""
    ini, fim = limites_mes(chave)
    regime = e.regime_tributario if e.regime_tributario in REGIMES else "simples"
    equipe = list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.papel == "funcionario")))
    principal = {f.usuario_id: f for f in db.scalars(select(FuncaoUsuario).where(
        FuncaoUsuario.empresa_id == e.id).order_by(FuncaoUsuario.ordem.desc(), FuncaoUsuario.id.desc()))}
    minutos = None
    sal = enc = vt = 0
    pessoas, sem_valor = 0, []
    for u in equipe:
        if u.data_admissao and u.data_admissao > fim:
            continue
        f = principal.get(u.id)
        if f and f.tipo == "hora" and f.valor_centavos:
            if minutos is None:
                _l, tot = regras.espelho(db, e, ini, min(fim, hoje))
                minutos = {t["u"].id: t["minutos"] for t in tot}
            base = _c(Decimal(f.valor_centavos) * minutos.get(u.id, 0) / 60)
        elif f and f.tipo == "anual":   # EUA: salário anual (exempt), um doze avos no mês
            base = _c(Decimal(f.valor_centavos) / 12)
        else:
            base = u.salario_centavos or (f.valor_centavos if f and f.tipo == "mensal" else 0)
        if not base:
            sem_valor.append(u.nome)
            continue
        pessoas += 1
        sal += base
        if (e.pais or "BR") == "BR":   # encargos e vale-transporte são do Brasil
            enc += _c(Decimal(base) * ENCARGOS.get(u.classificacao, ENCARGOS["clt"])[regime])
            vt += u.vt_centavos or 0
    return dict(salarios=sal, encargos=enc, vt=vt, total=sal + enc + vt, pessoas=pessoas,
                sem_valor=sem_valor, regime=regime)


def faixa(pct: float | None, meta: int) -> str:
    if pct is None:
        return ""
    return "ok" if pct <= meta else "alerta" if pct <= meta + 5 else "erro"


def historico(db: Session, e, hoje: date, n: int = 6) -> list[dict]:
    """Últimos n meses (o atual primeiro): faturamento informado, CMO e % do faturamento."""
    chaves, d = [], hoje.replace(day=1)
    for _ in range(n):
        chaves.append(chave_mes(d))
        d = (d - timedelta(days=1)).replace(day=1)
    fat = {f.mes: f.valor_centavos for f in db.scalars(select(FaturamentoMes).where(
        FaturamentoMes.empresa_id == e.id, FaturamentoMes.mes.in_(chaves)))}
    meta = e.meta_cmo or 30
    linhas = []
    for k in chaves:
        c = custo_mensal(db, e, k, hoje)
        f = fat.get(k)
        pct = round(c["total"] * 100 / f, 1) if f else None
        linhas.append(dict(mes=k, nome=nome_mes(k), faturamento=f, custo=c, pct=pct,
                           faixa=faixa(pct, meta), atual=k == chave_mes(hoje)))
    return linhas


def orientacao(e) -> str:
    meta = e.meta_cmo or 30
    if e.segmento in ALIMENTACAO:
        return (f"Em bares, restaurantes e cafés, o CMO saudável costuma ficar até cerca de 30% do "
                f"faturamento; passar de 35% costuma comprometer o lucro. Sua meta: {meta}%.")
    if e.segmento in SERVICOS:
        return (f"Em serviços a equipe pesa mais no faturamento. Sua meta: {meta}%. No Simples Nacional, "
                f"folha a partir de 28% da receita (Fator R) pode baixar o imposto de algumas atividades: "
                f"confira com o seu contador.")
    return (f"A referência varia conforme o ramo. Sua meta: {meta}% (ajuste em Configurações). "
            f"Acompanhe a tendência mês a mês.")


@rota.post("/painel/faturamento", dependencies=CSRF)
def faturamento_salvar(request: Request, mes: str = Form(""), valor: str = Form(""),
                       u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    if not pode(u, "pagamento"):
        avisar(request, "Você não tem acesso a esta parte do painel. Fale com o dono da conta.", "erro")
        return ir("/painel")
    hoje = regras.hoje(u.empresa)
    try:
        ini, _fim = limites_mes(mes)
    except (ValueError, AttributeError):
        ini = None
    centavos = ler_centavos(valor) if valor.strip() else 0
    if not ini or ini > hoje or centavos is None or centavos > 10**12:
        avisar(request, "Informe o mês e o faturamento (ex.: 85.000,00).", "erro")
        return ir("/painel#faturamento")
    f = db.scalar(select(FaturamentoMes).where(FaturamentoMes.empresa_id == u.empresa_id,
                                               FaturamentoMes.mes == mes))
    if not centavos:
        if f:
            db.delete(f)
    else:
        if not f:
            f = FaturamentoMes(empresa_id=u.empresa_id, mes=mes)
            db.add(f)
        f.valor_centavos, f.registrado_por, f.atualizado_em = centavos, u.nome, banco.agora()
    db.commit()
    avisar(request, "Faturamento salvo.")
    return ir("/painel#faturamento")


@rota.post("/painel/config/faturamento", dependencies=CSRF)
def config_faturamento(request: Request, usa: str = Form(""), meta: str = Form("30"),
                       regime: str = Form("simples"), u: Usuario = Depends(gestor_logado),
                       db: Session = Depends(get_db)):
    e = u.empresa
    try:
        m = int(meta)
    except ValueError:
        m = -1
    if not 5 <= m <= 80 or regime not in REGIMES:
        avisar(request, "Meta de CMO: use um número entre 5 e 80.", "erro")
        return ir("/painel/config#faturamento")
    e.usa_faturamento, e.meta_cmo, e.regime_tributario = bool(usa), m, regime
    db.commit()
    avisar(request, "Faturamento e CMO ligados: aparecem na tela Hoje." if usa else "Faturamento e CMO desligados.")
    return ir("/painel/config#faturamento")
