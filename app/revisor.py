"""Revisão da folha de pagamento pelo Claude (API da Anthropic).

O sistema calcula a folha; o Claude refaz as contas por conta própria a partir das mesmas
horas, salários e tabelas, e aponta divergências e riscos. Roda em segundo plano depois que o
gestor pede a revisão; o resultado fica guardado na folha (FolhaPagamento.revisao)."""
import json
import logging
import os

from .db import Config, FolhaPagamento, SessionLocal
from . import db as banco

log = logging.getLogger("turno.revisor")

MODELO = os.environ.get("CLAUDE_MODELO", "claude-sonnet-5-5")
CHAVE_CONFIG = "anthropic_api_key"
LIMITE_DIA = 20  # revisões por empresa por dia

IDIOMAS = {"pt": "português do Brasil", "en": "English", "es": "español"}

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

SISTEMA = """Você é um analista de departamento pessoal brasileiro revisando uma folha de \
pagamento que um sistema de controle de ponto calculou. Quem lê sua revisão é o dono ou gestor \
de uma pequena empresa, sem formação contábil.

Os dados vêm em JSON: as regras da empresa, as tabelas de INSS e IRRF usadas, e, para cada \
funcionário, as horas do período (em minutos), os dados salariais e o holerite que o sistema \
gerou (valores em centavos).

Como o sistema calcula (refaça as contas com estas regras e compare):
- valor da hora = salário ÷ jornada mensal (horas).
- extras = minutos ÷ 60 × valor da hora × (1 + percentual). Em controle "banco" as extras, \
atrasos e o DSR sobre extras não são pagos (vão para o banco de horas).
- DSR sobre extras: os minutos de dsr_extras já estão em horas normais; × valor da hora.
- adicional noturno = minutos noturnos (já com a hora reduzida) ÷ 60 × valor da hora × percentual.
- intervalo não concedido (art. 71, §4º) = minutos de intervalo_suprimido ÷ 60 × valor da hora × \
(1 + o maior entre 50% e o percentual da extra em dia normal). É indenizatório: não entra na base \
de INSS, IRRF e FGTS, e é pago também no controle "banco".
- faltas, DSR descontado e atrasos = minutos ÷ 60 × valor da hora; reduzem a base de INSS/IRRF/FGTS.
- INSS progressivo por faixas até o teto. IRRF: base = tributável − maior entre (INSS + \
dependentes × dedução) e o desconto simplificado; aplica a tabela; depois o redutor de 2026 \
calculado sobre o rendimento tributável; retenção abaixo do mínimo não é feita.
- vale-transporte: até o percentual do salário base, limitado ao custo informado.
- FGTS = percentual sobre a base tributável (depósito da empresa, não é desconto).
Arredondamento: meio centavo para cima em cada linha do holerite. O INSS é somado faixa a \
faixa sem arredondar e só o total é arredondado (por isso o desconto no teto é R$ 988,09). \
Diferenças de até 5 centavos não são divergência e não precisam de alerta.

O que fazer:
1. Refaça cada holerite. Conte em "conferidos" quantos você refez e em "divergencias" quantos \
têm diferença maior que 5 centavos em alguma linha; para cada divergência, crie um alerta de \
gravidade "alta" dizendo a linha, o valor do sistema e o valor que você encontrou.
2. Aponte riscos trabalhistas ou de preenchimento que os dados mostram: salário zerado ou abaixo \
do mínimo proporcional, período que não é um mês, muitas faltas, extras muito altas para o \
período, líquido muito baixo ou negativo, lançamentos manuais que parecem errados \
(ex.: descontos maiores que o salário, bônus marcado como não tributável), dependentes ou VT \
fora do comum. Use "media" para o que precisa de atenção e "baixa" para lembretes.
3. Não invente fatos que não estão nos dados e não cobre o que a folha não pretende fazer \
(encargos da empresa, eSocial, férias e 13º); se for útil, cite isso uma vez no resumo.
4. "resumo": 2 a 4 frases simples com o resultado geral e o total líquido da folha.
Use o nome do funcionário como aparece nos dados; para avisos gerais use "Geral".
Escreva tudo em {idioma}."""


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
    fe = f.fechamento
    itens = []
    for it in sorted(f.itens, key=lambda i: i.usuario.nome.lower()):
        calc = json.loads(it.calculo or "{}")
        linha = next((l for l in fe.linhas if l.id == it.linha_id), None)
        horas = {k: getattr(linha, k) for k in (
            "previsto", "trabalhado", "extra_normal", "extra_especial", "noturno", "debito",
            "faltas", "falta_min", "dias_ausencia", "dsr_perdidos", "dsr_perdido_min",
            "dsr_extras", "intervalo_suprimido")} if linha else {}
        itens.append(dict(
            funcionario=it.usuario.nome, funcao=it.usuario.cargo,
            salario=it.salario_centavos, jornada_mensal=it.jornada_mensal,
            dependentes=it.dependentes, custo_vale_transporte=it.vt_centavos,
            lancamentos_manuais=json.loads(it.lancamentos or "[]"), horas_minutos=horas,
            holerite=[{k: l[k] for k in ("desc", "ref", "tipo", "valor")}
                      for l in calc.get("linhas", [])],
            bruto=it.bruto, descontos=it.descontos, liquido=it.liquido, fgts=it.fgts,
            base_inss=calc.get("base_inss"), base_irrf=calc.get("base_irrf"),
            irrf_detalhe=calc.get("irrf_info")))
    return dict(
        competencia=f.competencia, periodo=f"{f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y}",
        dias_no_periodo=(f.fim - f.inicio).days + 1, controle=f.controle,
        percentuais=dict(extra_dia_normal=fe.pct_normal, extra_folga_feriado=fe.pct_especial,
                         adicional_noturno=fe.pct_noturno),
        tabelas=json.loads(f.tabelas or "{}"), funcionarios=itens,
        total_liquido=sum(i.liquido for i in f.itens))


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
                       "Revise esta folha de pagamento:\n\n"
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
