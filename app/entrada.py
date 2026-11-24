"""Telas de Entrar e Criar conta: aparência trocada no Painel Master (cores, textos e imagens)
e entrada com a conta do Google (OAuth). O Google só aparece quando GOOGLE_CLIENT_ID e
GOOGLE_CLIENT_SECRET estão no .env do servidor."""
import re
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import avisos_master, legal, master
from . import db as banco
from .core import (admin_logado, avisar, base_url, dentro_do_limite, exigir_csrf, google_credenciais,
                   google_ligado, ip, ir, plataforma, plataforma_mudou, render)
from .db import Config, Plataforma, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

PASTA = banco.PASTA_DADOS / "entrada"
NOME_IMG = re.compile(r"^[0-9a-f]{16}\.(jpg|png|webp)$")
TIPOS = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}
MAX_IMAGENS, MAX_BYTES = 6, 4 * 1024 * 1024
COR = re.compile(r"^#[0-9a-fA-F]{6}$")


def imagens(plat: dict | None = None) -> list[str]:
    plat = plat or plataforma()
    return [n for n in (plat.get("entrada_imagens") or "").split(",") if NOME_IMG.match(n)]


def contexto() -> dict:
    """O que as telas de entrada usam (vai para o render de entrar/cadastro)."""
    p = plataforma()
    return {"ent_cor1": p.get("entrada_cor1") or "#4f46e5", "ent_cor2": p.get("entrada_cor2") or "#0ea5e9",
            "ent_titulo": p.get("entrada_titulo") or "", "ent_texto": p.get("entrada_texto") or "",
            "ent_imagens": imagens(p), "google_ok": google_configurado()}


# ------------------------------------------------------------------ imagens públicas
@rota.get("/entrada-img/{nome}")
def imagem(nome: str):
    if not NOME_IMG.match(nome) or not (PASTA / nome).is_file():
        return Response(status_code=404)
    return FileResponse(PASTA / nome, headers={"Cache-Control": "public,max-age=604800"})


# ------------------------------------------------------------------ Painel Master
@rota.get("/master/entrada")
def tela(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/entrada.html", u=u, e=u.empresa, aba="master", sub="entrada",
                  p=db.get(Plataforma, 1), imgs=imagens(), MAX_IMAGENS=MAX_IMAGENS,
                  google_ok=google_configurado(), retorno_google=base_url(request) + "/entrar/google/volta",
                  google_id=google_credenciais()[0],
                  google_salvo_aqui=bool(db.get(Config, "google_client_secret")))


@rota.post("/master/entrada", dependencies=CSRF)
def salvar(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db),
           cor1: str = Form(""), cor2: str = Form(""), titulo: str = Form(""), texto: str = Form("")):
    if not COR.match(cor1) or not COR.match(cor2):
        avisar(request, "Escolha as duas cores.", "erro")
        return ir("/master/entrada")
    p = db.get(Plataforma, 1)
    p.entrada_cor1, p.entrada_cor2 = cor1.lower(), cor2.lower()
    p.entrada_titulo, p.entrada_texto = " ".join(titulo.split())[:120], " ".join(texto.split())[:300]
    db.commit()
    plataforma_mudou()
    avisar(request, "Tela de entrada atualizada.")
    return ir("/master/entrada")


@rota.post("/master/entrada/imagem", dependencies=CSRF)
def enviar_imagem(request: Request, arquivo: UploadFile = File(...), u: Usuario = Depends(admin_logado),
                  db: Session = Depends(get_db)):
    atuais = imagens()
    if len(atuais) >= MAX_IMAGENS:
        avisar(request, f"Use no máximo {MAX_IMAGENS} imagens. Remova uma antes de enviar outra.", "erro")
        return ir("/master/entrada")
    dados = arquivo.file.read(MAX_BYTES + 1)
    ext = next((x for sig, x in TIPOS.items() if dados.startswith(sig)), None)
    if ext == "webp" and dados[8:12] != b"WEBP":
        ext = None
    if not dados or len(dados) > MAX_BYTES or not ext:
        avisar(request, "A imagem precisa ser JPG, PNG ou WebP de até 4 MB.", "erro")
        return ir("/master/entrada")
    PASTA.mkdir(parents=True, exist_ok=True)
    nome = f"{secrets.token_hex(8)}.{ext}"
    (PASTA / nome).write_bytes(dados)
    p = db.get(Plataforma, 1)
    p.entrada_imagens = ",".join(atuais + [nome])
    db.commit()
    plataforma_mudou()
    avisar(request, "Imagem enviada. Com mais de uma, elas se revezam na tela de entrada.")
    return ir("/master/entrada")


@rota.post("/master/entrada/imagem/{nome}", dependencies=CSRF)
def mexer_imagem(request: Request, nome: str, acao: str = Form("remover"),
                 u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    lista = imagens()
    if nome in lista:
        i = lista.index(nome)
        if acao == "remover":
            lista.pop(i)
            (PASTA / nome).unlink(missing_ok=True)
            avisar(request, "Imagem removida.")
        elif acao == "primeira" and i:
            lista.insert(0, lista.pop(i))
            avisar(request, "Esta imagem agora aparece primeiro.")
        p = db.get(Plataforma, 1)
        p.entrada_imagens = ",".join(lista)
        db.commit()
        plataforma_mudou()
    return ir("/master/entrada")


# ------------------------------------------------------------------ Google
ID_GOOGLE = re.compile(r"^[0-9]+-[0-9a-z]+\.apps\.googleusercontent\.com$")


@rota.post("/master/entrada/google", dependencies=CSRF)
def google_salvar(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db),
                  client_id: str = Form(""), client_secret: str = Form(""), acao: str = Form("salvar")):
    def guardar(chave, valor):
        c = db.get(Config, chave)
        if valor:
            if c:
                c.valor = valor
            else:
                db.add(Config(chave=chave, valor=valor))
        elif c:
            db.delete(c)
    if acao == "desligar":
        guardar("google_client_id", "")
        guardar("google_client_secret", "")
        db.commit()
        plataforma_mudou()
        avisar(request, "Entrada com o Google desligada.")
        return ir("/master/entrada#google")
    client_id, client_secret = "".join(client_id.split()), "".join(client_secret.split())
    atual = db.get(Config, "google_client_secret")
    if not ID_GOOGLE.match(client_id):
        avisar(request, "O ID do cliente deve terminar em .apps.googleusercontent.com. Confira se copiou inteiro.", "erro")
    elif not client_secret and not atual:
        avisar(request, "Cole também a chave secreta do cliente (começa com GOCSPX-).", "erro")
    elif client_secret and not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", client_secret):
        avisar(request, "A chave secreta parece incompleta. Copie de novo no Google Cloud.", "erro")
    else:
        guardar("google_client_id", client_id)
        if client_secret:   # vazio = mantém a chave já salva
            guardar("google_client_secret", client_secret)
        db.commit()
        plataforma_mudou()
        avisar(request, "Google ligado. O botão “Continuar com o Google” já aparece nas telas de entrada.")
    return ir("/master/entrada#google")


GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USER = "https://openidconnect.googleapis.com/v1/userinfo"


def google_configurado() -> bool:
    return google_ligado()


def _retorno(request: Request) -> str:
    return base_url(request) + "/entrar/google/volta"


@rota.get("/entrar/google")
def google_ir(request: Request, modo: str = "entrar", plano: int = 0):
    if not google_configurado():
        avisar(request, "A entrada com o Google ainda não está disponível.", "erro")
        return ir("/entrar")
    estado = secrets.token_urlsafe(24)
    request.session["google_estado"] = estado
    request.session["google_modo"] = "cadastro" if modo == "cadastro" else "entrar"
    request.session["google_plano"] = int(plano or 0)
    q = {"client_id": google_credenciais()[0], "redirect_uri": _retorno(request),
         "response_type": "code", "scope": "openid email profile", "state": estado,
         "prompt": "select_account"}
    return ir(f"{GOOGLE_AUTH}?{urlencode(q)}")


def _perfil_google(request: Request, code: str) -> dict | None:
    """Troca o código pelo perfil (e-mail confirmado, nome, id). None se algo falhar."""
    try:
        with httpx.Client(timeout=10) as c:
            t = c.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": google_credenciais()[0],
                "client_secret": google_credenciais()[1],
                "redirect_uri": _retorno(request), "grant_type": "authorization_code"})
            if t.status_code != 200:
                return None
            p = c.get(GOOGLE_USER, headers={"Authorization": f"Bearer {t.json()['access_token']}"})
            if p.status_code != 200:
                return None
            return p.json()
    except (httpx.HTTPError, KeyError, ValueError):
        return None


@rota.get("/entrar/google/volta")
def google_volta(request: Request, code: str = "", state: str = "", error: str = "",
                 db: Session = Depends(get_db)):
    esperado = request.session.pop("google_estado", None)
    modo = request.session.pop("google_modo", "entrar")
    if error:   # a pessoa cancelou na tela do Google
        return ir("/cadastro" if modo == "cadastro" else "/entrar")
    if not google_configurado() or not esperado or not secrets.compare_digest(esperado, state or "") or not code:
        avisar(request, "Não deu para entrar com o Google. Tente de novo.", "erro")
        return ir("/entrar")
    if not dentro_do_limite(f"google:{ip(request)}", 20, 600):
        avisar(request, "Muitas tentativas. Aguarde alguns minutos.", "erro")
        return ir("/entrar")
    perfil = _perfil_google(request, code)
    if not perfil or not perfil.get("email") or not perfil.get("email_verified") or not perfil.get("sub"):
        avisar(request, "O Google não confirmou o seu e-mail. Tente de novo ou entre com a senha.", "erro")
        return ir("/entrar")
    email, sub = perfil["email"].strip().lower(), str(perfil["sub"])
    u = db.scalar(select(Usuario).where(Usuario.google_sub == sub)) or \
        db.scalar(select(Usuario).where(Usuario.email == email))
    if u:
        if not u.ativo or u.conta_excluida_em:
            avisar(request, "Este acesso está desativado. Fale com o seu gestor.", "erro")
            return ir("/entrar")
        if not u.empresa.ativo:
            avisar(request, "Esta conta está bloqueada. Fale com o suporte.", "erro")
            return ir("/entrar")
        if u.google_sub != sub:
            u.google_sub = sub
            db.commit()
        request.session.pop("master_uid", None)
        request.session["uid"] = u.id
        return ir("/painel" if u.gestor else "/app")
    # Ninguém com este e-mail: segue para criar a conta da empresa (se o cadastro estiver aberto).
    if not plataforma()["cadastro_aberto"]:
        avisar(request, "Não encontramos uma conta com este e-mail. Peça o acesso ao seu gestor.", "erro")
        return ir("/entrar")
    request.session["google_novo"] = {"email": email, "sub": sub,
                                      "nome": (perfil.get("name") or "").strip()[:120]}
    plano = request.session.pop("google_plano", 0)
    return ir("/cadastro?google=1" + (f"&plano={plano}" if plano else ""))


def google_pendente(request: Request) -> dict | None:
    g = request.session.get("google_novo")
    return g if isinstance(g, dict) and g.get("email") and g.get("sub") else None


@rota.post("/cadastro/google", dependencies=CSRF)
def cadastro_google(request: Request, tarefas_bg: BackgroundTasks, empresa: str = Form(""),
                    nome: str = Form(""), plano: int = Form(0), aceito: str = Form(""),
                    cupom: str = Form(""), contador: str = Form(""), db: Session = Depends(get_db)):
    g = google_pendente(request)
    if not g or not plataforma()["cadastro_aberto"]:
        return ir("/cadastro")
    empresa, nome = " ".join(empresa.split())[:120], " ".join(nome.split())[:120]
    erro = None
    if not empresa or not nome:
        erro = "Informe o nome da empresa e o seu nome."
    elif db.scalar(select(Usuario.id).where(Usuario.email == g["email"]).execution_options(alcance_livre=True)):
        erro = "Já existe uma conta com este e-mail."
    elif not aceito:
        erro = "Para criar a conta, aceite os Termos de Uso e a Política de Privacidade."
    if erro:
        return render(request, "cadastro.html", 400, erro=erro, plano=db.get(master.Plano, plano),
                      v={"empresa": empresa, "nome": nome, "email": g["email"]}, google=g, **contexto())
    u = master.criar_empresa(db, empresa, nome, g["email"], secrets.token_urlsafe(32), plano)
    u.google_sub = g["sub"]
    legal.registrar_aceite(u)
    db.commit()
    from . import crescimento
    crescimento.ao_criar_empresa(request, db, u, cupom, bool(contador))
    request.session.pop("google_novo", None)
    tarefas_bg.add_task(avisos_master.nova_empresa, u.empresa_id)
    request.session["uid"] = u.id
    avisar(request, "Conta criada. Responda 7 perguntas rápidas e deixamos tudo pronto.")
    return ir("/painel/comecar")
