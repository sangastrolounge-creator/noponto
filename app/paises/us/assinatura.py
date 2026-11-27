"""Assinatura das empresas dos EUA, cobrada pelo Stripe (o Brasil continua no Asaas).

A empresa assina pelo Stripe Checkout (cartão, mensal ou anual) e troca o cartão, vê as faturas
ou cancela no portal do Stripe (Billing Portal). O acesso é liberado quando o Stripe avisa
(webhook `invoice.paid`): fica pago até o fim do período da fatura. Tudo é idempotente.
O valor sai da mesma conta do Brasil (faixa, pessoas que trabalharam e cupom), em dólares.

Chave: STRIPE_SECRET_KEY no .env, ou colada pelo dono da plataforma em Master -> Stripe (guardada
na tabela config; o .env tem prioridade). Ao colar a chave, o webhook é criado sozinho na conta do
Stripe e o segredo dele fica guardado aqui. Nada disso passa pelo chat nem aparece na tela.

As telas da empresa ficam em /eua/painel/assinatura...; o RotasPorPais manda para cá quem é de
uma empresa dos EUA e abre /painel/assinatura."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import avisos_master, cobranca, crescimento, regras
from ... import db as banco
from ...core import (admin_logado, avisar, base_url, dentro_do_limite, exigir_csrf,
                     funcionarios_ativos, gestor_logado, ir, limite_do_plano, limite_funcionarios, log,
                     plataforma, render, situacao, validade)
from ...db import AsaasEvento, Config, Empresa, Pagamento, Plano, SessionLocal, Usuario, get_db

rota = APIRouter()          # telas da empresa (montadas em /eua, só para empresas dos EUA)
rota_geral = APIRouter()    # Master -> Stripe e o webhook
CSRF = [Depends(exigir_csrf)]
REF = "dwus"                # client_reference_id / metadados: dwus:<empresa>
VALOR_MINIMO_CENTAVOS = 50  # o Stripe não cobra menos de US$ 0,50
CFG_CHAVE, CFG_WEBHOOK, CFG_WEBHOOK_ID = "stripe_secret_key", "stripe_webhook_secret", "stripe_webhook_id"
EVENTOS = ["checkout.session.completed", "invoice.paid", "invoice.payment_failed",
           "customer.subscription.deleted"]
VOLTA = "/painel/assinatura"


# ------------------------------------------------------------------ chave e cliente do SDK
def _cfg(nome: str) -> str:
    with SessionLocal() as db:
        c = db.get(Config, nome)
        return (c.valor if c else "").strip()


def chave_secreta() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "").strip() or _cfg(CFG_CHAVE)


def configurado() -> bool:
    return bool(chave_secreta())


def segredo_webhook() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip() or _cfg(CFG_WEBHOOK)


def modo() -> str:
    """'teste', 'real' ou '' (sem chave)."""
    k = chave_secreta()
    return "" if not k else "teste" if "_test_" in k else "real"


def cliente(chave: str | None = None):
    """Cliente do SDK oficial. Os testes trocam esta função por um simulado."""
    import stripe
    return stripe.StripeClient(chave or chave_secreta())


# ------------------------------------------------------------------ planos e valores
def planos(db: Session) -> list[Plano]:
    return list(db.scalars(select(Plano).where(Plano.ativo.is_(True), Plano.pais == "US")
                           .order_by(Plano.ordem, Plano.preco_centavos)))


def plano_gratis(db: Session) -> Plano | None:
    return db.scalar(select(Plano).where(Plano.ativo.is_(True), Plano.pais == "US",
                                         Plano.preco_centavos == 0, Plano.por_pessoa_centavos == 0)
                     .order_by(Plano.ordem).limit(1))


def _intervalo(ciclo: str) -> str:
    return "year" if ciclo == "anual" else "month"


def ajustar_valores(db: Session, forcar: bool = False) -> int:
    """Uma vez por dia: muda a faixa e atualiza no Stripe o valor da próxima fatura (pessoas,
    faixa, fim do cupom), sem cobrança proporcional. Devolve quantas assinaturas mudaram."""
    if not configurado():
        return 0
    hoje = banco.agora().date()
    mudou = 0
    for e in list(db.scalars(select(Empresa).where(Empresa.stripe_assinatura_id != ""))):
        p = e.plano
        if not p or (not forcar and e.valor_conferido_em and e.valor_conferido_em.date() == hoje):
            continue
        mensal, n, faixa = crescimento.calcular(db, e, p)
        valor = crescimento.valor_do_ciclo(mensal, e.ciclo)
        if faixa and faixa.id != p.id:
            e.plano_id, e.plano = faixa.id, faixa
            regras.registrar(db, e.id, "cobrança", "faixa", f"Empresa passou para a faixa {faixa.nome} ({n} pessoa(s)).")
        e.pessoas_cobradas, e.valor_conferido_em = n, banco.agora()
        if not p.por_pessoa and not p.grupo and (not e.cupom_id or valor == e.valor_assinatura_centavos):
            db.commit()
            continue
        try:
            sub = cliente().v1.subscriptions.retrieve(e.stripe_assinatura_id)
            item = sub["items"]["data"][0]
            preco = item["price"]
            if valor >= VALOR_MINIMO_CENTAVOS and int(preco["unit_amount"] or 0) != valor:
                produto = preco["product"] if isinstance(preco["product"], str) else preco["product"]["id"]
                cliente().v1.subscriptions.update(e.stripe_assinatura_id, {
                    "items": [{"id": item["id"], "price_data": {
                        "currency": "usd", "product": produto, "unit_amount": valor,
                        "recurring": {"interval": _intervalo(e.ciclo)}}}],
                    "proration_behavior": "none"})
                mudou += 1
                log.info("Assinatura Stripe da empresa %s: %s pessoa(s), próxima fatura %s centavos", e.id, n, valor)
            e.valor_assinatura_centavos = valor
        except Exception as ex:  # noqa: BLE001 — Stripe fora do ar ou assinatura apagada: amanhã de novo
            log.warning("Não deu para ajustar a assinatura Stripe da empresa %s: %s", e.id, ex)
        db.commit()
    return mudou


# ------------------------------------------------------------------ avisos do Stripe
def _data(ts) -> datetime | None:
    return datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None) if ts else None


def _assinatura_da_fatura(inv: dict) -> str:
    """Muda de lugar conforme a versão da API do Stripe."""
    if inv.get("subscription"):
        return inv["subscription"] if isinstance(inv["subscription"], str) else inv["subscription"].get("id", "")
    det = ((inv.get("parent") or {}).get("subscription_details") or {})
    return det.get("subscription") or ""


def _fim_do_periodo(inv: dict) -> datetime | None:
    linhas = ((inv.get("lines") or {}).get("data") or [])
    fins = [li.get("period", {}).get("end") for li in linhas if li.get("period")]
    return _data(max(fins)) if fins else _data(inv.get("period_end"))


def _ref_da_fatura(inv: dict) -> str:
    for det in ((inv.get("parent") or {}).get("subscription_details") or {},
                inv.get("subscription_details") or {}):
        ref = str((det.get("metadata") or {}).get("ref") or "")
        if ref:
            return ref
    return ""


def _empresa_da_ref(db: Session, ref: str) -> Empresa | None:
    if ref.startswith(f"{REF}:") and ref.split(":", 1)[1].isdigit():
        e = db.get(Empresa, int(ref.split(":", 1)[1]))
        if e and e.pais == "US":
            return e
    return None


def empresa_da_fatura(db: Session, inv: dict) -> Empresa | None:
    e = _empresa_da_ref(db, _ref_da_fatura(inv))
    if e:
        return e
    sub = _assinatura_da_fatura(inv)
    e = db.scalar(select(Empresa).where(Empresa.stripe_assinatura_id == sub)) if sub else None
    if not e and inv.get("customer"):
        e = db.scalar(select(Empresa).where(Empresa.stripe_cliente_id == inv["customer"]))
    return e


def checkout_concluido(db: Session, s: dict) -> None:
    e = _empresa_da_ref(db, str(s.get("client_reference_id") or ""))
    if not e:
        return
    e.stripe_cliente_id = s.get("customer") or e.stripe_cliente_id
    e.stripe_assinatura_id = s.get("subscription") or e.stripe_assinatura_id
    meta = s.get("metadata") or {}
    plano_id = int(meta.get("plano_id") or 0)
    p = db.get(Plano, plano_id) if plano_id else None
    if p and p.pais == "US":
        e.plano_id = plano_id
    if meta.get("ciclo") in ("mensal", "anual"):
        e.ciclo = meta["ciclo"]
    db.commit()


def _registro(db: Session, e: Empresa, inv: dict, status: str) -> Pagamento:
    reg = Pagamento(empresa_id=e.id, tipo="assinatura", status=status, stripe_id=inv["id"],
                    plano_id=e.plano_id, plano_nome=e.plano.nome if e.plano else "",
                    dias=365 if e.ciclo == "anual" else 30, forma="cartao",
                    registrado_por="stripe", criado_em=banco.agora())
    db.add(reg)
    return reg


def fatura_paga(db: Session, inv: dict) -> bool:
    e = empresa_da_fatura(db, inv)
    if not e:
        return False
    reg = db.scalar(select(Pagamento).where(Pagamento.stripe_id == inv["id"]))
    if reg and reg.status == "pago":
        return True
    agora = banco.agora()
    if not reg:
        reg = _registro(db, e, inv, "pendente")
    reg.status, reg.pago_em = "pago", agora
    reg.valor_centavos = int(inv.get("amount_paid") or 0)
    reg.fatura_url = (inv.get("hosted_invoice_url") or "")[:300]
    fim = _fim_do_periodo(inv) or agora + timedelta(days=31)
    e.pago_ate = max(fim, e.pago_ate or agora)
    db.commit()
    log.info("Fatura Stripe %s paga: empresa %s liberada até %s", inv["id"], e.id, e.pago_ate)
    crescimento.apos_pagamento(db, e, reg)   # indicação e comissão do parceiro (US$ 0 = teste: nada)
    return True


def fatura_falhou(db: Session, inv: dict) -> bool:
    e = empresa_da_fatura(db, inv)
    if not e:
        return False
    reg = db.scalar(select(Pagamento).where(Pagamento.stripe_id == inv["id"]))
    if reg and reg.status == "pago":
        return True
    if not reg:
        reg = _registro(db, e, inv, "vencido")
    reg.status = "vencido"
    reg.valor_centavos = int(inv.get("amount_due") or 0)
    reg.fatura_url = (inv.get("hosted_invoice_url") or "")[:300]
    db.commit()
    return True


def assinatura_encerrada(db: Session, e: Empresa):
    e.stripe_assinatura_id = ""
    for reg in db.scalars(select(Pagamento).where(
            Pagamento.empresa_id == e.id, Pagamento.tipo == "assinatura",
            Pagamento.status.in_(("pendente", "vencido")))):
        reg.status = "cancelado"
    db.commit()


# ------------------------------------------------------------------ telas da empresa
@rota.get("/painel/assinatura")
def assinatura(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    aberta = db.scalar(select(Pagamento).where(
        Pagamento.empresa_id == e.id, Pagamento.status == "vencido", Pagamento.fatura_url != "",
        Pagamento.criado_em >= banco.agora() - timedelta(days=45))
        .order_by(Pagamento.criado_em.desc()).limit(1))
    pagos = list(db.scalars(select(Pagamento).where(Pagamento.empresa_id == e.id, Pagamento.status == "pago")
                            .order_by(Pagamento.pago_em.desc()).limit(12)))
    lista = planos(db)
    return render(request, "us/painel_assinatura.html", u=u, e=e, aba="assinatura",
                  planos=lista, usados=funcionarios_ativos(db, e.id), limite=limite_funcionarios(e),
                  stripe_ok=configurado(), cobradas=cobranca.pessoas_cobradas(db, e), aberta=aberta,
                  pagos=pagos, gratis=plano_gratis(db),
                  precos={p.id: cobranca.valor_atual(db, e, p)[0] for p in lista},
                  proxima=cobranca.valor_atual(db, e)[0] if e.plano else 0,
                  meses_anual=crescimento.meses_anual(), cupom=crescimento.cupom_valido(db, e),
                  texto_cupom=crescimento.texto_cupom, indica=crescimento.resumo_indicacoes(db, e),
                  voltou=request.query_params.get("ok") == "1")


@rota.post("/painel/assinatura/gratis", dependencies=CSRF)
def ir_para_gratis(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e, p = u.empresa, plano_gratis(db)
    from ...fase3 import locais_da
    erro = None
    if not p:
        erro = "Não há plano grátis disponível."
    elif situacao(e) == "cortesia":
        erro = "Sua conta é cortesia: não há o que mudar."
    elif e.stripe_assinatura_id:
        erro = "Cancele antes a assinatura em “Gerenciar assinatura”."
    elif limite_do_plano(p) and funcionarios_ativos(db, e.id) > limite_do_plano(p):
        erro = (f"O plano {p.nome} permite até {limite_do_plano(p)} funcionários e você tem "
                f"{funcionarios_ativos(db, e.id)} ativos. Desative alguém em Equipe antes.")
    elif p.limite_locais and 1 + len(locais_da(db, e.id)) > p.limite_locais:
        erro = (f"O plano {p.nome} permite {p.limite_locais} local(is) de ponto. Desative os "
                "locais extras em Locais antes.")
    if erro:
        avisar(request, erro, "erro")
        return ir(VOLTA)
    e.plano_id, e.teste_ate = p.id, None
    e.plano = p
    regras.registrar(db, e.id, u.nome, "plano grátis", f"Empresa passou para o plano {p.nome}.")
    db.commit()
    avisar(request, f"Pronto: sua empresa está no plano {p.nome}, sem cobrança.")
    return ir("/painel")


@rota.post("/painel/assinatura/cupom", dependencies=CSRF)
def usar_cupom(request: Request, codigo: str = Form(""), u: Usuario = Depends(gestor_logado),
               db: Session = Depends(get_db)):
    e = u.empresa
    erro = ("Cancele a assinatura em “Gerenciar assinatura” antes de usar um cupom; depois assine de novo."
            if e.stripe_assinatura_id else crescimento.aplicar_cupom(db, e, codigo))
    if erro:
        avisar(request, erro, "erro")
    else:
        db.commit()
        avisar(request, f"Cupom aplicado: {crescimento.texto_cupom(db.get(crescimento.Cupom, e.cupom_id))}.")
    return ir(VOLTA)


@rota.post("/painel/assinatura/pagar", dependencies=CSRF)
def pagar(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db),
          plano: int = Form(0), ciclo: str = Form("mensal")):
    e = u.empresa
    ciclo = "anual" if ciclo == "anual" else "mensal"
    escolhido = next((p for p in planos(db) if p.id == plano), None)
    mensal = cobranca.valor_atual(db, e, escolhido)[0] if escolhido else 0
    valor = crescimento.valor_do_ciclo(mensal, ciclo)
    erro = None
    if not configurado():
        erro = "O pagamento online não está disponível. Fale com o suporte."
    elif situacao(e) == "cortesia":
        erro = "Sua conta é cortesia: não há o que pagar."
    elif not escolhido or escolhido.gratis:
        erro = "Escolha um plano."
    elif valor < VALOR_MINIMO_CENTAVOS:
        erro = "Este plano não tem valor para cobrança online. Fale com o suporte."
    elif limite_do_plano(escolhido) and funcionarios_ativos(db, e.id) > limite_do_plano(escolhido):
        erro = (f"O plano {escolhido.nome} permite até {limite_do_plano(escolhido)} funcionários e você "
                f"tem {funcionarios_ativos(db, e.id)} ativos. Escolha um plano maior ou desative alguém em Equipe.")
    elif e.stripe_assinatura_id:
        erro = "A assinatura já está ativa. Para mudar de plano ou de cartão, use “Gerenciar assinatura”."
    elif not dentro_do_limite(f"pagar:{e.id}", 10, 3600):
        erro = "Muitas tentativas de pagamento. Aguarde um pouco."
    if erro:
        avisar(request, erro, "erro")
        return ir(VOLTA)
    retorno = f"{base_url(request)}{VOLTA}"
    params = {
        "mode": "subscription",
        "client_reference_id": f"{REF}:{e.id}",
        "line_items": [{"quantity": 1, "price_data": {
            "currency": "usd", "unit_amount": valor,
            "recurring": {"interval": _intervalo(ciclo)},
            "product_data": {"name": f"{plataforma()['nome']} · {escolhido.nome}"}}}],
        "metadata": {"plano_id": str(escolhido.id), "empresa_id": str(e.id), "ciclo": ciclo},
        # A assinatura leva a empresa nos metadados: a fatura sabe de quem é mesmo se chegar antes
        # do "checkout concluído" (o Stripe não garante a ordem dos avisos).
        "subscription_data": {"metadata": {"ref": f"{REF}:{e.id}", "empresa_id": str(e.id)}},
        "success_url": f"{retorno}?ok=1",
        "cancel_url": retorno,
        "locale": "auto",
    }
    if e.stripe_cliente_id:
        params["customer"] = e.stripe_cliente_id
    elif u.email:
        params["customer_email"] = u.email
    # Quem ainda tem dias (teste ou já pagos) só começa a ser cobrado quando eles acabarem.
    v = validade(e) if situacao(e) in ("ativa", "teste") else None
    if v and v > banco.agora() + timedelta(days=2):
        params["subscription_data"]["trial_end"] = int(v.replace(tzinfo=timezone.utc).timestamp())
    try:
        sessao = cliente().v1.checkout.sessions.create(params)
    except Exception as ex:  # noqa: BLE001 — erro do Stripe ou de conexão
        log.warning("Stripe Checkout falhou para a empresa %s: %s", e.id, ex)
        avisar(request, "Não foi possível abrir o pagamento agora. Tente de novo.", "erro")
        return ir(VOLTA)
    e.valor_assinatura_centavos = valor
    db.commit()
    return ir(sessao["url"])


@rota.post("/painel/assinatura/portal", dependencies=CSRF)
def portal(request: Request, u: Usuario = Depends(gestor_logado)):
    """Portal do Stripe: trocar cartão, ver faturas, cancelar."""
    e = u.empresa
    if not configurado() or not e.stripe_cliente_id:
        avisar(request, "Ainda não há assinatura para gerenciar.", "erro")
        return ir(VOLTA)
    try:
        s = cliente().v1.billing_portal.sessions.create(
            {"customer": e.stripe_cliente_id, "return_url": f"{base_url(request)}{VOLTA}"})
    except Exception as ex:  # noqa: BLE001
        log.warning("Portal do Stripe falhou para a empresa %s: %s", e.id, ex)
        avisar(request, "Não foi possível abrir o portal agora. Tente de novo.", "erro")
        return ir(VOLTA)
    return ir(s["url"])


# ------------------------------------------------------------------ Master
@rota_geral.post("/master/empresas/{empresa_id}/cancelar-stripe", dependencies=CSRF)
def master_cancelar(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                    db: Session = Depends(get_db)):
    e = db.get(Empresa, empresa_id)
    if not e:
        return ir("/master")
    if not e.stripe_assinatura_id:
        avisar(request, "Não há assinatura ativa.", "erro")
    else:
        try:
            cliente().v1.subscriptions.cancel(e.stripe_assinatura_id)
        except Exception as ex:  # noqa: BLE001
            log.warning("Stripe recusou o cancelamento da empresa %s: %s", e.id, ex)
            avisar(request, "Não foi possível encerrar a assinatura no Stripe agora. Tente de novo.", "erro")
            return ir(f"/master/empresas/{e.id}")
        assinatura_encerrada(db, e)
        regras.registrar(db, e.id, u.nome, "assinatura encerrada", "Assinatura do Stripe encerrada pelo Master.")
        db.commit()
        avisar(request, "Assinatura encerrada no Stripe.")
    return ir(f"/master/empresas/{e.id}")


def _guardar(db: Session, nome: str, valor: str | None) -> None:
    c = db.get(Config, nome)
    if valor is None:
        if c:
            db.delete(c)
        return
    if not c:
        c = Config(chave=nome, valor="")
        db.add(c)
    c.valor = valor


@rota_geral.get("/master/stripe")
def master_stripe(request: Request, u: Usuario = Depends(admin_logado)):
    return render(request, "master/stripe.html", u=u, e=u.empresa, aba="master", sub="stripe",
                  modo=modo(), webhook_ok=len(segredo_webhook()) >= 10,
                  chave_env=bool(os.environ.get("STRIPE_SECRET_KEY", "").strip()),
                  url_webhook=f"{base_url(request)}/stripe/webhook", eventos=EVENTOS)


@rota_geral.post("/master/stripe/chave", dependencies=CSRF)
def master_stripe_chave(request: Request, chave: str = Form(""), u: Usuario = Depends(admin_logado),
                        db: Session = Depends(get_db)):
    """Confere a chave no Stripe, cria o webhook na conta e guarda as duas coisas."""
    chave = chave.strip()
    if not (chave.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")) and len(chave) > 20):
        avisar(request, "Essa não parece uma chave secreta do Stripe (começa com sk_test_ ou sk_live_).", "erro")
        return ir("/master/stripe")
    url = f"{base_url(request)}/stripe/webhook"
    if not url.startswith("https://"):
        avisar(request, "O webhook precisa de endereço https. Configure o domínio antes.", "erro")
        return ir("/master/stripe")
    try:
        c = cliente(chave)
        c.v1.balance.retrieve()   # a chave vale?
    except Exception as ex:  # noqa: BLE001
        log.warning("Chave do Stripe recusada: %s", type(ex).__name__)
        avisar(request, "O Stripe recusou esta chave. Confira se copiou a chave secreta inteira.", "erro")
        return ir("/master/stripe")
    antigo, chave_antiga = _cfg(CFG_WEBHOOK_ID), _cfg(CFG_CHAVE)
    try:
        if antigo:
            try:   # o webhook antigo é apagado com a chave ANTIGA (pode ser de outro modo: real/teste)
                cliente(chave_antiga or chave).v1.webhook_endpoints.delete(antigo)
            except Exception:  # noqa: BLE001 — já tinha sido apagado no Stripe
                pass
        wh = c.v1.webhook_endpoints.create({
            "url": url, "enabled_events": EVENTOS,
            "description": f"{plataforma()['nome']} (assinaturas EUA)"})
    except Exception as ex:  # noqa: BLE001
        log.warning("Webhook do Stripe não criado: %s", ex)
        avisar(request, "A chave vale, mas o Stripe não deixou criar o webhook. Se for uma chave "
                        "restrita (rk_), dê a ela permissão de escrita em Webhook Endpoints.", "erro")
        return ir("/master/stripe")
    _guardar(db, CFG_CHAVE, chave)
    _guardar(db, CFG_WEBHOOK, wh["secret"])
    _guardar(db, CFG_WEBHOOK_ID, wh["id"])
    regras.registrar(db, u.empresa_id, u.nome, "Stripe ligado",
                     f"Chave em modo {'teste' if '_test_' in chave else 'real'}; webhook {url}.")
    db.commit()
    avisar(request, "Stripe ligado. A chave foi conferida e o webhook foi criado na sua conta do Stripe.")
    return ir("/master/stripe")


@rota_geral.post("/master/stripe/desligar", dependencies=CSRF)
def master_stripe_desligar(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    antigo = _cfg(CFG_WEBHOOK_ID)
    if antigo and _cfg(CFG_CHAVE):
        try:
            cliente(_cfg(CFG_CHAVE)).v1.webhook_endpoints.delete(antigo)
        except Exception:  # noqa: BLE001
            pass
    for nome in (CFG_CHAVE, CFG_WEBHOOK, CFG_WEBHOOK_ID):
        _guardar(db, nome, None)
    regras.registrar(db, u.empresa_id, u.nome, "Stripe desligado", "Chave e webhook removidos.")
    db.commit()
    avisar(request, "Stripe desligado nesta tela. Assinaturas já feitas continuam no Stripe.")
    return ir("/master/stripe")


# ------------------------------------------------------------------ webhook
def verificar(payload: bytes, assinatura_cab: str) -> dict:
    """Confere a assinatura do aviso (Stripe-Signature) e devolve o evento como dict."""
    import stripe
    try:
        stripe.Webhook.construct_event(payload, assinatura_cab, segredo_webhook())
    except (ValueError, stripe.SignatureVerificationError) as ex:
        raise HTTPException(400, "Assinatura inválida") from ex
    return json.loads(payload)


@rota_geral.post("/stripe/webhook")
async def webhook(request: Request, tarefas_bg: BackgroundTasks, db: Session = Depends(get_db)):
    if len(segredo_webhook()) < 10:
        raise HTTPException(503, "Webhook não configurado")
    ev = verificar(await request.body(), request.headers.get("stripe-signature", ""))
    eid, tipo = str(ev.get("id") or "")[:80], str(ev.get("type") or "")
    if eid and db.get(AsaasEvento, eid):    # a tabela guarda os avisos já tratados (Asaas e Stripe)
        return {"status": "ok"}
    obj = (ev.get("data") or {}).get("object") or {}
    if tipo == "checkout.session.completed":
        checkout_concluido(db, obj)
    elif tipo in ("invoice.paid", "invoice.payment_failed"):
        pago = tipo == "invoice.paid"
        ok = fatura_paga(db, obj) if pago else fatura_falhou(db, obj)
        valor = int(obj.get("amount_paid" if pago else "amount_due") or 0)
        if ok and valor > 0:   # US$ 0 = período de teste
            e = empresa_da_fatura(db, obj)
            if e:
                tarefas_bg.add_task(avisos_master.pagamento, e.id, valor, pago)
        recente = int(obj.get("created") or 0) > time.time() - 3 * 3600
        if not ok and recente and (_assinatura_da_fatura(obj) or _ref_da_fatura(obj)):
            # Provavelmente o "checkout concluído" ainda não chegou: o Stripe reenvia em minutos.
            raise HTTPException(409, "Empresa ainda não ligada a esta assinatura")
    elif tipo == "customer.subscription.deleted":
        e = db.scalar(select(Empresa).where(Empresa.stripe_assinatura_id == obj.get("id")))
        if e:
            assinatura_encerrada(db, e)
            tarefas_bg.add_task(avisos_master.cancelada, e.id)
    if eid:
        db.add(AsaasEvento(id=eid, evento=tipo[:40], criado_em=banco.agora()))
        db.commit()
    return {"status": "ok"}
