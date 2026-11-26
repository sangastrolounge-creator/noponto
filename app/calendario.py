"""Calendário da página Ponto: feriados nacionais, pontos facultativos, feriados cadastrados
pela empresa e datas comemorativas, com o resumo do ponto de cada dia."""
import calendar
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import paises
from .apuracao import pascoa
from .db import Feriado

MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
         "Setembro", "Outubro", "Novembro", "Dezembro"]


def _nesimo_dia_semana(ano: int, mes: int, dia_semana: int, n: int) -> date:
    """n-ésimo dia da semana do mês (dia_semana: 0 = segunda ... 6 = domingo)."""
    d = date(ano, mes, 1)
    d += timedelta(days=(dia_semana - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def facultativos(ano: int) -> list[tuple[date, str]]:
    """Pontos facultativos nacionais: não são feriados por lei, a empresa decide."""
    p = pascoa(ano)
    return [(p - timedelta(days=48), "Carnaval (segunda)"), (p - timedelta(days=47), "Carnaval"),
            (p - timedelta(days=46), "Quarta-feira de Cinzas"), (p + timedelta(days=60), "Corpus Christi")]


def comemorativas(ano: int) -> list[tuple[date, str]]:
    """Datas comemorativas que costumam mexer com o movimento (comércio, bares e restaurantes)."""
    fixas = [(3, 8, "Dia Internacional da Mulher"), (3, 15, "Dia do Consumidor"),
             (6, 12, "Dia dos Namorados"), (6, 24, "São João"), (7, 10, "Dia da Pizza"),
             (8, 11, "Dia do Garçom"), (9, 15, "Dia do Cliente"),
             (10, 30, "Dia do Comerciário"), (10, 12, "Dia das Crianças"),
             (12, 24, "Véspera de Natal"), (12, 31, "Véspera de Ano-Novo")]
    lista = [(date(ano, m, d), nome) for m, d, nome in fixas]
    lista += [(pascoa(ano), "Páscoa"),
              (_nesimo_dia_semana(ano, 5, 6, 2), "Dia das Mães"),
              (_nesimo_dia_semana(ano, 8, 6, 2), "Dia dos Pais"),
              (_nesimo_dia_semana(ano, 11, 4, 4), "Black Friday")]
    return sorted(lista)


def datas_do_ano(db: Session, e, ano: int) -> dict[date, list[tuple[str, str]]]:
    """{dia: [(tipo, nome)]} com tipo 'feriado', 'facultativo' ou 'comemorativa'.
    Feriado cadastrado pela empresa (Ausências -> Feriados) é o que vale no ponto."""
    mapa: dict[date, list[tuple[str, str]]] = {}
    cadastrados = {f.data: f.nome for f in db.scalars(select(Feriado).where(
        Feriado.empresa_id == e.id, Feriado.data.between(date(ano, 1, 1), date(ano, 12, 31))))}
    for d, nome in cadastrados.items():
        mapa.setdefault(d, []).append(("feriado", nome))
    r = paises.regras_de(e)
    for d, nome in r.feriados(ano):
        if d not in cadastrados:
            mapa.setdefault(d, []).append(("nacional", nome))
    for d, nome in r.facultativos(ano):
        if d not in cadastrados:
            mapa.setdefault(d, []).append(("facultativo", nome))
    for d, nome in r.comemorativas(ano):
        if not any(nome == n for _t, n in mapa.get(d, [])):
            mapa.setdefault(d, []).append(("comemorativa", nome))
    return mapa


def mes(db: Session, e, ano: int, mes_n: int, linhas: list[dict], hoje: date) -> dict:
    """Grade do mês (semanas começando na segunda) com as datas e o ponto de cada dia.
    `linhas`: dias do espelho de ponto (regras.espelho) para resumir pessoas, horas e alertas."""
    datas = datas_do_ano(db, e, ano)
    por_dia: dict[date, dict] = {}
    for l in linhas:
        r = por_dia.setdefault(l["dia"], dict(pessoas=0, minutos=0, atrasos=0, faltando=0))
        r["pessoas"] += 1
        r["minutos"] += l["minutos"]
        r["atrasos"] += 1 if l["atraso"] > (e.tolerancia_min or 0) else 0
        r["faltando"] += 1 if l["incompleto"] else 0
    semanas = []
    for semana in calendar.Calendar(firstweekday=0).monthdatescalendar(ano, mes_n):
        semanas.append([dict(dia=d, fora=d.month != mes_n, hoje=d == hoje,
                             fim_de_semana=d.weekday() >= 5, datas=datas.get(d, []),
                             ponto=por_dia.get(d)) for d in semana])
    anterior = date(ano, mes_n, 1) - timedelta(days=1)
    seguinte = date(ano, mes_n, 28) + timedelta(days=4)
    do_mes = sorted((d, itens) for d, itens in datas.items() if d.year == ano and d.month == mes_n)
    return dict(ano=ano, mes=mes_n, nome=f"{MESES[mes_n - 1]} de {ano}", semanas=semanas,
                anterior=f"{anterior:%Y-%m}", seguinte=f"{seguinte:%Y-%m}", lista=do_mes)
