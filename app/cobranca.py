"""Assinatura da empresa e cobrança pelo Asaas (mesmo desenho do avalia-app).

Avulso: uma cobrança que libera 30 dias. Automática: assinatura mensal no Asaas.
O acesso é liberado quando o Asaas avisa (webhook) ou quando a tela consulta a
cobrança pendente. Tudo é idempotente: confirmar duas vezes não soma dias a mais.

Planos por pessoa: valor = base + preço por pessoa que bateu o ponto nos últimos 30 dias (com
mínimo). Uma vez por dia o valor das próximas cobranças da assinatura é atualizado no Asaas.
Plano grátis (preço 0): a empresa passa para ele sem cobrança, com os limites do plano.
"""
import hmac
import os
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import asaas, avisos_master, crescimento, regras
from . import db as banco
from .core import (admin_logado, avisar, base_url, dentro_do_limite, exigir_csrf,
                   funcionarios_ativos, gestor_logado, ir, limite_do_plano, limite_funcionarios, log,
                   plataforma, render, situacao, validade)
from .db import AsaasEvento, Empresa, Pagamento, Plano, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
DIAS_AVULSO = 30
FUSO_HORAS = -3  # o Asaas trabalha no horário de Brasília
FORMAS = {"PIX": "pix", "BOLETO": "boleto", "CREDIT_CARD": "cartao"}
REF = "turno"  # prefixo do externalReference: a conta Asaas é dividida com outros sistemas


def email_do_dono() -> str:
    """E-mail que o Asaas usa para avisar de falha no webhook."""
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if not email:
        with SessionLocal() as db:
            email = db.scalar(select(Usuario.email).where(Usuario.is_admin.is_(True)).limit(1)) or ""
    return email


# ------------------------------------------------------------------ cobrança por pessoa
DIAS_ATIVO = 30


def pessoas_cobradas(db: Session, e: Empresa) -> int:
    """Pessoas que bateram o ponto nos últimos 30 dias (quem não trabalhou não é cobrado)."""
    from .db import Batida
    from .regras import VALIDA
    return db.scalar(select(func.count(func.distinct(Batida.usuario_id))).where(
        Batida.empresa_id == e.id, VALIDA, Batida.momento >= banco.agora() - timedelta(days=DIAS_ATIVO))) or 0


def valor_atual(db: Session, e: Empresa, p: Plano | None = None) -> tuple[int, int]:
    """(valor do mês em centavos, pessoas contadas) para a empresa no plano `p` (ou no dela),
    na faixa mais barata do grupo e com o cupom da empresa."""
    valor, n, _ = crescimento.calcular(db, e, p)
    return valor, n


def ajustar_valores(db: Session, forcar: bool = False) -> int:
    """Uma vez por dia, nas assinaturas: muda a empresa de faixa (planos de um grupo) e atualiza no
    Asaas o valor das próximas cobranças (pessoas, faixa, fim do cupom). Devolve quantas mudaram."""
    if not asaas.configurado():
        return 0
    hoje = banco.agora().date()
    mudou = 0
    for e in list(db.scalars(select(Empresa).where(Empresa.asaas_assinatura_id != ""))):
        p = e.plano
        if not p:
            continue
        if not forcar and e.valor_conferido_em and e.valor_conferido_em.date() == hoje:
            continue
        mensal, n, faixa = crescimento.calcular(db, e, p)
        valor = crescimento.valor_do_ciclo(mensal, e.ciclo)
        if faixa and faixa.id != p.id:
            e.plano_id, e.plano = faixa.id, faixa
            regras.registrar(db, e.id, "cobrança", "faixa",
                             f"Empresa passou para a faixa {faixa.nome} ({n} pessoa(s)).")
            log.info("Empresa %s mudou para a faixa %s", e.id, faixa.nome)
        e.pessoas_cobradas, e.valor_conferido_em = n, banco.agora()
        # Preço fixo sem faixas nem cupom: o valor combinado na assinatura não muda sozinho
        # (se o master mudar o preço do plano, quem já assina continua no preço antigo).
        if not p.por_pessoa and not p.grupo and (not e.cupom_id or valor == e.valor_assinatura_centavos):
            db.commit()
            continue
        try:
            if valor >= asaas.VALOR_MINIMO_CENTAVOS and asaas.valor_assinatura(e.asaas_assinatura_id) != valor:
                asaas.mudar_valor_assinatura(e.asaas_assinatura_id, valor)
                mudou += 1
                log.info("Assinatura da empresa %s: %s pessoa(s), próximas cobranças de %s centavos", e.id, n, valor)
            e.valor_assinatura_centavos = valor
        except asaas.AsaasErro as ex:   # Asaas fora do ar ou assinatura apagada: tenta amanhã
            log.warning("Não deu para ajustar a assinatura da empresa %s: %s", e.id, ex)
        db.commit()
    return mudou


def plano_gratis(db: Session) -> Plano | None:
    return db.scalar(select(Plano).where(Plano.ativo.is_(True), Plano.pais == "BR", Plano.preco_centavos == 0,
                                         Plano.por_pessoa_centavos == 0)
                     .order_by(Plano.ordem).limit(1))


def _planos(db: Session) -> list[Plano]:
    return list(db.scalars(select(Plano).where(Plano.ativo.is_(True), Plano.pais == "BR")
                           .order_by(Plano.ordem, Plano.preco_centavos)))


# ------------------------------------------------- efeitos de um pagamento
def _empresa_da_cobranca(db: Session, pay: dict, reg: Pagamento | None) -> Empresa | None:
    if reg:
        return db.get(Empresa, reg.empresa_id)
    if pay.get("subscription"):
        return db.scalar(select(Empresa).where(
            Empresa.asaas_assinatura_id == pay["subscription"]))
    return None


def _registro(db: Session, e: Empresa, pay: dict, status: str) -> Pagamento:
    """Mensalidades seguintes da assinatura chegam sem registro prévio."""
    reg = Pagamento(empresa_id=e.id, tipo="assinatura", status=status,
                    asaas_id=pay["id"], fatura_url=(pay.get("invoiceUrl") or "")[:300],
                    plano_id=e.plano_id, plano_nome=e.plano.nome if e.plano else "",
                    valor_centavos=round(float(pay.get("value") or 0) * 100),
                    dias=365 if e.ciclo == "anual" else DIAS_AVULSO,
                    forma=FORMAS.get(pay.get("billingType"), "outro"),
                    registrado_por="asaas", criado_em=banco.agora())
    db.add(reg)
    return reg


def confirmar_pagamento(db: Session, pay: dict) -> bool:
    """Pagamento confirmado no Asaas. Devolve False se a cobrança não é desta plataforma."""
    reg = db.scalar(select(Pagamento).where(Pagamento.asaas_id == pay["id"]))
    e = _empresa_da_cobranca(db, pay, reg)
    if not e:
        return False
    if not reg:
        reg = _registro(db, e, pay, "pendente")
    if reg.status == "pago":
        return True
    agora = banco.agora()
    reg.status, reg.pago_em = "pago", agora
    reg.valor_centavos = round(float(pay.get("value") or 0) * 100) or reg.valor_centavos
    reg.forma = FORMAS.get(pay.get("billingType"), reg.forma)
    base = max(agora, e.pago_ate or agora)
    if pay.get("subscription") and pay.get("dueDate"):
        # Assinatura: cobre até o próximo vencimento (mês ou ano).
        fim = asaas.mais_um_ano(pay["dueDate"]) if e.ciclo == "anual" else asaas.mais_um_mes(pay["dueDate"])
        e.pago_ate = max(fim, base)
    else:
        e.pago_ate = base + timedelta(days=reg.dias or DIAS_AVULSO)
    if reg.plano_id and db.get(Plano, reg.plano_id):
        e.plano_id = reg.plano_id
    db.commit()
    log.info("Pagamento %s confirmado: empresa %s liberada até %s", pay["id"], e.id, e.pago_ate)
    crescimento.apos_pagamento(db, e, reg)
    return True


def marcar_vencido(db: Session, pay: dict):
    reg = db.scalar(select(Pagamento).where(Pagamento.asaas_id == pay["id"]))
    e = _empresa_da_cobranca(db, pay, reg)
    if not e or (reg and reg.status == "pago"):
        return
    if not reg:
        reg = _registro(db, e, pay, "vencido")
    reg.status = "vencido"
    db.commit()


def assinatura_encerrada(db: Session, e: Empresa):
    e.asaas_assinatura_id = ""
    for reg in db.scalars(select(Pagamento).where(
            Pagamento.empresa_id == e.id, Pagamento.tipo == "assinatura",
            Pagamento.status.in_(("pendente", "vencido")))):
        reg.status = "cancelado"
    db.commit()


def atualizar_pendentes(db: Session, e: Empresa):
    """Consulta o Asaas quando o aviso ainda não chegou (volta da página de pagamento)."""
    if not asaas.configurado() or not dentro_do_limite(f"asaas-ver:{e.id}", 6, 60):
        return
    recentes = list(db.scalars(select(Pagamento).where(
        Pagamento.empresa_id == e.id, Pagamento.status.in_(("pendente", "vencido")),
        Pagamento.asaas_id.is_not(None),
        Pagamento.criado_em >= banco.agora() - timedelta(days=10))
        .order_by(Pagamento.criado_em.desc()).limit(3)))
    for reg in recentes:
        try:
            pay = asaas.consultar_cobranca(reg.asaas_id)
        except asaas.AsaasErro:
            continue
        if pay.get("status") in asaas.PAGOS:
            confirmar_pagamento(db, pay)
        elif pay.get("deleted"):
            reg.status = "cancelado"
            db.commit()


# --------------------------------------------------------- tela da empresa
@rota.get("/painel/assinatura")
def assinatura(request: Request, u: Usuario = Depends(gestor_logado),
               db: Session = Depends(get_db)):
    e = u.empresa
    atualizar_pendentes(db, e)
    aberta = db.scalar(select(Pagamento).where(
        Pagamento.empresa_id == e.id, Pagamento.status.in_(("pendente", "vencido")),
        Pagamento.fatura_url != "", Pagamento.criado_em >= banco.agora() - timedelta(days=45))
        .order_by(Pagamento.criado_em.desc()).limit(1))
    pagos = list(db.scalars(select(Pagamento).where(
        Pagamento.empresa_id == e.id, Pagamento.status == "pago")
        .order_by(Pagamento.pago_em.desc()).limit(12)))
    planos = _planos(db)
    precos = {p.id: valor_atual(db, e, p)[0] for p in planos}
    return render(request, "painel_assinatura.html", u=u, e=e, aba="assinatura",
                  planos=planos, usados=funcionarios_ativos(db, e.id),
                  limite=limite_funcionarios(e), asaas_ok=asaas.configurado(),
                  cobradas=pessoas_cobradas(db, e), aberta=aberta, pagos=pagos,
                  gratis=plano_gratis(db), precos=precos,
                  proxima=valor_atual(db, e)[0] if e.plano else 0,
                  meses_anual=crescimento.meses_anual(), cupom=crescimento.cupom_valido(db, e),
                  texto_cupom=crescimento.texto_cupom, indica=crescimento.resumo_indicacoes(db, e))


@rota.post("/painel/assinatura/gratis", dependencies=CSRF)
def ir_para_gratis(request: Request, u: Usuario = Depends(gestor_logado),
                   db: Session = Depends(get_db)):
    """Passa a empresa para o plano grátis (sem cobrança, com os limites dele)."""
    e, p = u.empresa, plano_gratis(db)
    from .fase3 import locais_da
    erro = None
    if not p:
        erro = "Não há plano grátis disponível."
    elif situacao(e) == "cortesia":
        erro = "Sua conta é cortesia: não há o que mudar."
    elif e.asaas_assinatura_id:
        erro = "Encerre antes a cobrança automática."
    elif limite_do_plano(p) and funcionarios_ativos(db, e.id) > limite_do_plano(p):
        erro = (f"O plano {p.nome} permite até {limite_do_plano(p)} funcionários e você tem "
                f"{funcionarios_ativos(db, e.id)} ativos. Desative alguém em Equipe antes.")
    elif p.limite_locais and 1 + len(locais_da(db, e.id)) > p.limite_locais:
        erro = (f"O plano {p.nome} permite {p.limite_locais} local(is) de ponto. Desative os "
                "locais extras em Locais antes.")
    if erro:
        avisar(request, erro, "erro")
        return ir("/painel/assinatura")
    e.plano_id, e.teste_ate = p.id, None
    e.plano = p
    regras.registrar(db, e.id, u.nome, "plano grátis", f"Empresa passou para o plano {p.nome}.")
    db.commit()
    avisar(request, f"Pronto: sua empresa está no plano {p.nome}, sem cobrança.")
    return ir("/painel")


@rota.post("/painel/assinatura/pagar", dependencies=CSRF)
def pagar(request: Request, u: Usuario = Depends(gestor_logado),
          db: Session = Depends(get_db), plano: int = Form(0),
          cpf_cnpj: str = Form(""), modo: str = Form("automatica"), ciclo: str = Form("mensal")):
    e = u.empresa
    ciclo = "anual" if ciclo == "anual" else "mensal"
    escolhido = next((p for p in _planos(db) if p.id == plano), None)
    if escolhido:   # plano de um grupo: vale a faixa que cabe e fica mais barata
        mensal, _, faixa = crescimento.calcular(db, e, escolhido)
        escolhido = faixa or escolhido
    valor = crescimento.valor_do_ciclo(mensal, ciclo) if escolhido else 0
    dias = 365 if ciclo == "anual" else DIAS_AVULSO
    erro = None
    if not asaas.configurado():
        erro = "O pagamento online não está disponível. Fale com o suporte."
    elif situacao(e) == "cortesia":
        erro = "Sua conta é cortesia: não há o que pagar."
    elif not escolhido or escolhido.gratis:
        erro = "Escolha um plano."
    elif valor < asaas.VALOR_MINIMO_CENTAVOS:
        erro = "Este plano não tem valor para cobrança online. Fale com o suporte."
    elif (limite_do_plano(escolhido)
          and funcionarios_ativos(db, e.id) > limite_do_plano(escolhido)):
        erro = (f"O plano {escolhido.nome} permite até {limite_do_plano(escolhido)} "
                f"funcionários e você tem {funcionarios_ativos(db, e.id)} ativos. Escolha um "
                "plano maior ou desative alguém em Equipe.")
    elif e.asaas_assinatura_id:
        erro = ("A cobrança automática já está ativa. Para mudar de plano, "
                "encerre a cobrança automática e assine de novo.")
    elif not e.asaas_cliente_id and not asaas.cpf_cnpj_valido(cpf_cnpj or e.cpf_cnpj):
        erro = "Informe um CPF ou CNPJ válido para emitir a cobrança."
    elif not dentro_do_limite(f"pagar:{e.id}", 10, 3600):
        erro = "Muitas tentativas de pagamento. Aguarde um pouco."
    if erro:
        avisar(request, erro, "erro")
        return ir("/painel/assinatura")

    retorno = f"{base_url(request)}/painel/assinatura"
    nome_plat = plataforma()["nome"]
    try:
        if not e.asaas_cliente_id:
            doc = asaas.digitos(cpf_cnpj or e.cpf_cnpj)
            e.asaas_cliente_id = asaas.criar_cliente(e.nome, u.email, doc, f"{REF}:{e.id}")
            e.cpf_cnpj = doc
            db.commit()

        if modo == "avulso":
            # Reaproveita a cobrança de hoje para o mesmo plano, em vez de gerar outra.
            igual = db.scalar(select(Pagamento).where(
                Pagamento.empresa_id == e.id, Pagamento.tipo == "avulso",
                Pagamento.status == "pendente", Pagamento.plano_id == escolhido.id,
                Pagamento.valor_centavos == valor, Pagamento.dias == dias,
                Pagamento.criado_em >= banco.agora() - timedelta(hours=12)))
            if igual and igual.fatura_url:
                return ir(igual.fatura_url)
            pay = asaas.criar_cobranca(
                e.asaas_cliente_id, valor,
                f"{nome_plat} · {escolhido.nome} · " + ("12 meses" if ciclo == "anual" else f"{DIAS_AVULSO} dias"),
                f"{REF}:{e.id}", retorno)
            tipo = "avulso"
        else:
            # Quem ainda tem dias (teste ou pagos) só começa a ser cobrado quando acabarem.
            v = validade(e) if situacao(e) in ("ativa", "teste") else None
            primeiro = None
            if v and v > banco.agora() + timedelta(days=2):
                primeiro = (v + timedelta(hours=FUSO_HORAS)).date().isoformat()
            extra = {"ciclo": "YEARLY"} if ciclo == "anual" else {}
            ass = asaas.criar_assinatura(
                e.asaas_cliente_id, valor,
                f"{nome_plat} · {escolhido.nome} · " + ("anuidade" if ciclo == "anual" else "mensalidade"),
                f"{REF}:{e.id}", primeiro, retorno, **extra)
            pay = ass.get("primeira")
            if not pay or not pay.get("invoiceUrl"):
                asaas.cancelar_assinatura(ass["id"])
                raise asaas.AsaasErro("O Asaas não gerou a primeira cobrança. Tente de novo.")
            e.asaas_assinatura_id, e.ciclo = ass["id"], ciclo
            e.valor_assinatura_centavos, e.valor_conferido_em = valor, banco.agora()
            tipo = "assinatura"
    except asaas.AsaasErro as ex:
        db.commit()
        avisar(request, f"Não foi possível gerar a cobrança: {ex}", "erro")
        return ir("/painel/assinatura")

    db.add(Pagamento(
        empresa_id=e.id, tipo=tipo, status="pendente", asaas_id=pay["id"],
        fatura_url=(pay.get("invoiceUrl") or "")[:300], plano_id=escolhido.id,
        plano_nome=escolhido.nome, valor_centavos=valor,
        dias=dias, forma="outro", registrado_por=u.email or u.nome,
        criado_em=banco.agora()))
    db.commit()
    return ir(pay["invoiceUrl"])


def _encerrar(request: Request, db: Session, e: Empresa) -> bool:
    if not e.asaas_assinatura_id:
        avisar(request, "Não há cobrança automática ativa.", "erro")
        return False
    try:
        asaas.cancelar_assinatura(e.asaas_assinatura_id)
    except asaas.AsaasErro as ex:
        avisar(request, f"Não foi possível encerrar no Asaas: {ex}", "erro")
        return False
    assinatura_encerrada(db, e)
    return True


@rota.post("/painel/assinatura/cancelar", dependencies=CSRF)
def cancelar(request: Request, u: Usuario = Depends(gestor_logado),
             db: Session = Depends(get_db)):
    if _encerrar(request, db, u.empresa):
        avisar(request, "Cobrança automática encerrada. O acesso continua até a data já paga.")
    return ir("/painel/assinatura")


@rota.post("/master/empresas/{empresa_id}/cancelar-assinatura", dependencies=CSRF)
def master_cancelar(empresa_id: int, request: Request,
                    u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    e = db.get(Empresa, empresa_id)
    if not e:
        return ir("/master")
    if _encerrar(request, db, e):
        avisar(request, "Cobrança automática encerrada no Asaas.")
    return ir(f"/master/empresas/{e.id}")


# ----------------------------------------------------------------- webhook
@rota.post("/asaas/webhook")
async def webhook(request: Request, tarefas_bg: BackgroundTasks, db: Session = Depends(get_db)):
    token = asaas.token_webhook()
    recebido = request.headers.get("asaas-access-token", "")
    if len(token) < 32 or not hmac.compare_digest(recebido.encode(), token.encode()):
        raise HTTPException(401, "Token inválido")
    try:
        ev = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON inválido")
    eid, tipo = str(ev.get("id") or "")[:80], str(ev.get("event") or "")
    if eid and db.get(AsaasEvento, eid):
        return {"status": "ok"}
    pay, ass = ev.get("payment") or {}, ev.get("subscription") or {}
    if tipo in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED") and pay.get("id"):
        ja_pago = db.scalar(select(Pagamento.id).where(Pagamento.asaas_id == pay["id"],
                                                       Pagamento.status == "pago"))
        if confirmar_pagamento(db, pay) and not ja_pago:
            e = _empresa_da_cobranca(db, pay, db.scalar(select(Pagamento).where(Pagamento.asaas_id == pay["id"])))
            if e:   # aviso ao dono da plataforma
                tarefas_bg.add_task(avisos_master.pagamento, e.id, round(float(pay.get("value") or 0) * 100), True)
    elif tipo == "PAYMENT_OVERDUE" and pay.get("id"):
        marcar_vencido(db, pay)
        e = _empresa_da_cobranca(db, pay, db.scalar(select(Pagamento).where(Pagamento.asaas_id == pay["id"])))
        if e:
            tarefas_bg.add_task(avisos_master.pagamento, e.id, round(float(pay.get("value") or 0) * 100), False)
    elif tipo in ("PAYMENT_DELETED", "PAYMENT_REFUNDED") and pay.get("id"):
        reg = db.scalar(select(Pagamento).where(Pagamento.asaas_id == pay["id"]))
        if reg and tipo == "PAYMENT_REFUNDED":
            reg.status = "estornado"
            from . import parceiros
            parceiros.estornar(db, reg)
        elif reg and reg.status != "pago":
            reg.status = "cancelado"
        db.commit()
    elif tipo in ("SUBSCRIPTION_DELETED", "SUBSCRIPTION_INACTIVATED") and ass.get("id"):
        e = db.scalar(select(Empresa).where(Empresa.asaas_assinatura_id == ass["id"]))
        if e:
            assinatura_encerrada(db, e)
            tarefas_bg.add_task(avisos_master.cancelada, e.id)
    if eid:
        db.add(AsaasEvento(id=eid, evento=tipo[:40], criado_em=banco.agora()))
        db.commit()
    return {"status": "ok"}
