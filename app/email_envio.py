"""Envio de e-mail por SMTP (ex.: a caixa da Hostinger, smtp.hostinger.com) e a recuperação de
senha dos gestores. Os dados do SMTP vêm do .env (SMTP_HOST, SMTP_PORT, SMTP_USER,
SMTP_PASSWORD, EMAIL_FROM) ou são colados pelo dono da plataforma em Master -> E-mail."""
import hashlib
import os
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from . import i18n, regras
from .core import (admin_logado, avisar, base_url, dentro_do_limite, email_valido, exigir_csrf,
                   gerar_hash, ip, ir, log, plataforma, render)
from .db import Config, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
CAMPOS = ("smtp_host", "smtp_port", "smtp_user", "smtp_senha", "smtp_remetente")
ENV = {"smtp_host": "SMTP_HOST", "smtp_port": "SMTP_PORT", "smtp_user": "SMTP_USER",
       "smtp_senha": "SMTP_PASSWORD", "smtp_remetente": "EMAIL_FROM"}
VALIDADE_LINK = timedelta(hours=1)


def dados() -> dict:
    with SessionLocal() as db:
        guardado = {c: (db.get(Config, c).valor if db.get(Config, c) else "") for c in CAMPOS}
    return {c: (os.environ.get(ENV[c], "").strip() or guardado[c]) for c in CAMPOS}


def configurado() -> bool:
    d = dados()
    return bool(d["smtp_host"] and d["smtp_user"] and d["smtp_senha"])


def _mandar(para: str, assunto: str, texto: str) -> None:
    """Envia de verdade (os testes trocam esta função). Erros sobem para quem chamou."""
    d = dados()
    porta = int(d["smtp_port"] or 465)
    msg = EmailMessage()
    remetente = d["smtp_remetente"] or d["smtp_user"]
    msg["From"] = formataddr((plataforma()["nome"], remetente))
    msg["To"] = para
    msg["Subject"] = assunto
    msg["Message-ID"] = make_msgid(domain=remetente.split("@")[-1])
    msg.set_content(texto)
    contexto = ssl.create_default_context()
    if porta == 465:
        with smtplib.SMTP_SSL(d["smtp_host"], porta, context=contexto, timeout=20) as s:
            s.login(d["smtp_user"], d["smtp_senha"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(d["smtp_host"], porta, timeout=20) as s:
            s.starttls(context=contexto)
            s.login(d["smtp_user"], d["smtp_senha"])
            s.send_message(msg)


def enviar(para: str, assunto: str, texto: str) -> bool:
    """Para usar em segundo plano: registra a falha no log em vez de derrubar a tela."""
    if not configurado():
        log.warning("E-mail não enviado (SMTP não configurado): %s", assunto)
        return False
    try:
        _mandar(para, assunto, texto)
        return True
    except Exception as ex:
        log.warning("Falha ao enviar e-mail (%s): %s", type(ex).__name__, ex)
        return False


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ esqueci a senha
@rota.get("/esqueci")
def esqueci_form(request: Request):
    return render(request, "esqueci.html", enviado=False, erro=None, email_ok=configurado())


@rota.post("/esqueci", dependencies=CSRF)
def esqueci(request: Request, tarefas_bg: BackgroundTasks, email: str = Form(""),
            db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not email_valido(email):
        return render(request, "esqueci.html", 400, enviado=False, email_ok=configurado(),
                      erro="Informe o e-mail com que você entra no painel.")
    if not dentro_do_limite(f"esqueci:{ip(request)}", 5, 3600):
        return render(request, "esqueci.html", 429, enviado=False, email_ok=configurado(),
                      erro="Muitas tentativas. Aguarde um pouco e tente de novo.")
    u = db.scalar(select(Usuario).where(Usuario.email == email))
    if u and u.ativo and u.empresa.ativo and dentro_do_limite(f"esqueci-u:{u.id}", 3, 3600):
        token = secrets.token_urlsafe(32)
        u.redefinir_hash, u.redefinir_ate = _hash(token), banco.agora() + VALIDADE_LINK
        db.commit()
        nome = plataforma()["nome"]
        link = f"{base_url(request)}/redefinir/{token}"
        assunto = i18n._("Redefinir a sua senha do {v1}", v1=nome)
        texto = i18n._("Olá, {v1}!\n\nRecebemos um pedido para redefinir a sua senha do {v2}. Para criar uma senha nova, abra este link (ele vale por 1 hora e só uma vez):\n\n{v3}\n\nSe não foi você, ignore este e-mail: a sua senha continua a mesma.",
                       v1=u.primeiro_nome, v2=nome, v3=link)
        tarefas_bg.add_task(enviar, u.email, assunto, texto)
    # A resposta é a mesma exista ou não a conta (não revela quem está cadastrado).
    return render(request, "esqueci.html", enviado=True, erro=None, email_ok=configurado())


def _pelo_token(db: Session, token: str) -> Usuario | None:
    if len(token) < 20:
        return None
    u = db.scalar(select(Usuario).where(Usuario.redefinir_hash == _hash(token)))
    if not u or not u.redefinir_ate or u.redefinir_ate < banco.agora() or not u.ativo or u.conta_excluida_em:
        return None
    return u


@rota.get("/redefinir/{token}")
def redefinir_form(request: Request, token: str, db: Session = Depends(get_db)):
    u = _pelo_token(db, token)
    return render(request, "redefinir.html", 200 if u else 404, u=u, token=token, erro=None)


@rota.post("/redefinir/{token}", dependencies=CSRF)
def redefinir(request: Request, token: str, senha: str = Form(""), senha2: str = Form(""),
              db: Session = Depends(get_db)):
    u = _pelo_token(db, token)
    if not u:
        return render(request, "redefinir.html", 404, u=None, token=token, erro=None)
    erro = None
    if len(senha) < 6:
        erro = "A senha precisa ter pelo menos 6 caracteres."
    elif senha != senha2:
        erro = "As duas senhas não são iguais."
    if erro:
        return render(request, "redefinir.html", 400, u=u, token=token, erro=erro)
    u.senha_hash, u.redefinir_hash, u.redefinir_ate = gerar_hash(senha), None, None
    u.convite_token = None
    regras.registrar(db, u.empresa_id, u.nome, "senha redefinida", f"{u.nome} criou uma senha nova pelo e-mail.")
    db.commit()
    avisar(request, "Senha nova criada. Entre com o seu e-mail e a senha nova.")
    return ir("/entrar")


# ------------------------------------------------------------------ Master -> E-mail
@rota.get("/master/email")
def master_email(request: Request, u: Usuario = Depends(admin_logado)):
    d = dados()
    return render(request, "master/email.html", u=u, e=u.empresa, aba="master", sub="email",
                  d={k: v for k, v in d.items() if k != "smtp_senha"}, tem_senha=bool(d["smtp_senha"]),
                  ok=configurado(), do_env=bool(os.environ.get("SMTP_HOST", "").strip()))


@rota.post("/master/email", dependencies=CSRF)
def master_email_salvar(request: Request, tarefas_bg: BackgroundTasks, host: str = Form(""),
                        porta: str = Form("465"), usuario: str = Form(""), senha: str = Form(""),
                        remetente: str = Form(""), acao: str = Form("salvar"),
                        u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if acao == "testar":
        if not configurado():
            avisar(request, "Salve os dados do e-mail antes de testar.", "erro")
            return ir("/master/email")
        try:
            _mandar(u.email, i18n._("Teste de e-mail do {v1}", v1=plataforma()["nome"]),
                    i18n._("Se você recebeu esta mensagem, o envio de e-mails do {v1} está funcionando.",
                           v1=plataforma()["nome"]))
        except Exception as ex:
            log.warning("Teste de e-mail falhou: %s", ex)
            avisar(request, f"O envio falhou ({type(ex).__name__}). Confira o servidor, a porta, o e-mail e a senha.", "erro")
            return ir("/master/email")
        avisar(request, f"E-mail de teste enviado para {u.email}. Confira a caixa de entrada (e o spam).")
        return ir("/master/email")
    host, usuario, remetente = host.strip(), usuario.strip().lower(), remetente.strip().lower()
    if not host or not email_valido(usuario) or (remetente and not email_valido(remetente)) \
            or not porta.strip().isdigit():
        avisar(request, "Confira o servidor, a porta e o e-mail da caixa.", "erro")
        return ir("/master/email")
    valores = {"smtp_host": host, "smtp_port": porta.strip(), "smtp_user": usuario,
               "smtp_remetente": remetente or usuario}
    if senha:
        valores["smtp_senha"] = senha
    for chave, valor in valores.items():
        c = db.get(Config, chave) or Config(chave=chave, valor="")
        c.valor = valor
        db.add(c)
    regras.registrar(db, u.empresa_id, u.nome, "e-mail configurado", f"{usuario} em {host}:{porta.strip()}.")
    db.commit()
    avisar(request, "Dados do e-mail salvos. Use “Enviar e-mail de teste” para conferir.")
    return ir("/master/email")
