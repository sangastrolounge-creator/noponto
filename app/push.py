"""Avisos push (Web Push) para os aparelhos dos gestores. Opcional: sem a biblioteca
pywebpush o app funciona igual, só sem a notificação no aparelho."""
import base64
import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import i18n
from .db import Alerta, Config, PushSub, SessionLocal, Usuario

log = logging.getLogger("turno")

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import WebPushException, webpush
    DISPONIVEL = True
except Exception:  # biblioteca ausente
    DISPONIVEL = False

CONTATO = os.environ.get("PUSH_CONTATO", "").strip() or "mailto:suporte@exemplo.com.br"
TITULOS = {
    "fora_turno": "Fora da área durante o turno", "sem_confirmacao": "Turno não confirmado", "descanso": "Pausas não liberadas", "menor": "Menor de idade: limite de horário", "sem_ponto": "Ponto não batido", "atraso": "Atraso", "fora_area": "Fora da área",
    "sem_saida": "Saída não registrada", "sem_intervalo": "Intervalo não batido", "saida_antecipada": "Saída antecipada",
    "sem_escala": "Ponto fora da escala", "aparelho": "Aparelho não autorizado",
    "ajuste": "Pedido de ajuste de ponto", "ausencia": "Atestado para conferir",
    "folha": "Folha de ponto contestada",
    "perto_extra": "Perto da hora extra", "hora_extra": "Hora extra",
}
# Tipo de alerta -> parte do painel: o gerente só recebe o aviso se tiver essa permissão.
PERMISSAO_DO_TIPO = {"aparelho": "equipe", "ausencia": "ausencias", "folha": "fechamento"}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def chaves(db: Session) -> tuple[str, str] | None:
    """Par de chaves VAPID (privada, pública), criado na primeira vez."""
    if not DISPONIVEL:
        return None
    priv, pub = db.get(Config, "vapid_priv"), db.get(Config, "vapid_pub")
    if priv and pub:
        return priv.valor, pub.valor
    k = ec.generate_private_key(ec.SECP256R1())
    priv_v = _b64(k.private_numbers().private_value.to_bytes(32, "big"))
    pub_v = _b64(k.public_key().public_bytes(serialization.Encoding.X962,
                                             serialization.PublicFormat.UncompressedPoint))
    db.merge(Config(chave="vapid_priv", valor=priv_v))
    db.merge(Config(chave="vapid_pub", valor=pub_v))
    db.commit()
    return priv_v, pub_v


def chave_publica(db: Session) -> str:
    c = chaves(db)
    return c[1] if c else ""


def enviar(db: Session, empresa_id: int, titulo: str, texto: str, url: str = "/painel",
           permissao: str | None = None) -> int:
    """Manda a notificação aos aparelhos dos gestores da empresa. Com `permissao`, só o dono e
    os gerentes que têm acesso àquela parte do painel."""
    subs = list(db.scalars(select(PushSub).join(Usuario).where(
        Usuario.empresa_id == empresa_id, Usuario.papel == "gestor", Usuario.ativo.is_(True))))
    if permissao:
        from .core import pode
        subs = [s for s in subs if pode(db.get(Usuario, s.usuario_id), permissao)]
    return _mandar(db, subs, titulo, texto, url)


def enviar_usuario(db: Session, usuario_id: int, titulo: str, texto: str, url: str = "/app",
                   traduzir: bool = True) -> int:
    """Manda a notificação aos aparelhos de uma pessoa (lembrete de ponto, resposta de ajuste)."""
    return _mandar(db, list(db.scalars(select(PushSub).where(PushSub.usuario_id == usuario_id))),
                   titulo, texto, url, traduzir)


def _mandar(db: Session, subs: list, titulo: str, texto: str, url: str, traduzir: bool = True) -> int:
    c = chaves(db)
    if not c or not subs:
        return 0
    # O aviso sai no idioma padrão da plataforma (o app não guarda o idioma de cada pessoa).
    if traduzir:
        with i18n.no_idioma("pt"):
            titulo, texto = i18n._(titulo), i18n._(texto)
    enviados = 0
    for s in subs:
        try:
            webpush(subscription_info={"endpoint": s.endpoint,
                                       "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                    data=json.dumps({"titulo": titulo, "texto": texto, "url": url}),
                    vapid_private_key=c[0], vapid_claims={"sub": CONTATO}, ttl=3600,
                    timeout=10)
            enviados += 1
        except WebPushException as ex:
            codigo = getattr(ex.response, "status_code", None)
            if codigo in (404, 410):  # aparelho cancelou a inscrição
                db.delete(s)
            else:
                log.warning("Falha no aviso push (%s): %s", codigo, ex)
        except Exception:
            log.exception("Falha no aviso push")
    db.commit()
    return enviados


def avisar_alertas(ids: list[int]):
    """Chamado em segundo plano depois que alertas são criados."""
    if not DISPONIVEL or not ids:
        return
    with SessionLocal() as db:
        for a in db.scalars(select(Alerta).where(Alerta.id.in_(ids))):
            enviar(db, a.empresa_id, TITULOS.get(a.tipo, "Aviso"), a.texto,
                   permissao=PERMISSAO_DO_TIPO.get(a.tipo, "ponto"))


def avisar_usuarios(lembretes: list[tuple]):
    """Lembretes para funcionários: lista de (usuario_id, título, texto[, url])."""
    if not DISPONIVEL or not lembretes:
        return
    with SessionLocal() as db:
        for uid, titulo, texto, *url in lembretes:
            enviar_usuario(db, uid, titulo, texto, url[0] if url else "/app")


def avisar_gestores(empresa_id: int, titulo: str, texto: str, url: str = "/painel",
                    permissao: str | None = None):
    """Em segundo plano: avisa os gestores da empresa (com `permissao`, só quem tem acesso)."""
    if not DISPONIVEL:
        return
    with SessionLocal() as db:
        enviar(db, empresa_id, titulo, texto, url, permissao)
