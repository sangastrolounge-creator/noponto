"""Controle de férias pela CLT (no lugar dos saldos de PTO e sick leave da versão americana).

As férias gozadas são as ausências do tipo "ferias" aprovadas (com os dias vendidos em
`Ausencia.abono_dias`). O saldo de cada período aquisitivo sai de:
- a data de admissão da ficha (sem ela, a pessoa não aparece no controle);
- as faltas dos períodos de ponto fechados que caem dentro do período aquisitivo (um fechamento
  que pega dois períodos tem as faltas divididas pela quantidade de dias de cada um);
- os afastamentos pelo INSS (art. 133, IV);
- os ajustes feitos à mão pelo gestor (FeriasAjuste).
As regras ficam em clt.py."""
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import apuracao, clt, push, regras
from . import db as banco
from .apuracao import MSG_FECHADO
from .core import (avisar, da_empresa, data_iso, exigir_csrf, gestor_logado, ir, render,
                   usuario_logado)
from .db import (Alerta, Ausencia, Fechamento, FechamentoLinha, FeriasAjuste, Feriado, Turno,
                 Usuario, get_db)

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
SITUACOES = {"aquisicao": "Em aquisição", "a_gozar": "A gozar", "vencendo": "Vence em breve",
             "vencido": "Vencido: pagar em dobro", "gozado": "Gozado", "sem_direito": "Sem direito"}


def participa(u: Usuario) -> bool:
    return bool(u.ativo and u.na_escala and (u.classificacao or "clt") not in clt.SEM_FERIAS)


def _sobreposicao(a_ini: date, a_fim: date, b_ini: date, b_fim: date) -> int:
    return max(0, (min(a_fim, b_fim) - max(a_ini, b_ini)).days + 1)


def _dados(db: Session, u: Usuario, hoje: date) -> dict:
    periodos = clt.periodos_aquisitivos(u.data_admissao, hoje)
    faltas = {ini: 0.0 for ini, _ in periodos}
    for f_ini, f_fim, n in db.execute(
            select(Fechamento.inicio, Fechamento.fim, FechamentoLinha.faltas)
            .join(Fechamento, FechamentoLinha.fechamento_id == Fechamento.id)
            .where(FechamentoLinha.usuario_id == u.id, FechamentoLinha.faltas > 0)):
        total = (f_fim - f_ini).days + 1
        for ini, fim in periodos:
            faltas[ini] += n * _sobreposicao(f_ini, f_fim, ini, fim) / total
    afast = {ini: 0 for ini, _ in periodos}
    gozos = []
    for a in db.scalars(select(Ausencia).where(
            Ausencia.usuario_id == u.id, Ausencia.situacao == "aprovado",
            Ausencia.tipo.in_(("ferias", "afastamento")))):
        if a.tipo == "ferias":
            gozos.append({"inicio": a.inicio, "fim": a.fim, "abono": a.abono_dias or 0, "id": a.id})
        else:
            for ini, fim in periodos:
                afast[ini] += _sobreposicao(a.inicio, a.fim, ini, fim)
    ajustes = {}
    for ini, dias in db.execute(select(FeriasAjuste.periodo_inicio, func.sum(FeriasAjuste.dias))
                                .where(FeriasAjuste.usuario_id == u.id)
                                .group_by(FeriasAjuste.periodo_inicio)):
        ajustes[ini] = int(dias or 0)
    return dict(faltas={k: int(v + 0.5) for k, v in faltas.items()}, gozos=gozos,
                ajustes=ajustes, afastamentos=afast)


def situacao(db: Session, u: Usuario, hoje: date | None = None) -> dict | None:
    """Períodos e resumo das férias da pessoa, ou None se ela não entra no controle."""
    if not participa(u) or not u.data_admissao:
        return None
    hoje = hoje or regras.hoje(u.empresa)
    if u.data_admissao > hoje:
        return None
    d = _dados(db, u, hoje)
    periodos = clt.saldo_ferias(u.data_admissao, hoje, d["faltas"], d["gozos"], d["ajustes"],
                                d["afastamentos"], u.classificacao or "clt")
    ajustes = list(db.scalars(select(FeriasAjuste).where(FeriasAjuste.usuario_id == u.id)
                              .order_by(FeriasAjuste.criado_em.desc())))
    return dict(u=u, periodos=periodos, ajustes=ajustes, **clt.resumo_ferias(periodos))


def da_equipe(db: Session, e) -> tuple[list[dict], list[Usuario]]:
    """(situação de cada pessoa com admissão, pessoas sem data de admissão)."""
    hoje = regras.hoje(e)
    linhas, sem_data = [], []
    for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True))
                        .order_by(Usuario.nome)):
        if not participa(u):
            continue
        if not u.data_admissao:
            sem_data.append(u)
            continue
        s = situacao(db, u, hoje)
        if s:
            linhas.append(s)
    return linhas, sem_data


def periodo_para(s: dict | None, inicio: date) -> dict | None:
    """Período aquisitivo de onde saem férias que começam em `inicio`: o mais antigo já completo
    nessa data que ainda tem saldo."""
    if not s:
        return None
    for p in s["periodos"]:
        if p["fim"] < inicio and p["saldo"] > 0:
            return p
    return None


def _descansos(db: Session, u: Usuario, inicio: date) -> set[date]:
    """Feriados e dias de repouso semanal logo depois do início (art. 134, §3º)."""
    dias = [inicio + timedelta(days=i) for i in (1, 2)]
    fora = set(db.scalars(select(Feriado.data).where(Feriado.empresa_id == u.empresa_id,
                                                      Feriado.data.in_(dias))))
    turnos = {t.data: t for t in db.scalars(select(Turno).where(Turno.usuario_id == u.id,
                                                                Turno.data.in_(dias)))}
    for d in dias:
        t = turnos.get(d)
        if (t and t.folga) or (not t and d.weekday() == 6):   # sem escala, o repouso é o domingo
            fora.add(d)
    return fora


def conferir(db: Session, u: Usuario, inicio: date, fim: date, abono: int,
             ignorar_id: int | None = None) -> list[str]:
    """Avisos da CLT para férias de `inicio` a `fim` (com `abono` dias vendidos)."""
    hoje = regras.hoje(u.empresa)
    s = situacao(db, u, hoje)
    if s and ignorar_id:   # pedido já aprovado sendo conferido de novo: tira ele da conta
        for p in s["periodos"]:
            for g in p["gozos"]:
                if g.get("id") == ignorar_id:
                    p["saldo"] += g["dias"]
    if not u.data_admissao:
        return ["Sem data de admissão na ficha: o sistema não sabe o saldo de férias."]
    p = periodo_para(s, inicio)
    anteriores = [clt.dias_corridos(g["inicio"], g["fim"]) for g in p["gozos"]
                  if g.get("id") != ignorar_id] if p else []
    return clt.conferir_ferias(inicio, fim, abono, hoje, p, anteriores, _descansos(db, u, inicio))


def _abono(txt: str) -> int | None:
    txt = (txt or "0").strip() or "0"
    return int(txt) if txt.isdigit() and int(txt) <= 10 else None


# ------------------------------------------------------------------ gestor
def _tela(request: Request, u: Usuario, db: Session, previa: dict | None = None, status: int = 200):
    e = u.empresa
    linhas, sem_data = da_equipe(db, e)
    equipe = [p for p in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id,
                                                          Usuario.ativo.is_(True)).order_by(Usuario.nome))
              if participa(p)]
    hoje = regras.hoje(e)
    return render(request, "painel_ferias.html", status, u=u, e=e, aba="ausencias", linhas=linhas,
                  sem_data=sem_data, equipe=equipe, hoje=hoje, previa=previa,
                  SITUACOES_FERIAS=SITUACOES, valor=clt.valor_ferias,
                  sugestao=hoje + timedelta(days=clt.AVISO_DIAS))


@rota.get("/painel/ferias")
def ferias_tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    if u.empresa.pais == "US":   # férias da CLT: nos EUA o descanso remunerado é o PTO, em Ausências
        return ir("/painel/ausencias")
    return _tela(request, u, db)


@rota.post("/painel/ferias", dependencies=CSRF)
def ferias_lancar(request: Request, usuario_id: int = Form(...), inicio: str = Form(""),
                  fim: str = Form(""), abono: str = Form("0"), obs: str = Form(""),
                  confirmar: str = Form(""), u: Usuario = Depends(gestor_logado),
                  db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    ini, ate, ab = data_iso(inicio, None), data_iso(fim, None), _abono(abono)
    if not ini or not ate or ate < ini or (ate - ini).days > 59 or ab is None:
        avisar(request, "Confira as datas das férias e os dias vendidos (de 0 a 10).", "erro")
        return ir("/painel/ferias")
    if apuracao.esta_fechado(db, u.empresa, ini):
        avisar(request, MSG_FECHADO, "erro")
        return ir("/painel/ferias")
    if db.scalar(select(Ausencia.id).where(Ausencia.usuario_id == f.id, Ausencia.situacao == "aprovado",
                                           Ausencia.inicio <= ate, Ausencia.fim >= ini)):
        avisar(request, f"{f.primeiro_nome} já tem uma ausência lançada nesses dias.", "erro")
        return ir("/painel/ferias")
    avisos = conferir(db, f, ini, ate, ab)
    if avisos and not confirmar:
        return _tela(request, u, db, previa=dict(f=f, inicio=ini, fim=ate, abono=ab,
                                                 obs=obs.strip()[:300], avisos=avisos))
    a = Ausencia(empresa_id=u.empresa_id, usuario_id=f.id, tipo="ferias", inicio=ini, fim=ate,
                 abono_dias=ab, obs=obs.strip()[:300], situacao="aprovado", criado_por=u.nome,
                 decidido_por=u.nome, criado_em=banco.agora())
    db.add(a)
    regras.registrar(db, u.empresa_id, u.nome, "férias lançadas",
                     f"{f.nome}: {ini:%d/%m/%Y} a {ate:%d/%m/%Y} ({clt.dias_corridos(ini, ate)} dias"
                     + (f", {ab} vendidos" if ab else "") + ")"
                     + (f". Lançadas mesmo com os avisos: {' '.join(avisos)}" if avisos else "."))
    db.commit()
    avisar(request, f"Férias de {f.primeiro_nome} lançadas. Nesses dias o ponto não é cobrado.")
    return ir("/painel/ferias")


@rota.post("/painel/ferias/ajuste", dependencies=CSRF)
def ferias_ajuste(request: Request, usuario_id: int = Form(...), periodo: str = Form(""),
                  dias: str = Form(""), motivo: str = Form(""), u: Usuario = Depends(gestor_logado),
                  db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    s = situacao(db, f)
    ini = data_iso(periodo, None)
    motivo = motivo.strip()[:200]
    try:
        n = int(dias.strip())
    except ValueError:
        n = 0
    if not s or not ini or ini not in {p["inicio"] for p in s["periodos"]}:
        avisar(request, "Escolha o período aquisitivo.", "erro")
    elif not n or abs(n) > 60:
        avisar(request, "Informe os dias (ex.: 30 para somar, -6 para tirar).", "erro")
    elif not motivo:
        avisar(request, "Diga o motivo do ajuste (ex.: saldo de antes do sistema).", "erro")
    else:
        db.add(FeriasAjuste(empresa_id=u.empresa_id, usuario_id=f.id, periodo_inicio=ini, dias=n,
                            motivo=motivo, autor=u.nome, criado_em=banco.agora()))
        regras.registrar(db, u.empresa_id, u.nome, "ajuste de férias",
                         f"{f.nome}, período a partir de {ini:%d/%m/%Y}: {n:+d} dia(s) ({motivo}).")
        db.commit()
        avisar(request, f"Saldo de férias de {f.primeiro_nome} ajustado.")
    return ir("/painel/ferias")


@rota.post("/painel/ferias/ajuste/{aid}/remover", dependencies=CSRF)
def ferias_ajuste_remover(request: Request, aid: int, u: Usuario = Depends(gestor_logado),
                          db: Session = Depends(get_db)):
    a = da_empresa(db, FeriasAjuste, aid, u)
    regras.registrar(db, u.empresa_id, u.nome, "ajuste de férias removido",
                     f"{a.usuario.nome}, período a partir de {a.periodo_inicio:%d/%m/%Y}: "
                     f"{a.dias:+d} dia(s) ({a.motivo}).")
    db.delete(a)
    db.commit()
    avisar(request, "Ajuste removido.")
    return ir("/painel/ferias")


# ------------------------------------------------------------------ funcionário
@rota.post("/app/ferias", dependencies=CSRF)
def app_ferias(request: Request, tarefas_bg: BackgroundTasks, inicio: str = Form(""),
               fim: str = Form(""), abono: str = Form("0"), obs: str = Form(""),
               u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    hoje = regras.hoje(e)
    ini, ate, ab = data_iso(inicio, None), data_iso(fim, None), _abono(abono)
    erro = None
    if not participa(u):
        erro = "Seu tipo de contrato não tem férias pelo aplicativo. Fale com o gestor."
    elif not ini or not ate or ate < ini or (ate - ini).days > 59 or ab is None:
        erro = "Confira as datas das férias (o último dia não pode ser antes do primeiro)."
    elif ini <= hoje:
        erro = "Peça as férias com antecedência: o primeiro dia precisa ser depois de hoje."
    elif (db.scalar(select(func.count()).select_from(Ausencia).where(
            Ausencia.usuario_id == u.id, Ausencia.situacao == "pendente")) or 0) >= 5:
        erro = "Você já tem 5 envios aguardando resposta."
    if erro:
        avisar(request, erro, "erro")
        return ir("/app/ajustes#ferias")
    a = Ausencia(empresa_id=e.id, usuario_id=u.id, tipo="ferias", inicio=ini, fim=ate, abono_dias=ab,
                 obs=obs.strip()[:300], situacao="pendente", criado_por=u.nome, criado_em=banco.agora())
    alerta = Alerta(empresa_id=e.id, usuario_id=u.id, tipo="ausencia", criado_em=banco.agora(),
                    texto=f"{u.nome} pediu férias de {ini:%d/%m} a {ate:%d/%m}"
                          + (f", vendendo {ab} dia(s)" if ab else "") + ".")
    db.add_all([a, alerta])
    db.commit()
    tarefas_bg.add_task(push.avisar_alertas, [alerta.id])
    avisar(request, "Pedido de férias enviado ao gestor. Você será avisado da resposta.")
    return ir("/app/ajustes#ferias")


# ------------------------------------------------------------------ conformidade
def item_conformidade(db: Session, e) -> dict:
    """Férias vencidas (em dobro), vencendo em até 60 dias e pessoas sem data de admissão."""
    linhas, sem_data = da_equipe(db, e)
    vencidos = [(s["u"], p) for s in linhas for p in s["vencidos"]]
    vencendo = [(s["u"], p) for s in linhas for p in s["vencendo"]]
    det = [f"{x.nome}: {p['saldo']} dia(s) do período {p['inicio']:%d/%m/%Y} a {p['fim']:%d/%m/%Y}, "
           f"prazo terminou em {p['concessivo']:%d/%m/%Y}" for x, p in vencidos]
    det += [f"{x.nome}: {p['saldo']} dia(s) para dar até {p['concessivo']:%d/%m/%Y}" for x, p in vencendo]
    if sem_data:
        det.append(f"{len(sem_data)} pessoa(s) sem data de admissão: " + ", ".join(x.nome for x in sem_data[:6]))
    if vencidos:
        sit, resumo = "problema", (f"{len(vencidos)} período(s) de férias com o prazo vencido: os dias "
                                   "gozados depois do prazo são pagos em dobro (CLT art. 137).")
    elif vencendo:
        sit, resumo = "atencao", (f"{len(vencendo)} período(s) de férias vencem nos próximos 60 dias. "
                                  "Marque as férias e avise a pessoa com 30 dias (art. 135).")
    elif sem_data:
        sit, resumo = "atencao", "Sem a data de admissão o sistema não calcula as férias dessas pessoas."
    else:
        sit, resumo = "ok", "Nenhuma férias vencida ou perto de vencer."
    return {"chave": "ferias", "titulo": "Férias", "situacao": sit, "resumo": resumo,
            "detalhes": det[:12], "acao": ("Abrir férias", "/painel/ferias") if det else None}
