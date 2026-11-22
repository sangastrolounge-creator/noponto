"""Cliente mínimo da API do Asaas: cliente, cobrança avulsa, assinatura mensal e webhook.

Mesmo desenho do cmv-app e do avalia-app. Sem ASAAS_API_KEY a cobrança automática fica desligada
e a plataforma segue com o pagamento registrado à mão pelo master.
"""
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger("turno.asaas")

# Produção: https://api.asaas.com/v3 · testes (sandbox): https://api-sandbox.asaas.com/v3
API_URL = (os.environ.get("ASAAS_API_URL") or "https://api.asaas.com/v3").rstrip("/")
PAGOS = ("CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH")
EVENTOS = ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_OVERDUE", "PAYMENT_DELETED",
           "PAYMENT_REFUNDED", "SUBSCRIPTION_INACTIVATED", "SUBSCRIPTION_DELETED"]
VALOR_MINIMO_CENTAVOS = 500  # regra do Asaas


class AsaasErro(Exception):
    def __init__(self, mensagem: str, status: int = 0):
        super().__init__(mensagem)
        self.status = status


def configurado() -> bool:
    return bool(os.environ.get("ASAAS_API_KEY"))


def token_webhook() -> str:
    return os.environ.get("ASAAS_WEBHOOK_TOKEN") or ""


def chamar(metodo: str, caminho: str, corpo: dict | None = None,
           params: dict | None = None) -> dict:
    chave = os.environ.get("ASAAS_API_KEY")
    if not chave:
        raise AsaasErro("Cobrança automática indisponível. Fale com o suporte.")
    try:
        r = httpx.request(metodo, f"{API_URL}{caminho}", json=corpo, params=params,
                          timeout=30, headers={"access_token": chave,
                                               "User-Agent": "turno-app/1.0"})
    except httpx.HTTPError as e:
        raise AsaasErro("Não foi possível falar com o Asaas agora. Tente de novo.") from e
    try:
        dados = r.json() if r.content else {}
    except ValueError:
        dados = {}
    if r.status_code >= 400:
        erros = dados.get("errors") if isinstance(dados, dict) else None
        msg = "; ".join(e.get("description", "") for e in erros or []
                        if e.get("description")) or f"Erro {r.status_code} no Asaas"
        log.warning("Asaas %s %s -> %s: %s", metodo, caminho, r.status_code, msg)
        raise AsaasErro(msg, r.status_code)
    return dados


# ---------------------------------------------------------------- CPF/CNPJ
def digitos(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


def _dv(d: str, pesos) -> int:
    r = sum(int(c) * p for c, p in zip(d, pesos)) % 11
    return 0 if r < 2 else 11 - r


def cpf_cnpj_valido(v: str | None) -> bool:
    d = digitos(v)
    if len(d) == 11 and len(set(d)) > 1:
        return (_dv(d[:9], range(10, 1, -1)) == int(d[9])
                and _dv(d[:10], range(11, 1, -1)) == int(d[10]))
    if len(d) == 14 and len(set(d)) > 1:
        p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        return _dv(d[:12], p1) == int(d[12]) and _dv(d[:13], [6] + p1) == int(d[13])
    return False


# ------------------------------------------------------------------- datas
def hoje() -> str:
    """Hoje no horário de Brasília, que é o que o Asaas usa nos vencimentos."""
    return datetime.now(timezone(timedelta(hours=-3))).date().isoformat()


def mais_um_mes(data_iso: str) -> datetime:
    """Mesmo dia do mês seguinte (ou o último dia dele), em UTC sem tzinfo:
    até quando uma mensalidade cobre."""
    d = date.fromisoformat(data_iso[:10])
    a, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    for dia in (d.day, 30, 29, 28):
        try:
            return datetime(a, m, dia, 23, 59)
        except ValueError:
            continue
    raise ValueError(data_iso)


def mais_meses(data_iso: str, meses: int) -> datetime:
    """Mesmo dia, `meses` depois (ou o último dia do mês), em UTC sem tzinfo."""
    d = date.fromisoformat(data_iso[:10])
    total = d.year * 12 + d.month - 1 + meses
    a, m = divmod(total, 12)
    for dia in (d.day, 30, 29, 28):
        try:
            return datetime(a, m + 1, dia, 23, 59)
        except ValueError:
            continue
    raise ValueError(data_iso)


def mais_um_ano(data_iso: str) -> datetime:
    return mais_meses(data_iso, 12)


# --------------------------------------------------------------- operações
def criar_cliente(nome: str, email: str, cpf_cnpj: str, ref: str) -> str:
    return chamar("POST", "/customers", {
        "name": nome[:100], "email": email, "cpfCnpj": digitos(cpf_cnpj),
        "externalReference": ref})["id"]


def _com_retorno(caminho: str, corpo: dict, url_retorno: str | None) -> dict:
    """O Asaas só aceita retorno automático para https do domínio cadastrado na
    conta; se recusar, cria a cobrança sem o retorno."""
    if (url_retorno or "").startswith("https://"):
        try:
            return chamar("POST", caminho, {**corpo, "callback": {
                "successUrl": url_retorno, "autoRedirect": True}})
        except AsaasErro as e:
            if e.status != 400:
                raise
            log.warning("Asaas recusou o retorno automático (%s); criando sem ele", e)
    return chamar("POST", caminho, corpo)


def criar_cobranca(cliente: str, centavos: int, descricao: str, ref: str,
                   url_retorno: str | None = None) -> dict:
    """Cobrança avulsa: o pagador escolhe Pix, boleto ou cartão na página do Asaas."""
    return _com_retorno("/payments", {
        "customer": cliente, "billingType": "UNDEFINED", "value": centavos / 100,
        "dueDate": hoje(), "description": descricao[:500],
        "externalReference": ref}, url_retorno)


def criar_assinatura(cliente: str, centavos: int, descricao: str, ref: str,
                     primeiro_vencimento: str | None = None,
                     url_retorno: str | None = None, ciclo: str = "MONTHLY") -> dict:
    """Assinatura mensal (ou anual, ciclo YEARLY). Devolve a assinatura com a primeira cobrança
    em 'primeira'."""
    ass = _com_retorno("/subscriptions", {
        "customer": cliente, "billingType": "UNDEFINED", "value": centavos / 100,
        "nextDueDate": primeiro_vencimento or hoje(), "cycle": ciclo,
        "description": descricao[:500], "externalReference": ref}, url_retorno)
    cobr = chamar("GET", f"/subscriptions/{ass['id']}/payments",
                  params={"limit": 10}).get("data") or []
    ass["primeira"] = min(cobr, key=lambda p: p.get("dueDate") or "") if cobr else None
    return ass


def mudar_valor_assinatura(assinatura_id: str, centavos: int):
    """Muda o valor das próximas cobranças da assinatura (as já geradas ficam como estão)."""
    chamar("PUT", f"/subscriptions/{assinatura_id}", {
        "value": centavos / 100, "updatePendingPayments": False})


def adiar_assinatura(assinatura_id: str, meses: int = 1) -> str:
    """Empurra o próximo vencimento da assinatura (mês grátis). Devolve a nova data."""
    ass = chamar("GET", f"/subscriptions/{assinatura_id}")
    proximo = ass.get("nextDueDate") or hoje()
    nova = mais_meses(proximo, meses).date().isoformat()
    chamar("PUT", f"/subscriptions/{assinatura_id}", {"nextDueDate": nova, "updatePendingPayments": False})
    return nova


def valor_assinatura(assinatura_id: str) -> int:
    return round(float(chamar("GET", f"/subscriptions/{assinatura_id}").get("value") or 0) * 100)


def consultar_cobranca(pagamento_id: str) -> dict:
    return chamar("GET", f"/payments/{pagamento_id}")


def cancelar_assinatura(assinatura_id: str):
    try:
        chamar("DELETE", f"/subscriptions/{assinatura_id}")
    except AsaasErro as e:
        if e.status != 404:
            raise


def garantir_webhook(url_publica: str, email: str, nome: str):
    """Cadastra no Asaas o aviso de pagamentos para este servidor, se não existir."""
    token = token_webhook()
    if not configurado() or not url_publica.startswith("https://") or len(token) < 32:
        return
    url = f"{url_publica}/asaas/webhook"
    try:
        avisos = chamar("GET", "/webhooks", params={"limit": 100}).get("data") or []
        if any(a.get("url") == url for a in avisos):
            return
        chamar("POST", "/webhooks", {
            "name": nome[:50], "url": url, "email": email, "enabled": True,
            "interrupted": False, "apiVersion": 3, "authToken": token,
            "sendType": "SEQUENTIALLY", "events": EVENTOS})
        log.info("Webhook do Asaas cadastrado: %s", url)
    except AsaasErro as e:
        log.error("Não foi possível cadastrar o webhook do Asaas: %s", e)
