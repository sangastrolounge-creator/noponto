"""Regras do Brasil: CLT, banco de horas, extras 50%/100%, DSR, adicional noturno com hora
reduzida, feriados nacionais, folha com INSS/IRRF/FGTS e cobrança pelo Asaas.

Este módulo só aponta para o código que já existia; nada muda no resultado."""
from .. import apuracao, calculo_folha, calendario

CODIGO = "BR"
NOME = "Brasil"
MOEDA = "BRL"
IDIOMA = "pt"
COBRANCA = "asaas"


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


def apurar(db, e, ini, fim, usuario_id=None):
    return apuracao.apurar(db, e, ini, fim, usuario_id)


def fechar(db, e, ini, fim, autor):
    return apuracao.fechar(db, e, ini, fim, autor)


def calcular_folha(*args, **kwargs):
    return calculo_folha.calcular(*args, **kwargs)


def alertas_folha(*args, **kwargs):
    return calculo_folha.alertas(*args, **kwargs)
