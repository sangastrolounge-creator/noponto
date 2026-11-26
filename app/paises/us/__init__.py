"""Regras dos EUA: FLSA (extra acima de 40 h/semana) e regras dos estados (CA, AK, CO, NV),
meal e rest premium da Califórnia, feriados federais, períodos semanal, quinzenal, semimensal
e mensal, sick leave e PTO, Fair Workweek. Sem banco de horas, DSR nem hora noturna reduzida.

A folha americana é só o bruto (gross pay): os impostos ficam com o provedor de folha."""
from . import apuracao, calendario

CODIGO = "US"
NOME = "Estados Unidos"
MOEDA = "USD"
IDIOMA = "en"
COBRANCA = "stripe"


def feriados(ano):
    return apuracao.feriados_nacionais(ano)


def facultativos(ano):
    return calendario.observados(ano)


def comemorativas(ano):
    return calendario.comemorativas(ano)


def inicio_semana(e, d):
    return apuracao.inicio_semana(e, d)


def periodo_aberto(db, e):
    return apuracao.periodo_aberto(db, e)


def apurar(db, e, ini, fim, usuario_id=None):
    return apuracao.apurar(db, e, ini, fim, usuario_id)


def fechar(db, e, ini, fim, autor):
    return apuracao.fechar(db, e, ini, fim, autor)


def calcular_folha(*args, **kwargs):
    raise NotImplementedError("Folha dos EUA (gross pay) entra na tela própria de pagamento.")


def alertas_folha(*args, **kwargs):
    return []
