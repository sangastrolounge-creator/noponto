"""Regras do Brasil: CLT, banco de horas, extras 50%/100%, DSR, adicional noturno com hora
reduzida, feriados nacionais, folha com INSS/IRRF/FGTS e cobrança pelo Asaas.

Este módulo só aponta para o código que já existia; nada muda no resultado."""
from datetime import timedelta

from .. import apuracao, calculo_folha, calendario, escala_justa, menores, salario_minimo  # noqa: F401

CODIGO = "BR"
NOME = "Brasil"
MOEDA = "BRL"
IDIOMA = "pt"
COBRANCA = "asaas"
SEMANA_LIMITE_MIN = apuracao.SEMANA_CLT_MIN   # acima disso a semana passa a ter hora extra


def feriados(ano):
    return apuracao.feriados_nacionais(ano)


def facultativos(ano):
    return calendario.facultativos(ano)


def comemorativas(ano):
    return calendario.comemorativas(ano)


def inicio_semana(e, d):
    return apuracao.inicio_semana(e, d)


def periodo_aberto(db, e):
    return apuracao.periodo_aberto(db, e)


def periodo_sugerido(db, e):
    """Do início do período aberto até o fim daquele mês (ou até hoje, se o mês não acabou)."""
    ini, hoje = apuracao.periodo_aberto(db, e)
    fim_mes = (ini.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return ini, min(fim_mes, hoje)


# Totais do resumo da folha da equipe
CHAVES_RESUMO = ("trabalhado", "noturno", "extra_normal", "extra_especial", "falta_min", "faltas",
                 "dsr_perdidos", "dsr_perdido_min", "dsr_extras", "intervalo_suprimido", "sobreaviso")
# Ausências que o próprio funcionário pode pedir pelo app
TIPOS_PEDIDO_AUSENCIA = ("atestado", "abono", "outro")


def ao_reabrir(db, f):
    """Nada além do que o fechamento comum já desfaz."""


def apurar(db, e, ini, fim, usuario_id=None):
    return apuracao.apurar(db, e, ini, fim, usuario_id)


def fechar(db, e, ini, fim, autor):
    return apuracao.fechar(db, e, ini, fim, autor)


def calcular_folha(*args, **kwargs):
    return calculo_folha.calcular(*args, **kwargs)


def alertas_folha(*args, **kwargs):
    return calculo_folha.alertas(*args, **kwargs)
