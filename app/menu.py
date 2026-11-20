"""Contadores do menu do painel: o que pede a atenção do gestor em cada parte."""
from sqlalchemy import func, select

from .core import pode
from .db import Ajuste, Ausencia, Oferta, SessionLocal


def contadores(u) -> dict[str, int]:
    """{chave do menu: quantidade}. Só o que a pessoa pode ver."""
    if not u or not u.gestor:
        return {}
    e, n = u.empresa, {}
    try:
        with SessionLocal() as db:
            if pode(u, "ponto"):
                from .ponto_problemas import problemas
                n["ponto"] = len(problemas(db, e)) + (db.scalar(select(func.count()).select_from(Ajuste).where(
                    Ajuste.empresa_id == e.id, Ajuste.situacao == "pendente")) or 0)
            if pode(u, "ausencias"):
                n["ausencias"] = db.scalar(select(func.count()).select_from(Ausencia).where(
                    Ausencia.empresa_id == e.id, Ausencia.situacao == "pendente")) or 0
            if pode(u, "escala"):
                n["escala"] = db.scalar(select(func.count()).select_from(Oferta).where(
                    Oferta.empresa_id == e.id, Oferta.situacao == "pedida")) or 0
    except Exception:   # o menu nunca derruba a página
        return {}
    return {k: v for k, v in n.items() if v}
