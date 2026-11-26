"""Pagamento: valor da hora de cada um, pagamento bruto a partir do período fechado, revisão
pelo Claude, planilha para o provedor de folha e resumo no app do funcionário.
Impostos ficam com o provedor de folha (Gusto, ADP, QuickBooks Payroll...)."""
import csv
import io
import json
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import calculo_folha as cf
from ... import i18n, regras
from . import apuracao, revisor, salario_minimo
from ... import db as banco
from ...core import (Redirecionar, admin_logado, avisar, da_empresa, dinheiro, exigir_csrf,
                   gestor_logado, ir, ler_centavos, pagamento_liberado, render, usuario_logado, caminho_da_tela)
from ...db import (Fechamento, FechamentoLinha, FolhaItem, FolhaPagamento, Historico, Usuario,
                 get_db)

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MSG_FINALIZADA = "Esta folha já foi finalizada. Desfaça a finalização para mudar alguma coisa."


def gestor_pagamento(request: Request, u: Usuario = Depends(gestor_logado)) -> Usuario:
    """O plano da empresa precisa incluir Pagamento (a tela principal mostra o convite)."""
    if not pagamento_liberado(u.empresa) and caminho_da_tela(request) != "/painel/pagamento":
        avisar(request, "O seu plano não inclui Pagamento. Mude de plano em Assinatura.", "erro")
        raise Redirecionar("/painel/pagamento")
    return u


def _equipe(db: Session, e) -> list[Usuario]:
    return list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True))
        .order_by(Usuario.nome)))


def _horas(linha: FechamentoLinha | None) -> dict:
    if not linha:
        return {}
    return {k: getattr(linha, k) for k in apuracao.CHAVES}


def _horas_para_calculo(it: FolhaItem, linha) -> dict:
    """Prestador 1099 não é empregado: as regras de hora extra e de refeição não se aplicam,
    todas as horas valem o valor combinado."""
    h = _horas(linha)
    if h and (it.usuario.classificacao or "w2").startswith("1099"):
        h = {**h, "regular": h.get("regular", 0) + h.get("ot", 0) + h.get("dt", 0), "ot": 0, "dt": 0,
             "meal_premios": 0, "descanso_premios": 0, "previsibilidade_min": 0, "previsibilidade_centavos": 0}
    return h


def recalcular_item(it: FolhaItem, f: FolhaPagamento) -> None:
    fe = f.fechamento
    linha = next((l for l in fe.linhas if l.id == it.linha_id), None)
    calc = cf.calcular(it.taxa_hora_centavos, it.isento, it.salario_periodo_centavos,
                       json.loads(it.lancamentos or "[]"), _horas_para_calculo(it, linha), fe.pct_noturno,
                       fe.pct_feriado)
    calc["alertas"] = cf.alertas(calc, it.taxa_hora_centavos, it.isento,
                                 it.salario_periodo_centavos, (f.fim - f.inicio).days + 1,
                                 it.usuario.gorjeta)
    it.calculo = json.dumps(calc, ensure_ascii=False)
    it.bruto = calc["bruto"]


def _copiar_cadastro(it: FolhaItem, u: Usuario) -> None:
    it.taxa_hora_centavos, it.isento = u.taxa_hora_centavos, u.isento
    it.salario_periodo_centavos = u.salario_periodo_centavos


def _rascunho(request: Request, f: FolhaPagamento) -> bool:
    if f.situacao != "rascunho":
        avisar(request, MSG_FINALIZADA, "erro")
        return False
    return True


def _revisao_invalida(f: FolhaPagamento) -> None:
    """Mudou algum valor: a revisão do Claude deixa de valer."""
    if f.revisao_situacao in ("pronta", "erro"):
        f.revisao_situacao, f.revisao = "", ""


# ============================================================ tela principal
@rota.get("/painel/pagamento")
def pagamento(request: Request, u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    e = u.empresa
    folhas = list(db.scalars(select(FolhaPagamento).where(FolhaPagamento.empresa_id == e.id)
                             .order_by(FolhaPagamento.fim.desc()).limit(24)))
    com_folha = {f.fechamento_id for f in folhas}
    fechamentos = [f for f in db.scalars(select(Fechamento).where(Fechamento.empresa_id == e.id)
                                         .order_by(Fechamento.fim.desc()).limit(12))
                   if f.id not in com_folha]
    return render(request, "painel_pagamento.html", u=u, e=e, aba="pagamento", folhas=folhas,
                  fechamentos=fechamentos, equipe=_equipe(db, e),
                  claude=revisor.configurado(db), liberado=pagamento_liberado(e),
                  sm=salario_minimo.situacao(db, e, regras.hoje(e)))


@rota.post("/painel/pagamento/dados", dependencies=CSRF)
async def pagamento_dados(request: Request, u: Usuario = Depends(gestor_pagamento),
                          db: Session = Depends(get_db)):
    form = await request.form()
    mudou = 0
    for p in _equipe(db, u.empresa):
        if f"taxa_{p.id}" not in form:
            continue
        taxa = ler_centavos(str(form.get(f"taxa_{p.id}", "")) or "0")
        sal = ler_centavos(str(form.get(f"sal_{p.id}", "")) or "0")
        isento, gorjeta = bool(form.get(f"isento_{p.id}")), bool(form.get(f"gorjeta_{p.id}"))
        if taxa is None or sal is None or taxa > 100000:
            avisar(request, f"Confira os dados de {p.nome}: valores em dólares, como 15.50.", "erro")
            return ir("/painel/pagamento")
        novo = (taxa, isento, sal, gorjeta)
        if novo != (p.taxa_hora_centavos, p.isento, p.salario_periodo_centavos, p.gorjeta):
            p.taxa_hora_centavos, p.isento, p.salario_periodo_centavos, p.gorjeta = novo
            mudou += 1
    if mudou:
        regras.registrar(db, u.empresa_id, u.nome, "dados de pagamento",
                         f"Dados de pagamento de {mudou} funcionário(s) atualizados.")
    db.commit()
    baixos = salario_minimo.abaixo(db, u.empresa, regras.hoje(u.empresa))
    if baixos:
        avisar(request, salario_minimo.aviso(baixos), "erro")
    else:
        avisar(request, "Dados de pagamento salvos. Nos pagamentos em rascunho, use “Recalcular” para aplicar.")
    return ir("/painel/pagamento")


@rota.post("/painel/pagamento/gerar", dependencies=CSRF)
def pagamento_gerar(request: Request, fechamento_id: int = Form(...),
                    u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    e = u.empresa
    fe = da_empresa(db, Fechamento, fechamento_id, u)
    existe = db.scalar(select(FolhaPagamento).where(FolhaPagamento.fechamento_id == fe.id))
    if existe:
        return ir(f"/painel/pagamento/{existe.id}")
    f = FolhaPagamento(empresa_id=e.id, fechamento_id=fe.id, competencia=f"{fe.fim:%Y-%m}",
                       inicio=fe.inicio, fim=fe.fim, criado_por=u.nome, criado_em=banco.agora())
    f.fechamento = fe
    for linha in sorted(fe.linhas, key=lambda l: l.usuario.nome.lower()):
        it = FolhaItem(empresa_id=e.id, usuario_id=linha.usuario_id, linha_id=linha.id)
        it.usuario = linha.usuario
        _copiar_cadastro(it, linha.usuario)
        f.itens.append(it)
        recalcular_item(it, f)
    db.add(f)
    regras.registrar(db, e.id, u.nome, "folha de pagamento gerada",
                     f"Pagamento de {fe.inicio:%m/%d/%Y} a {fe.fim:%m/%d/%Y}, {len(f.itens)} funcionário(s).")
    db.commit()
    sem_taxa = sum(1 for it in f.itens if not it.taxa_hora_centavos and not it.isento)
    if sem_taxa:
        avisar(request, f"Pagamento gerado. {sem_taxa} funcionário(s) estão sem valor da hora: "
                        f"preencha os dados de pagamento e clique em Recalcular.", "erro")
    else:
        avisar(request, "Pagamento gerado. Confira cada resumo e, se quiser, peça a revisão do Claude.")
    return ir(f"/painel/pagamento/{f.id}")


@rota.get("/painel/pagamento/{fid}")
def pagamento_ver(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                  db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    itens = sorted(f.itens, key=lambda i: i.usuario.nome.lower())
    calcs = {it.id: json.loads(it.calculo or "{}") for it in itens}
    total = {"bruto": sum(it.bruto for it in itens),
             "gorjetas": sum(c.get("gorjetas", 0) for c in calcs.values()),
             "horas": sum(c.get("horas_pagas", 0) for c in calcs.values())}
    for cod in ("ot", "dt"):
        total[cod] = sum(l["valor"] for c in calcs.values() for l in c.get("linhas", []) if l["cod"] == cod)
    revisao = json.loads(f.revisao) if f.revisao else None
    if revisao and revisao.get("alertas"):
        ordem = {"alta": 0, "media": 1, "baixa": 2}
        revisao["alertas"].sort(key=lambda a: ordem.get(a.get("gravidade"), 3))
    return render(request, "painel_pagamento_folha.html", u=u, e=u.empresa, aba="pagamento",
                  f=f, itens=itens, calcs=calcs, total=total, revisao=revisao,
                  claude=revisor.configurado(db), modelo=revisor.MODELO)


@rota.post("/painel/pagamento/{fid}/recalcular", dependencies=CSRF)
def pagamento_recalcular(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                         db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    if _rascunho(request, f):
        for it in f.itens:
            _copiar_cadastro(it, it.usuario)
            recalcular_item(it, f)
        _revisao_invalida(f)
        db.commit()
        avisar(request, "Pagamentos recalculados com os dados atuais.")
    return ir(f"/painel/pagamento/{f.id}")


@rota.post("/painel/pagamento/{fid}/finalizar", dependencies=CSRF)
def pagamento_finalizar(request: Request, fid: int, acao: str = Form("finalizar"),
                        u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    periodo = f"{f.inicio:%m/%d/%Y} a {f.fim:%m/%d/%Y}"
    if acao == "desfazer" and f.situacao == "finalizada":
        f.situacao, f.finalizada_em = "rascunho", None
        regras.registrar(db, u.empresa_id, u.nome, "folha de pagamento reaberta", f"Folha de {periodo}.")
        avisar(request, "Finalização desfeita. Os holerites saíram do app dos funcionários até você finalizar de novo.")
    elif acao == "finalizar" and f.situacao == "rascunho":
        f.situacao, f.finalizada_em = "finalizada", banco.agora()
        regras.registrar(db, u.empresa_id, u.nome, "folha de pagamento finalizada", f"Folha de {periodo}.")
        avisar(request, "Folha finalizada. Cada funcionário já vê o próprio holerite no app.")
    db.commit()
    return ir(f"/painel/pagamento/{f.id}")


@rota.post("/painel/pagamento/{fid}/excluir", dependencies=CSRF)
def pagamento_excluir(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                      db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    if not _rascunho(request, f):
        return ir(f"/painel/pagamento/{f.id}")
    regras.registrar(db, u.empresa_id, u.nome, "folha de pagamento excluída",
                     f"Folha de {f.inicio:%m/%d/%Y} a {f.fim:%m/%d/%Y}.")
    db.delete(f)
    db.commit()
    avisar(request, "Folha excluída. Você pode gerar de novo a partir do período fechado.")
    return ir("/painel/pagamento")


# ============================================================ lançamentos manuais
@rota.post("/painel/pagamento/{fid}/item/{iid}/lancamento", dependencies=CSRF)
def lancamento_novo(request: Request, fid: int, iid: int, descricao: str = Form(""),
                    tipo: str = Form("bonus"), valor: str = Form(""),
                    u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    it = da_empresa(db, FolhaItem, iid, u)
    volta = f"/painel/pagamento/{f.id}/holerite/{it.id}"
    if it.folha_id != f.id or not _rascunho(request, f):
        return ir(volta)
    centavos, descricao = ler_centavos(valor), descricao.strip()[:80]
    if not centavos or tipo not in cf.TIPOS_LANCAMENTO:
        avisar(request, "Informe o tipo e o valor (ex.: 150.00).", "erro")
        return ir(volta)
    lanc = json.loads(it.lancamentos or "[]")
    if len(lanc) >= 20:
        avisar(request, "Limite de 20 lançamentos por holerite.", "erro")
        return ir(volta)
    lanc.append(dict(descricao=descricao, tipo=tipo, centavos=centavos))
    it.lancamentos = json.dumps(lanc, ensure_ascii=False)
    recalcular_item(it, f)
    _revisao_invalida(f)
    regras.registrar(db, u.empresa_id, u.nome, "lançamento na folha",
                     f"{cf.TIPOS_LANCAMENTO[tipo][0]} de {dinheiro(centavos)} para {it.usuario.nome}.")
    db.commit()
    return ir(volta)


@rota.post("/painel/pagamento/{fid}/item/{iid}/lancamento/{n}/remover", dependencies=CSRF)
def lancamento_remover(request: Request, fid: int, iid: int, n: int,
                       u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    it = da_empresa(db, FolhaItem, iid, u)
    volta = f"/painel/pagamento/{f.id}/holerite/{it.id}"
    if it.folha_id != f.id or not _rascunho(request, f):
        return ir(volta)
    lanc = json.loads(it.lancamentos or "[]")
    if 0 <= n < len(lanc):
        tirado = lanc.pop(n)
        it.lancamentos = json.dumps(lanc, ensure_ascii=False)
        recalcular_item(it, f)
        _revisao_invalida(f)
        regras.registrar(db, u.empresa_id, u.nome, "lançamento na folha",
                         f"Removido {dinheiro(tirado['centavos'])} de {it.usuario.nome}.")
        db.commit()
    return ir(volta)


@rota.post("/painel/pagamento/{fid}/gorjetas", dependencies=CSRF)
async def gorjetas_dividir(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                           db: Session = Depends(get_db)):
    """Tip pool: divide um total de gorjetas entre quem a gestora marcou, por horas ou igual."""
    form = await request.form()
    f = da_empresa(db, FolhaPagamento, fid, u)
    volta = f"/painel/pagamento/{f.id}#gorjetas"
    if not _rascunho(request, f):
        return ir(volta)
    total = ler_centavos(str(form.get("total", "")))
    tipo, modo = str(form.get("tipo", "gorjeta_cartao")), str(form.get("modo", "horas"))
    descricao = (str(form.get("descricao", "")).strip() or "Tip pool")[:80]
    itens = [it for it in f.itens if form.get(f"p_{it.id}")]
    if not total or tipo not in ("gorjeta_cartao", "gorjeta_dinheiro") or modo not in ("horas", "igual"):
        avisar(request, "Informe o total das gorjetas (ex.: 1250.00).", "erro")
        return ir(volta)
    if not itens:
        avisar(request, "Marque quem participa da divisão.", "erro")
        return ir(volta)
    linhas = {l.id: l for l in f.fechamento.linhas}

    def trabalhado(it):
        l = linhas.get(it.linha_id)
        return (l.regular + l.ot + l.dt) if l else 0
    pesos = {it.id: (trabalhado(it) if modo == "horas" else 1) for it in itens}
    partes = cf.dividir_gorjetas(total, pesos)
    if not partes:
        avisar(request, "Ninguém marcado tem horas trabalhadas neste período.", "erro")
        return ir(volta)
    for it in itens:
        if partes.get(it.id):
            lanc = json.loads(it.lancamentos or "[]")
            lanc.append(dict(descricao=descricao, tipo=tipo, centavos=partes[it.id]))
            it.lancamentos = json.dumps(lanc, ensure_ascii=False)
            recalcular_item(it, f)
    _revisao_invalida(f)
    regras.registrar(db, u.empresa_id, u.nome, "divisão de gorjetas",
                     f"{dinheiro(total)} divididos ({modo}): "
                     + ", ".join(f"{it.usuario.nome} {dinheiro(partes[it.id])}" for it in itens if partes.get(it.id)))
    db.commit()
    avisar(request, f"{dinheiro(total)} em gorjetas divididos entre {len(partes)} pessoa(s).")
    return ir(volta)


# ============================================================ holerites e planilha
def _holerite_ctx(it: FolhaItem) -> dict:
    calc = json.loads(it.calculo or "{}")
    return dict(it=it, calc=calc, lanc=json.loads(it.lancamentos or "[]"),
                proventos=calc.get("linhas", []))


@rota.get("/painel/pagamento/{fid}/holerite/{iid}")
def holerite(request: Request, fid: int, iid: int, u: Usuario = Depends(gestor_pagamento),
             db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    it = da_empresa(db, FolhaItem, iid, u)
    if it.folha_id != f.id:
        return ir(f"/painel/pagamento/{f.id}")
    return render(request, "holerites.html", u=u, e=u.empresa, f=f, lista=[_holerite_ctx(it)],
                  editar=f.situacao == "rascunho", volta=f"/painel/pagamento/{f.id}",
                  emitido=regras.agora_local(u.empresa))


@rota.get("/painel/pagamento/{fid}/holerites")
def holerites_todos(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                    db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    lista = [_holerite_ctx(it) for it in sorted(f.itens, key=lambda i: i.usuario.nome.lower())]
    return render(request, "holerites.html", u=u, e=u.empresa, f=f, lista=lista, editar=False,
                  volta=f"/painel/pagamento/{f.id}", emitido=regras.agora_local(u.empresa))


@rota.get("/painel/pagamento/{fid}/planilha.csv")
def pagamento_csv(fid: int, u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    """Pagamento bruto para o provedor de folha: horas em decimal e valores em dólares."""
    f = da_empresa(db, FolhaPagamento, fid, u)
    saida = io.StringIO()
    w = csv.writer(saida)
    w.writerow(["Employee", "Job title", "Period start", "Period end", "Pay type", "Hourly rate",
                "Regular hours", "Overtime hours", "Double time hours", "Regular pay",
                "Overtime pay", "Double time pay", "Meal premium", "Paid leave",
                "Night differential", "Holiday premium", "Bonus/commission",
                "Regular rate adjustment", "Credit card tips", "Cash tips", "Other",
                "Gross pay", "Tax classification"])
    r = lambda c: f"{c / 100:.2f}"  # noqa: E731
    hd = lambda m: f"{m / 60:.2f}"  # noqa: E731
    for it in sorted(f.itens, key=lambda i: i.usuario.nome.lower()):
        c = json.loads(it.calculo or "{}")
        val = {l["cod"]: l["valor"] for l in c.get("linhas", [])}
        lanc = json.loads(it.lancamentos or "[]")

        def soma(*tipos):
            return sum(int(l["centavos"]) for l in lanc if l.get("tipo") in tipos)
        linha = next((l for l in f.fechamento.linhas if l.id == it.linha_id), None)
        h = _horas_para_calculo(it, linha) or {}
        w.writerow([it.usuario.nome, it.usuario.cargo, f"{f.inicio:%m/%d/%Y}", f"{f.fim:%m/%d/%Y}",
                    "salary" if it.isento else "hourly", r(it.taxa_hora_centavos),
                    hd(h.get("regular", 0)), hd(h.get("ot", 0)),
                    hd(h.get("dt", 0)), r(val.get("regular", val.get("salario", 0))),
                    r(val.get("ot", 0)), r(val.get("dt", 0)), r(val.get("meal", 0)),
                    r(val.get("pto", 0)), r(val.get("noturno", 0)), r(val.get("feriado", 0)),
                    r(soma("bonus", "comissao", "bonus_livre")), r(val.get("ajuste_taxa", 0)),
                    r(soma("gorjeta_cartao")), r(soma("gorjeta_dinheiro")), r(soma("reembolso")),
                    r(it.bruto), {"1099_ind": "1099 (individual)", "1099_emp": "1099 (business)"}.get(
                        it.usuario.classificacao, "W-2")])
    return Response(saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="gross-pay-{f.inicio}-to-{f.fim}.csv"'})


# ============================================================ revisão pelo Claude
@rota.post("/painel/pagamento/{fid}/revisar", dependencies=CSRF)
def pagamento_revisar(request: Request, tarefas_bg: BackgroundTasks, fid: int,
                      u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    volta = ir(f"/painel/pagamento/{f.id}")
    if not revisor.configurado(db):
        avisar(request, "A revisão pelo Claude ainda não foi ligada nesta plataforma.", "erro")
        return volta
    if f.revisao_situacao == "andamento" and f.revisao_em and \
            (banco.agora() - f.revisao_em).total_seconds() < 600:
        avisar(request, "A revisão já está em andamento. Aguarde alguns instantes.")
        return volta
    hoje = banco.agora().replace(hour=0, minute=0, second=0, microsecond=0)
    feitas = db.scalar(select(func.count()).select_from(Historico).where(
        Historico.empresa_id == u.empresa_id, Historico.acao == "folha revisada pelo Claude",
        Historico.criado_em >= hoje)) or 0
    if feitas >= revisor.LIMITE_DIA:
        avisar(request, f"Limite de {revisor.LIMITE_DIA} revisões por dia atingido. Tente amanhã.", "erro")
        return volta
    f.revisao_situacao, f.revisao, f.revisao_em = "andamento", "", banco.agora()
    regras.registrar(db, u.empresa_id, u.nome, "folha revisada pelo Claude",
                     f"Revisão pedida para a folha de {f.inicio:%m/%d/%Y} a {f.fim:%m/%d/%Y}.")
    db.commit()
    tarefas_bg.add_task(revisor.revisar, f.id, i18n.idioma())
    avisar(request, "Revisão pedida ao Claude. Leva de alguns segundos a poucos minutos; a página se atualiza sozinha.")
    return volta


# ============================================================ funcionário: holerite no app
@rota.get("/app/holerite/{iid}")
def app_holerite(request: Request, iid: int, u: Usuario = Depends(usuario_logado),
                 db: Session = Depends(get_db)):
    it = db.get(FolhaItem, iid)
    if not it or it.usuario_id != u.id or it.folha.situacao != "finalizada":
        return ir("/app/folha")
    return render(request, "holerites.html", u=u, e=u.empresa, f=it.folha,
                  lista=[_holerite_ctx(it)], editar=False, volta="/app/folha",
                  emitido=regras.agora_local(u.empresa))


def holerites_do_funcionario(db: Session, u: Usuario) -> list[FolhaItem]:
    return list(db.scalars(select(FolhaItem).join(FolhaPagamento).where(
        FolhaItem.usuario_id == u.id, FolhaPagamento.situacao == "finalizada")
        .order_by(FolhaPagamento.fim.desc()).limit(24)))


def periodo_tem_folha_finalizada(db: Session, fechamento_id: int) -> bool:
    return bool(db.scalar(select(FolhaPagamento.id).where(
        FolhaPagamento.fechamento_id == fechamento_id, FolhaPagamento.situacao == "finalizada")))


def apagar_folhas_do_periodo(db: Session, fechamento_id: int) -> int:
    folhas = list(db.scalars(select(FolhaPagamento).where(
        FolhaPagamento.fechamento_id == fechamento_id)))
    for f in folhas:
        db.delete(f)
    return len(folhas)


from ...core import templates  # noqa: E402
templates.env.globals["tipos_lanc_us"] = cf.TIPOS_LANCAMENTO
