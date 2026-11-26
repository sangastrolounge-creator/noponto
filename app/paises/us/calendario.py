"""Datas do calendário dos EUA: dia observado dos feriados federais e datas comemorativas."""
from datetime import date, timedelta

from ...apuracao import pascoa
from .apuracao import _n_dia_semana, feriados_nacionais


def observados(ano: int) -> list[tuple[date, str]]:
    """Feriado federal que cai no sábado é observado na sexta; no domingo, na segunda."""
    lista = []
    for d, nome in feriados_nacionais(ano) + feriados_nacionais(ano + 1)[:1]:
        if d.weekday() == 5:
            obs = d - timedelta(days=1)
        elif d.weekday() == 6:
            obs = d + timedelta(days=1)
        else:
            continue
        if obs.year == ano:
            lista.append((obs, f"{nome} (observed)"))
    return lista


def comemorativas(ano: int) -> list[tuple[date, str]]:
    """Datas comemorativas que costumam mexer com o movimento (varejo, bares e restaurantes)."""
    fixas = [(2, 14, "Valentine's Day"), (3, 17, "St. Patrick's Day"), (5, 5, "Cinco de Mayo"),
             (10, 31, "Halloween"), (12, 24, "Christmas Eve"), (12, 31, "New Year's Eve")]
    lista = [(date(ano, m, d), nome) for m, d, nome in fixas]
    thanksgiving = _n_dia_semana(ano, 11, 3, 4)
    lista += [(_n_dia_semana(ano, 2, 6, 2), "Super Bowl Sunday"),
              (pascoa(ano), "Easter"),
              (_n_dia_semana(ano, 5, 6, 2), "Mother's Day"),
              (_n_dia_semana(ano, 6, 6, 3), "Father's Day"),
              (thanksgiving + timedelta(days=1), "Black Friday")]
    return sorted(lista)


