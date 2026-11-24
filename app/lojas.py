"""Apps nas lojas (Google Play e, depois, App Store).

O app Android é uma Trusted Web Activity: abre o próprio site em tela cheia. Para o Android confiar
que o app e o site são do mesmo dono, o site publica /.well-known/assetlinks.json com o nome do
pacote e a impressão digital SHA-256 da chave que assina o app (a do Google Play App Signing e,
se quiser testar o pacote antes, a da chave de envio). Os valores são públicos e ficam em
Master -> Lojas de apps."""
import json
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import regras
from .core import admin_logado, avisar, exigir_csrf, ir, render
from .db import Config, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
CHAVE = "lojas"
PACOTE_PADRAO = "br.com.dataworke.app"
PACOTE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def ler(db: Session) -> dict:
    c = db.get(Config, CHAVE)
    try:
        d = json.loads(c.valor) if c and c.valor else {}
    except ValueError:
        d = {}
    return {"android_pacote": d.get("android_pacote") or PACOTE_PADRAO,
            "android_sha256": list(d.get("android_sha256") or []),
            "play_url": d.get("play_url") or ""}


def digital(txt: str) -> str | None:
    """Impressão digital SHA-256 no formato do assetlinks: AB:CD:... (32 pares)."""
    h = re.sub(r"[^0-9A-Fa-f]", "", txt or "").upper()
    if len(h) != 64:
        return None
    return ":".join(h[i:i + 2] for i in range(0, 64, 2))


@rota.get("/.well-known/assetlinks.json")
def assetlinks():
    with SessionLocal() as db:
        d = ler(db)
    corpo = [] if not d["android_sha256"] else [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": d["android_pacote"],
                   "sha256_cert_fingerprints": d["android_sha256"]}}]
    return JSONResponse(corpo, headers={"Cache-Control": "public, max-age=300"})


@rota.get("/master/lojas")
def tela(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/lojas.html", u=u, e=u.empresa, aba="master", sub="lojas", d=ler(db))


@rota.post("/master/lojas", dependencies=CSRF)
def salvar(request: Request, android_pacote: str = Form(""), android_sha256: str = Form(""),
           play_url: str = Form(""), u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    pacote = android_pacote.strip().lower() or PACOTE_PADRAO
    if not PACOTE_RE.match(pacote) or len(pacote) > 100:
        avisar(request, "Nome do pacote inválido. Use algo como br.com.dataworke.app.", "erro")
        return ir("/master/lojas")
    linhas = [x for x in re.split(r"[\n,;]+", android_sha256) if x.strip()]
    digitais = [digital(x) for x in linhas]
    if any(x is None for x in digitais):
        avisar(request, "Impressão digital inválida: copie o SHA-256 do Play Console (32 pares como AB:CD:…).", "erro")
        return ir("/master/lojas")
    play_url = play_url.strip()
    if play_url and not play_url.startswith("https://play.google.com/"):
        avisar(request, "O link da loja precisa começar com https://play.google.com/.", "erro")
        return ir("/master/lojas")
    unicas = list(dict.fromkeys(digitais))[:5]
    db.merge(Config(chave=CHAVE, valor=json.dumps({"android_pacote": pacote, "android_sha256": unicas,
                                                   "play_url": play_url})))
    regras.registrar(db, u.empresa_id, u.nome, "apps nas lojas",
                     f"Android: {pacote}, {len(unicas)} impressão(ões) digital(is).")
    db.commit()
    avisar(request, "Dados das lojas salvos.")
    return ir("/master/lojas")
