"""Revisão do pagamento bruto pelo Claude (API da Anthropic).

O sistema calcula o bruto; o Claude refaz as contas por conta própria a partir das mesmas
horas e valores, e aponta divergências e riscos trabalhistas dos EUA. Roda em segundo plano depois que o
gestor pede a revisão; o resultado fica guardado na folha (FolhaPagamento.revisao)."""
import json
import logging
import os

from ...db import Config, FolhaPagamento, SessionLocal
from ... import db as banco

log = logging.getLogger("turno.revisor")

MODELO = os.environ.get("CLAUDE_MODELO", "claude-sonnet-5-5")
CHAVE_CONFIG = "anthropic_api_key"
LIMITE_DIA = 20  # revisões por empresa por dia

IDIOMAS = {"pt": "Brazilian Portuguese", "en": "English", "es": "Spanish"}

ESQUEMA = {
    "type": "object",
    "properties": {
        "resumo": {"type": "string"},
        "conferidos": {"type": "integer"},
        "divergencias": {"type": "integer"},
        "alertas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "funcionario": {"type": "string"},
                "gravidade": {"type": "string", "enum": ["alta", "media", "baixa"]},
                "mensagem": {"type": "string"},
            },
            "required": ["funcionario", "gravidade", "mensagem"],
            "additionalProperties": False,
        }},
    },
    "required": ["resumo", "conferidos", "divergencias", "alertas"],
    "additionalProperties": False,
}

SISTEMA = """You are a U.S. payroll and wage-and-hour compliance analyst reviewing the gross pay \
that a time-and-attendance system calculated for one pay period. The reader is the owner or \
manager of a small business (often a restaurant), not an accountant.

The data comes as JSON: the company's state and policies, and for each employee the hours of \
the period (in minutes), the pay data, and the gross pay lines the system produced (amounts in \
cents). Taxes and withholdings are NOT part of this calculation: a payroll provider handles them.

How the system calculates (redo the math with these rules and compare):
- Hours: federal FLSA overtime at 1.5x over 40 hours in the fixed workweek; the state's daily \
rules are already applied in the "regular", "ot" and "dt" minutes (e.g. California: over 8 h a \
day at 1.5x, over 12 h at 2x, 7th consecutive day). Take the hour split as given; do not \
re-derive it from punches you don't have.
- regular pay = regular minutes / 60 x hourly rate; overtime = ot / 60 x rate x 1.5; double time = \
dt / 60 x rate x 2; meal premium = meal_premios / 60 x rate; rest break premium = descanso_premios / 60 x rate; predictability pay = previsibilidade_min / 60 x rate + previsibilidade_centavos; paid leave = pto_min / 60 x rate.
- night differential and holiday premium are optional company policies: minutes / 60 x rate x \
percent.
- Non-discretionary bonuses and commissions raise the FLSA regular rate: adjustment = (bonus / \
hours worked) x 0.5 per overtime hour (x 1 per double-time hour).
- Exempt employees get their period salary and no overtime.
Each line is rounded to the cent, half up.

What to do:
1. Recompute every employee's lines. Put in "conferidos" how many you checked and in \
"divergencias" how many have a line off by more than 5 cents; for each, an alert with \
"gravidade" "alta" naming the line, the system's value and yours.
2. Flag wage-and-hour risks the data shows: pay below "salario_minimo_local" (the federal, \
state or city minimum the system uses for this company; for tipped employees compare the cash \
wage with "gorjeta_dinheiro_centavos"); tipped employees whose cash wage plus tips \
may not reach the minimum (tip credit); exempt salary below $684/week; heavy overtime; \
missed meal breaks or meal premiums (California); no-shows and late arrivals that suggest \
schedule problems; manual entries that look wrong (discretionary vs non-discretionary bonus, \
reimbursement amounts). Use "media" for things to check and "baixa" for reminders.
3. Do not invent facts that are not in the data, and do not cover what this calculation does \
not do (tax withholding, benefits, filings); mention that once in the summary if useful.
4. "resumo": 2 to 4 plain sentences with the overall result and the total gross pay.
Use the employee's name as it appears in the data; for general notes use "Geral".
Write everything in {idioma}."""


def chave(db=None) -> str:
    v = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if v:
        return v
    fechar = db is None
    db = db or SessionLocal()
    try:
        c = db.get(Config, CHAVE_CONFIG)
        return (c.valor or "").strip() if c else ""
    finally:
        if fechar:
            db.close()


def configurado(db=None) -> bool:
    return bool(chave(db))


def dados_da_folha(f: FolhaPagamento) -> dict:
    from .apuracao import CHAVES
    fe = f.fechamento
    e = f.fechamento.linhas[0].usuario.empresa if fe.linhas else None
    itens = []
    for it in sorted(f.itens, key=lambda i: i.usuario.nome.lower()):
        calc = json.loads(it.calculo or "{}")
        linha = next((l for l in fe.linhas if l.id == it.linha_id), None)
        horas = {k: getattr(linha, k) for k in CHAVES} if linha else {}
        itens.append(dict(
            funcionario=it.usuario.nome, funcao=it.usuario.cargo,
            valor_hora=it.taxa_hora_centavos, exempt=it.isento,
            salario_periodo=it.salario_periodo_centavos, recebe_gorjeta=it.usuario.gorjeta,
            lancamentos_manuais=json.loads(it.lancamentos or "[]"), horas_minutos=horas,
            linhas=[{k: l[k] for k in ("cod", "desc", "ref", "valor")}
                    for l in calc.get("linhas", [])],
            bruto=it.bruto, alertas_do_sistema=calc.get("alertas", [])))
    return dict(
        periodo=f"{f.inicio:%m/%d/%Y} to {f.fim:%m/%d/%Y}",
        dias_no_periodo=(f.fim - f.inicio).days + 1, estado=fe.estado,
        salario_minimo_local=_minimos(e, f.fim) if e else None,
        frequencia_pagamento=e.frequencia if e else "",
        politicas=dict(diferencial_noturno_pct=fe.pct_noturno, premio_feriado_pct=fe.pct_feriado),
        funcionarios=itens, total_bruto=sum(i.bruto for i in f.itens))


def _minimos(e, d) -> dict:
    """Mínimo do local no fim do período (centavos/h), da tabela do sistema."""
    from . import salario_minimo as sm
    n, g = sm.minimo(e, d), sm.minimo(e, d, True)
    return {"normal_centavos": n["centavos"], "gorjeta_dinheiro_centavos": g["centavos"], "fonte": n["fonte"]}


def _gravar(folha_id: int, situacao: str, revisao: dict) -> None:
    with SessionLocal() as db:
        f = db.get(FolhaPagamento, folha_id)
        if f:
            f.revisao_situacao = situacao
            f.revisao = json.dumps(revisao, ensure_ascii=False)
            f.revisao_em = banco.agora()
            db.commit()


def revisar(folha_id: int, idioma: str = "pt") -> None:
    """Chamado em segundo plano. Nunca levanta erro: grava 'erro' com a explicação."""
    import anthropic  # só carrega quando alguém pede uma revisão

    with SessionLocal() as db:
        f = db.get(FolhaPagamento, folha_id)
        if not f:
            return
        dados = dados_da_folha(f)
        api_key = chave(db)
    if not api_key:
        _gravar(folha_id, "erro", {"erro": "A chave da API do Claude não está configurada."})
        return
    client = anthropic.Anthropic(api_key=api_key, timeout=300, max_retries=2)
    try:
        resp = client.beta.messages.create(
            model=MODELO,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": ESQUEMA}},
            system=SISTEMA.replace("{idioma}", IDIOMAS.get(idioma, IDIOMAS["pt"])),
            messages=[{"role": "user", "content":
                       "Review this pay period:\n\n"
                       + json.dumps(dados, ensure_ascii=False)}],
        )
    except anthropic.AuthenticationError:
        _gravar(folha_id, "erro", {"erro": "A chave da API do Claude foi recusada. Confira no Master."})
        return
    except anthropic.RateLimitError:
        _gravar(folha_id, "erro", {"erro": "O Claude está com muitos pedidos agora. Tente de novo em alguns minutos."})
        return
    except anthropic.APIStatusError as ex:
        log.warning("revisão da folha %s: erro %s %s", folha_id, ex.status_code, ex.message)
        _gravar(folha_id, "erro", {"erro": f"O Claude respondeu com erro ({ex.status_code}). Tente de novo mais tarde."})
        return
    except anthropic.APIConnectionError:
        _gravar(folha_id, "erro", {"erro": "Não foi possível falar com o Claude (conexão). Tente de novo."})
        return
    if resp.stop_reason == "refusal":
        _gravar(folha_id, "erro", {"erro": "O Claude não conseguiu revisar esta folha."})
        return
    if resp.stop_reason == "max_tokens":
        _gravar(folha_id, "erro", {"erro": "A resposta do Claude ficou grande demais. Tente de novo."})
        return
    texto = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        resultado = json.loads(texto)
    except ValueError:
        _gravar(folha_id, "erro", {"erro": "A resposta do Claude veio num formato inesperado."})
        return
    resultado["modelo"] = resp.model
    _gravar(folha_id, "pronta", resultado)
