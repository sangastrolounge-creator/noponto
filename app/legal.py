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
VERSAO = "2026-11-19"
IDIOMAS = ("pt",)


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
    }


def registrar_aceite(u) -> None:
    u.termos_em = banco.agora()
    u.termos_versao = VERSAO


def _pagina(request: Request, doc: str):
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
