"""Termos de Uso e Política de Privacidade.

Texto em português, segundo a lei brasileira (LGPD, Marco Civil da Internet, CDC); vale para
todos os idiomas da tela. Os dados da empresa dona da plataforma (razão social e CNPJ, endereço,
e-mail do encarregado, foro e país dos servidores) vêm de Master -> Plataforma.

Ao mudar o texto de forma relevante, atualize VERSAO: quem aceitar depois fica registrado
com a nova versão (usuarios.termos_versao)."""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from . import db as banco
from .core import plataforma, render

rota = APIRouter()
VERSAO = "2026-11-25"
IDIOMAS = ("pt",)
# EUA (dataworke.com e empresas americanas): textos da empresa americana; o inglês é o que vale,
# espanhol e português são traduções de cortesia. Ficam em templates/legal/us/.
VERSAO_EUA = "2026-11-19"
IDIOMAS_EUA = ("en", "es", "pt")


def dados() -> dict:
    p = plataforma()
    marca = p["nome"]
    return {
        "marca": marca,
        "empresa": (p.get("razao_social") or "").strip() or marca,
        "endereco": (p.get("endereco") or "").strip(),
        "email": (p.get("email_legal") or "").strip(),
        "foro": (p.get("estado_lei") or "").strip(),   # cidade/UF do foro (coluna antiga: estado_lei)
        "pais": (p.get("pais_servidores") or "").strip() or "Brasil",
        "versao": date.fromisoformat(VERSAO),
        "rep": _rep(),
    }


def dados_eua() -> dict:
    from .core import nome_eua
    p = plataforma()
    marca = nome_eua(p["nome"])
    return {
        "marca": marca,
        "empresa": (p.get("eua_razao_social") or "").strip() or marca,
        "endereco": (p.get("eua_endereco") or "").strip(),
        "email": (p.get("eua_email_legal") or "").strip() or (p.get("email_legal") or "").strip(),
        "estado": (p.get("eua_estado_lei") or "").strip() or "Wyoming",
        "foro": (p.get("eua_estado_lei") or "").strip() or "Wyoming",
        "pais": (p.get("pais_servidores") or "").strip() or "Brazil",
        "versao": date.fromisoformat(VERSAO_EUA),
        "rep": {"completo": False, "inpi": ""},
    }


def pais_da_pagina(request: Request) -> str:
    """Quem está logado segue o país da empresa; o visitante, o do endereço do site."""
    from . import paises
    from .db import SessionLocal, Usuario
    uid = request.session.get("uid")
    if uid:
        with SessionLocal() as db:
            u = db.get(Usuario, uid)
            if u and u.empresa:
                return u.empresa.pais or "BR"
    return paises.pais_do_host(request)


def _rep() -> dict:
    """Situação do REP-P (Portaria 671) para o texto dos Termos de Uso."""
    from . import rep
    from .db import SessionLocal
    with SessionLocal() as db:
        s = rep.situacao(db)
    return {"completo": s["completo"], "inpi": rep.digitos(s["config"]["inpi"])}


def registrar_aceite(u, pais: str = "BR") -> None:
    u.termos_em = banco.agora()
    u.termos_versao = VERSAO_EUA if pais == "US" else VERSAO


def _pagina(request: Request, doc: str):
    if pais_da_pagina(request) == "US":
        from . import i18n
        cod = i18n.idioma() if i18n.idioma() in IDIOMAS_EUA else "en"
        return render(request, f"legal/us/{doc}_{cod}.html", L=dados_eua(), doc=doc, cod=cod, pais_tela="US")
    return render(request, f"legal/{doc}_pt.html", L=dados(), doc=doc, cod="pt")


@rota.get("/terms")
def termos(request: Request):
    return _pagina(request, "terms")


@rota.get("/privacy")
def privacidade(request: Request):
    return _pagina(request, "privacy")


@rota.get("/termos")
def termos_pt():
    return RedirectResponse("/terms", 301)


@rota.get("/privacidade")
def privacidade_pt():
    return RedirectResponse("/privacy", 301)
