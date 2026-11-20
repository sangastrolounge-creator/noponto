"""Folha de pagamento: dados salariais, geração a partir do período fechado, holerites,
revisão pelo Claude e holerite no app do funcionário."""
import csv
import io
import json
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import calculo_folha as cf
from . import i18n, regras, revisor, salario_minimo
from . import db as banco
from .core import (Redirecionar, admin_logado, avisar, da_empresa, dinheiro, exigir_csrf,
                   gestor_logado, ir, ler_centavos, pagamento_liberado, render, usuario_logado)
from .db import (Fechamento, FechamentoLinha, FolhaItem, FolhaPagamento, Historico, Usuario,
                 get_db)

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MSG_FINALIZADA = "Esta folha já foi finalizada. Desfaça a finalização para mudar alguma coisa."


def gestor_pagamento(request: Request, u: Usuario = Depends(gestor_logado)) -> Usuario:
    """O plano da empresa precisa incluir a folha de pagamento (a tela principal mostra o convite)."""
    if not pagamento_liberado(u.empresa) and request.url.path != "/painel/pagamento":
        avisar(request, "O seu plano não inclui a folha de pagamento. Mude de plano em Assinatura.", "erro")
        raise Redirecionar("/painel/pagamento")
    return u


def _equipe(db: Session, e) -> list[Usuario]:
    """Quem entra na folha: ativos na escala, menos prestador PJ (não é empregado)."""
    return [p for p in db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True))
        .order_by(Usuario.nome)) if (p.classificacao or "clt") != "pj"]


def _horas(linha: FechamentoLinha | None) -> dict:
    if not linha:
        return {}
    return {k: getattr(linha, k) for k in (
        "extra_normal", "extra_especial", "noturno", "debito", "faltas", "falta_min",
        "dsr_perdidos", "dsr_perdido_min", "dsr_extras", "intervalo_suprimido", "sobreaviso")}


def recalcular_item(it: FolhaItem, f: FolhaPagamento) -> None:
    fe = f.fechamento
    linha = next((l for l in fe.linhas if l.id == it.linha_id), None)
    t = json.loads(f.tabelas)
    calc = cf.calcular(it.salario_centavos, it.jornada_mensal, it.dependentes, it.vt_centavos,
                       json.loads(it.lancamentos or "[]"), _horas(linha), fe.pct_normal,
                       fe.pct_especial, fe.pct_noturno, f.controle, t)
    calc["alertas"] = cf.alertas(calc, it.salario_centavos, it.jornada_mensal,
                                 (f.fim - f.inicio).days + 1, t)
    it.calculo = json.dumps(calc, ensure_ascii=False)
    for k in ("bruto", "descontos", "liquido", "fgts"):
        setattr(it, k, calc[k])


def _copiar_cadastro(it: FolhaItem, u: Usuario) -> None:
    it.salario_centavos, it.jornada_mensal = u.salario_centavos, u.jornada_mensal or 220
    it.dependentes, it.vt_centavos = u.dependentes, u.vt_centavos


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
        if f"sal_{p.id}" not in form:
            continue
        sal = ler_centavos(str(form.get(f"sal_{p.id}", "")) or "0")
        vt = ler_centavos(str(form.get(f"vt_{p.id}", "")) or "0")
        try:
            jornada = int(form.get(f"jor_{p.id}") or 220)
            dep = int(form.get(f"dep_{p.id}") or 0)
        except ValueError:
            jornada, dep = -1, -1
        if sal is None or vt is None or not 1 <= jornada <= 300 or not 0 <= dep <= 20:
            avisar(request, f"Confira os dados de {p.nome}: salário e vale em reais, "
                            f"jornada de 1 a 300 horas e dependentes de 0 a 20.", "erro")
            return ir("/painel/pagamento")
        novo = (sal, jornada, dep, vt)
        if novo != (p.salario_centavos, p.jornada_mensal, p.dependentes, p.vt_centavos):
            p.salario_centavos, p.jornada_mensal, p.dependentes, p.vt_centavos = novo
            mudou += 1
    if mudou:
        regras.registrar(db, u.empresa_id, u.nome, "dados salariais",
                         f"Dados salariais de {mudou} funcionário(s) atualizados.")
    db.commit()
    baixos = salario_minimo.abaixo(db, u.empresa, regras.hoje(u.empresa))
    if baixos:
        avisar(request, salario_minimo.aviso(baixos), "erro")
    else:
        avisar(request, "Dados salariais salvos. Nas folhas em rascunho, use “Recalcular” para aplicar.")
    return ir("/painel/pagamento")


@rota.post("/painel/pagamento/gerar", dependencies=CSRF)
def pagamento_gerar(request: Request, fechamento_id: int = Form(...),
                    u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    e = u.empresa
    fe = da_empresa(db, Fechamento, fechamento_id, u)
    existe = db.scalar(select(FolhaPagamento).where(FolhaPagamento.fechamento_id == fe.id))
    if existe:
        return ir(f"/painel/pagamento/{existe.id}")
    t = cf.tabelas(db)
    f = FolhaPagamento(empresa_id=e.id, fechamento_id=fe.id, competencia=f"{fe.fim:%Y-%m}",
                       inicio=fe.inicio, fim=fe.fim, controle=e.controle,
                       tabelas=json.dumps(t), criado_por=u.nome, criado_em=banco.agora())
    f.fechamento = fe
    for linha in sorted(fe.linhas, key=lambda l: l.usuario.nome.lower()):
        if (linha.usuario.classificacao or "clt") == "pj":
            continue   # prestador PJ não entra na folha
        it = FolhaItem(empresa_id=e.id, usuario_id=linha.usuario_id, linha_id=linha.id)
        it.usuario = linha.usuario
        _copiar_cadastro(it, linha.usuario)
        f.itens.append(it)
        recalcular_item(it, f)
    db.add(f)
    regras.registrar(db, e.id, u.nome, "folha de pagamento gerada",
                     f"Folha de {fe.inicio:%d/%m/%Y} a {fe.fim:%d/%m/%Y}, {len(f.itens)} holerite(s).")
    db.commit()
    sem_salario = sum(1 for it in f.itens if not it.salario_centavos)
    if sem_salario:
        avisar(request, f"Folha gerada. {sem_salario} funcionário(s) estão sem salário cadastrado: "
                        f"preencha os dados salariais e clique em Recalcular.", "erro")
    else:
        avisar(request, "Folha gerada. Confira os holerites e, se quiser, peça a revisão do Claude.")
    return ir(f"/painel/pagamento/{f.id}")


@rota.get("/painel/pagamento/{fid}")
def pagamento_ver(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                  db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    itens = sorted(f.itens, key=lambda i: i.usuario.nome.lower())
    calcs = {it.id: json.loads(it.calculo or "{}") for it in itens}
    total = {k: sum(getattr(it, k) for it in itens) for k in ("bruto", "descontos", "liquido", "fgts")}
    total["inss"] = sum(l["valor"] for c in calcs.values() for l in c.get("linhas", []) if l["cod"] == "inss")
    total["irrf"] = sum(l["valor"] for c in calcs.values() for l in c.get("linhas", []) if l["cod"] == "irrf")
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
        avisar(request, "Holerites recalculados com os dados salariais atuais.")
    return ir(f"/painel/pagamento/{f.id}")


@rota.post("/painel/pagamento/{fid}/finalizar", dependencies=CSRF)
def pagamento_finalizar(request: Request, fid: int, acao: str = Form("finalizar"),
                        u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    periodo = f"{f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y}"
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
                     f"Folha de {f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y}.")
    db.delete(f)
    db.commit()
    avisar(request, "Folha excluída. Você pode gerar de novo a partir do período fechado.")
    return ir("/painel/pagamento")


# ============================================================ gorjetas
def dividir(total: int, pesos: dict) -> dict:
    """Divide o total (centavos) proporcionalmente aos pesos. Os centavos que sobram do
    arredondamento vão para quem teve a maior fração."""
    from decimal import Decimal
    pesos = {k: v for k, v in pesos.items() if v > 0}
    soma = sum(pesos.values())
    if total <= 0 or not soma:
        return {}
    exato = {k: Decimal(total) * v / soma for k, v in pesos.items()}
    parte = {k: int(x) for k, x in exato.items()}
    sobra = total - sum(parte.values())
    for k in sorted(exato, key=lambda k: (exato[k] - parte[k], -k), reverse=True)[:sobra]:
        parte[k] += 1
    return parte


@rota.post("/painel/pagamento/{fid}/gorjetas", dependencies=CSRF)
async def gorjetas_dividir(request: Request, fid: int, u: Usuario = Depends(gestor_pagamento),
                           db: Session = Depends(get_db)):
    """Rateio de gorjetas (Lei 13.419/2017): divide o total entre quem o gestor marcar, pelas
    horas trabalhadas ou em partes iguais. Entra no holerite como provento tributável (a gorjeta
    integra a remuneração: INSS, FGTS e IRRF incidem). A retenção para encargos prevista na
    convenção coletiva, se houver, deve ser descontada do total antes de dividir."""
    form = await request.form()
    f = da_empresa(db, FolhaPagamento, fid, u)
    volta = f"/painel/pagamento/{f.id}#gorjetas"
    if not _rascunho(request, f):
        return ir(volta)
    total = ler_centavos(str(form.get("total", "")))
    modo = str(form.get("modo", "horas"))
    descricao = (str(form.get("descricao", "")).strip() or "Gorjetas (rateio)")[:80]
    itens = [it for it in f.itens if form.get(f"p_{it.id}")]
    if not total or modo not in ("horas", "igual"):
        avisar(request, "Informe o total das gorjetas (ex.: 1.250,00).", "erro")
        return ir(volta)
    if not itens:
        avisar(request, "Marque quem participa do rateio.", "erro")
        return ir(volta)
    linhas = {l.id: l for l in f.fechamento.linhas}
    pesos = {it.id: ((linhas[it.linha_id].trabalhado if it.linha_id in linhas else 0)
                     if modo == "horas" else 1) for it in itens}
    partes = dividir(total, pesos)
    if not partes:
        avisar(request, "Ninguém marcado tem horas trabalhadas neste período.", "erro")
        return ir(volta)
    for it in itens:
        if partes.get(it.id):
            lanc = json.loads(it.lancamentos or "[]")
            lanc.append(dict(descricao=descricao, tipo="p", centavos=partes[it.id], tributavel=True))
            it.lancamentos = json.dumps(lanc, ensure_ascii=False)
            recalcular_item(it, f)
    _revisao_invalida(f)
    regras.registrar(db, u.empresa_id, u.nome, "rateio de gorjetas",
                     f"{dinheiro(total)} divididos ({'pelas horas' if modo == 'horas' else 'em partes iguais'}): "
                     + ", ".join(f"{it.usuario.nome} {dinheiro(partes[it.id])}" for it in itens if partes.get(it.id)))
    db.commit()
    avisar(request, f"{dinheiro(total)} em gorjetas divididos entre {len(partes)} pessoa(s).")
    return ir(volta)


# ============================================================ lançamentos manuais
@rota.post("/painel/pagamento/{fid}/item/{iid}/lancamento", dependencies=CSRF)
def lancamento_novo(request: Request, fid: int, iid: int, descricao: str = Form(""),
                    tipo: str = Form("p"), valor: str = Form(""), tributavel: str = Form(""),
                    u: Usuario = Depends(gestor_pagamento), db: Session = Depends(get_db)):
    f = da_empresa(db, FolhaPagamento, fid, u)
    it = da_empresa(db, FolhaItem, iid, u)
    volta = f"/painel/pagamento/{f.id}/holerite/{it.id}"
    if it.folha_id != f.id or not _rascunho(request, f):
        return ir(volta)
    centavos, descricao = ler_centavos(valor), descricao.strip()[:80]
    if not centavos or len(descricao) < 2 or tipo not in ("p", "d"):
        avisar(request, "Informe a descrição e o valor (ex.: 150,00).", "erro")
        return ir(volta)
    lanc = json.loads(it.lancamentos or "[]")
    if len(lanc) >= 20:
        avisar(request, "Limite de 20 lançamentos por holerite.", "erro")
        return ir(volta)
    lanc.append(dict(descricao=descricao, tipo=tipo, centavos=centavos,
                     tributavel=tipo == "p" and bool(tributavel)))
    it.lancamentos = json.dumps(lanc, ensure_ascii=False)
    recalcular_item(it, f)
    _revisao_invalida(f)
    regras.registrar(db, u.empresa_id, u.nome, "lançamento na folha",
                     f"{'Provento' if tipo == 'p' else 'Desconto'} “{descricao}” de "
                     f"{dinheiro(centavos)} para {it.usuario.nome}.")
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
                         f"Removido “{tirado['descricao']}” de {it.usuario.nome}.")
        db.commit()
    return ir(volta)


# ============================================================ holerites e planilha
def _holerite_ctx(it: FolhaItem) -> dict:
    calc = json.loads(it.calculo or "{}")
    return dict(it=it, calc=calc, lanc=json.loads(it.lancamentos or "[]"),
                proventos=[l for l in calc.get("linhas", []) if l["tipo"] == "p"],
                descontos=[l for l in calc.get("linhas", []) if l["tipo"] == "d"])


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
    f = da_empresa(db, FolhaPagamento, fid, u)
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Funcionário", "Função", "Competência", "Salário base", "Proventos (bruto)",
                "INSS", "IRRF", "Outros descontos", "Total de descontos", "Líquido",
                "Base INSS", "Base IRRF", "FGTS (empresa)", "Itens"])
    r = lambda c: f"{c / 100:.2f}".replace(".", ",")  # noqa: E731
    for it in sorted(f.itens, key=lambda i: i.usuario.nome.lower()):
        c = json.loads(it.calculo or "{}")
        val = {l["cod"]: l["valor"] for l in c.get("linhas", [])}
        inss_v, ir_v = val.get("inss", 0), val.get("irrf", 0)
        itens = " | ".join(f"{l['desc']}: {'+' if l['tipo'] == 'p' else '-'}{r(l['valor'])}"
                           for l in c.get("linhas", []))
        w.writerow([it.usuario.nome, it.usuario.cargo, f.competencia, r(it.salario_centavos),
                    r(it.bruto), r(inss_v), r(ir_v), r(it.descontos - inss_v - ir_v),
                    r(it.descontos), r(it.liquido), r(c.get("base_inss", 0)),
                    r(c.get("base_irrf", 0)), r(it.fgts), itens])
    return Response("﻿" + saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="folha-pagamento-{f.competencia}.csv"'})


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
                     f"Revisão pedida para a folha de {f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y}.")
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


# ============================================================ master: tabelas e chave do Claude
TABELAS_CAMPOS = [  # (chave, rótulo, tipo) — tipo: c = reais, p = percentual, n = número
    ("salario_minimo", "Salário mínimo", "c"),
    ("irrf_dependente", "IRRF: dedução por dependente", "c"),
    ("irrf_simplificado", "IRRF: desconto simplificado", "c"),
    ("irrf_minimo", "IRRF: retenção mínima", "c"),
    ("isento_ate", "Redutor: isento até", "c"),
    ("redutor_max", "Redutor: valor máximo", "c"),
    ("redutor_ate", "Redutor: vale até", "c"),
    ("redutor_a", "Redutor: parcela fixa (a)", "c"),
    ("redutor_b", "Redutor: fator (b)", "n"),
    ("fgts_pct", "FGTS (%)", "p"),
    ("vt_pct", "Vale-transporte: desconto máximo (%)", "p"),
]


def _num(txt: str) -> float | None:
    try:
        return float(str(txt).replace(",", "."))
    except ValueError:
        return None


@rota.get("/master/folha")
def master_folha(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/folha.html", u=u, e=u.empresa, aba="master", sub="folha",
                  t=cf.tabelas(db),
                  campos=TABELAS_CAMPOS, claude=revisor.configurado(db),
                  chave_env=bool(os.environ.get("ANTHROPIC_API_KEY")),
                  modelo=revisor.MODELO)


@rota.post("/master/folha", dependencies=CSRF)
async def master_folha_salvar(request: Request, u: Usuario = Depends(admin_logado),
                              db: Session = Depends(get_db)):
    form = await request.form()
    t = cf.tabelas(db)
    novo = dict(t)
    try:
        for chave, rotulo, tipo in TABELAS_CAMPOS:
            v = str(form.get(chave, ""))
            if tipo == "c":
                c = ler_centavos(v)
                if c is None:
                    raise ValueError(rotulo)
                novo[chave] = c
            else:
                n = _num(v)
                if n is None or n < 0:
                    raise ValueError(rotulo)
                novo[chave] = int(n) if tipo == "p" and n == int(n) else n
        novo["vigencia"] = str(form.get("vigencia", "")).strip()[:20]
        inss = []
        for i in range(4):
            teto, pct = ler_centavos(str(form.get(f"inss_teto_{i}", ""))), _num(form.get(f"inss_pct_{i}", ""))
            if not teto or pct is None:
                raise ValueError(f"INSS faixa {i + 1}")
            inss.append([teto, pct])
        irrf = []
        for i in range(5):
            teto = ler_centavos(str(form.get(f"ir_teto_{i}", "") or "0"))
            pct, ded = _num(form.get(f"ir_pct_{i}", "")), ler_centavos(str(form.get(f"ir_ded_{i}", "") or "0"))
            if teto is None or pct is None or ded is None:
                raise ValueError(f"IRRF faixa {i + 1}")
            irrf.append([teto if i < 4 else 0, pct, ded])
        if [x[0] for x in inss] != sorted(x[0] for x in inss) or \
                [x[0] for x in irrf[:4]] != sorted(x[0] for x in irrf[:4]):
            raise ValueError("as faixas precisam estar em ordem crescente")
        novo["inss"], novo["irrf"] = inss, irrf
    except ValueError as ex:
        avisar(request, f"Confira o campo: {ex}.", "erro")
        return ir("/master/folha")
    cf.salvar_tabelas(db, novo)
    db.commit()
    avisar(request, "Tabelas salvas. Valem para as próximas folhas geradas (as já geradas guardam as tabelas da época).")
    return ir("/master/folha")


@rota.post("/master/folha/chave", dependencies=CSRF)
def master_folha_chave(request: Request, chave: str = Form(""), apagar: str = Form(""),
                       u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    from .db import Config
    c = db.get(Config, revisor.CHAVE_CONFIG)
    chave = chave.strip()
    if apagar:
        if c:
            db.delete(c)
        avisar(request, "Chave do Claude removida.")
    elif not chave.startswith("sk-ant-") or len(chave) < 30:
        avisar(request, "Essa não parece uma chave da API da Anthropic (começa com sk-ant-).", "erro")
        return ir("/master/folha")
    else:
        if not c:
            c = Config(chave=revisor.CHAVE_CONFIG, valor="")
            db.add(c)
        c.valor = chave
        avisar(request, "Chave do Claude salva. A revisão das folhas já pode ser usada.")
    db.commit()
    return ir("/master/folha")


def periodo_tem_folha_finalizada(db: Session, fechamento_id: int) -> bool:
    return bool(db.scalar(select(FolhaPagamento.id).where(
        FolhaPagamento.fechamento_id == fechamento_id, FolhaPagamento.situacao == "finalizada")))


def apagar_folhas_do_periodo(db: Session, fechamento_id: int) -> int:
    folhas = list(db.scalars(select(FolhaPagamento).where(
        FolhaPagamento.fechamento_id == fechamento_id)))
    for f in folhas:
        db.delete(f)
    return len(folhas)
