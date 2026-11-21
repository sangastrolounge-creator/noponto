"""Relatórios do gestor: escalado × trabalhado, pontualidade, faltas, notas dos turnos e custo
por cargo, num período escolhido (padrão: últimos 14 dias)."""
import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import apuracao, custos, regras, setores
from .core import data_iso, gestor_logado, pode, render
from .db import AvaliacaoTurno, Batida, Turno, Usuario, get_db
from .i18n import _

rota = APIRouter()
MAX_DIAS = 92


def montar(db: Session, e, ini: date, fim: date, setor: int = 0) -> dict:
    hoje = regras.hoje(e)
    agora_l = regras.agora_local(e)
    fim_ef = min(fim, hoje)
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id))
               if setores.no_setor(u, setor)}
    linhas: dict[int, dict] = {}

    def linha(uid):
        if uid not in linhas:
            linhas[uid] = {"u": pessoas[uid], "turnos": 0, "escala": 0, "trab": 0, "pontuais": 0,
                           "atrasos": 0, "atraso_min": 0, "faltas": 0, "notas": [], "custo": 0,
                           "noturno": 0, "noturno_valor": 0}
        return linhas[uid]

    bats: dict[tuple[int, date], list[Batida]] = {}
    for b in db.scalars(select(Batida).where(Batida.empresa_id == e.id, Batida.dia.between(ini, fim_ef),
                                             regras.VALIDA).order_by(Batida.momento)):
        bats.setdefault((b.usuario_id, b.dia), []).append(b)
    aus = apuracao.ausencias_por_dia(db, e, ini, fim_ef) if fim_ef >= ini else {}
    for t in db.scalars(select(Turno).where(Turno.empresa_id == e.id, Turno.data.between(ini, fim_ef),
                                            Turno.folga.is_(False))):
        if t.usuario_id not in pessoas or not t.inicio or not t.fim:
            continue
        t_ini, t_fim = regras.intervalo(t)
        if t_ini > agora_l or (t.usuario_id, t.data) in aus:
            continue
        x = linha(t.usuario_id)
        x["turnos"] += 1
        x["escala"] += regras.minutos_turno(t)
        bs = bats.get((t.usuario_id, t.data))
        if not bs:
            if t_fim < agora_l:
                x["faltas"] += 1
            continue
        if bs[0].tipo == "entrada":
            atraso = bs[0].atraso_min or 0
            if atraso > e.tolerancia_min:
                x["atrasos"] += 1
                x["atraso_min"] += atraso
            else:
                x["pontuais"] += 1
    d = ini
    while d <= fim_ef:
        for uid, m in custos.minutos_trabalhados(db, e, d).items():
            if uid not in pessoas:
                continue
            x = linha(uid)
            x["trab"] += m
            u = pessoas[uid]
            x["custo"] += round(m * custos.valor_hora(u) / 60)
        d += timedelta(days=1)
    # Horas noturnas (22h às 5h, já com a hora reduzida quando a empresa usa) e o adicional.
    if fim_ef >= ini:
        for ap in apuracao.apurar(db, e, ini, fim_ef):
            if ap["noturno"] and ap["u"].id in pessoas:
                x = linha(ap["u"].id)
                x["noturno"] = ap["noturno"]
                x["noturno_valor"] = round(ap["noturno"] * custos.valor_hora(ap["u"]) * e.pct_noturno / 6000)
    comentarios = []
    for a in db.scalars(select(AvaliacaoTurno).where(AvaliacaoTurno.empresa_id == e.id,
                                                     AvaliacaoTurno.dia.between(ini, fim),
                                                     AvaliacaoTurno.nota > 0).order_by(AvaliacaoTurno.dia.desc())):
        if a.usuario_id in pessoas:
            linha(a.usuario_id)["notas"].append(a.nota)
            if a.comentario:
                comentarios.append(a)
    lista = sorted(linhas.values(), key=lambda x: x["u"].nome.lower())
    for x in lista:
        x["dif"] = x["trab"] - x["escala"]
        n = x["pontuais"] + x["atrasos"]
        x["pont_pct"] = round(100 * x["pontuais"] / n) if n else None
        x["nota"] = round(sum(x["notas"]) / len(x["notas"]), 1) if x["notas"] else None
    cargos: dict[str, dict] = {}
    for x in lista:
        c = (x["u"].cargo or "").strip() or _("Sem cargo")
        g = cargos.setdefault(c, {"cargo": c, "pessoas": 0, "trab": 0, "escala": 0, "custo": 0})
        g["pessoas"] += 1
        g["trab"] += x["trab"]
        g["escala"] += x["escala"]
        g["custo"] += x["custo"]
    tot = {k: sum(x[k] for x in lista) for k in ("turnos", "escala", "trab", "pontuais", "atrasos", "faltas", "custo",
                                               "noturno", "noturno_valor")}
    n = tot["pontuais"] + tot["atrasos"]
    tot["pont_pct"] = round(100 * tot["pontuais"] / n) if n else None
    todas = [nn for x in lista for nn in x["notas"]]
    tot["nota"] = round(sum(todas) / len(todas), 1) if todas else None
    tot["n_notas"] = len(todas)
    tot["dif"] = tot["trab"] - tot["escala"]
    vendas = sum(custos.vendas(db, e.id, ini, fim_ef).values()) if fim_ef >= ini else 0
    tot["vendas"] = vendas
    tot["pct_vendas"] = round(100 * tot["custo"] / vendas, 1) if vendas else None
    return {"linhas": lista, "cargos": sorted(cargos.values(), key=lambda g: -g["custo"]),
            "tot": tot, "comentarios": comentarios[:30]}


def _periodo(e, ini: str, fim: str) -> tuple[date, date]:
    hoje = regras.hoje(e)
    f = data_iso(fim, hoje)
    i = data_iso(ini, f - timedelta(days=13))
    if i > f:
        i, f = f, i
    if (f - i).days >= MAX_DIAS:
        i = f - timedelta(days=MAX_DIAS - 1)
    return i, f


@rota.get("/painel/relatorios")
def tela(request: Request, ini: str = "", fim: str = "", formato: str = "", setor: str = "",
         u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    i, f = _periodo(e, ini, fim)
    sid = setores.ler_filtro(db, e.id, setor)
    r = montar(db, e, i, f, sid)
    ver_custo = pode(u, "pagamento")
    if formato == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")   # Excel em português: ponto e vírgula e vírgula decimal

        def n(v):
            return str(v).replace(".", ",")
        cab = [_("Funcionário"), _("Cargo"), _("Turnos"), _("Horas na escala"), _("Horas trabalhadas"),
               _("Diferença"), _("Pontualidade (%)"), _("Atrasos"), _("Faltas"), _("Nota média")]
        if ver_custo:
            cab.append(_("Custo (R$)"))
        cab.append(_("Horas noturnas"))
        if ver_custo:
            cab.append(_("Adicional noturno (R$)"))
        w.writerow(cab)
        for x in r["linhas"]:
            row = [x["u"].nome, x["u"].cargo or "", x["turnos"], n(round(x["escala"] / 60, 2)),
                   n(round(x["trab"] / 60, 2)), n(round(x["dif"] / 60, 2)),
                   "" if x["pont_pct"] is None else x["pont_pct"], x["atrasos"], x["faltas"],
                   "" if x["nota"] is None else n(x["nota"])]
            if ver_custo:
                row.append(n(f"{x['custo'] / 100:.2f}"))
            row.append(n(round(x["noturno"] / 60, 2)))
            if ver_custo:
                row.append(n(f"{x['noturno_valor'] / 100:.2f}"))
            w.writerow(row)
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="report-{i.isoformat()}-{f.isoformat()}.csv"'})
    return render(request, "painel_relatorios.html", u=u, e=e, aba="relatorios", ini=i, fim=f, r=r,
                  setores=setores.setores_da(db, e.id), setor=sid,
                  ver_custo=ver_custo, hoje=regras.hoje(e))
