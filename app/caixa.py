"""Caixa de e-mail da plataforma dentro do Master (ex.: contato@dataworke.com.br na Hostinger).

Lê a caixa de entrada por IMAP (imap.hostinger.com:993) a cada minuto, guarda uma cópia local para
listar e buscar rápido, e responde por SMTP (smtp.hostinger.com:465) gravando a resposta também na
pasta Enviados do servidor. Anexos recebidos são baixados do servidor na hora. E-mails de um lead do
CRM entram no histórico dele. A senha da caixa é colada só na tela do Master (nunca aparece de novo)."""
import imaplib
import json
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parsedate_to_datetime
from html import unescape

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import admin_logado, avisar, email_valido, exigir_csrf, ir, log, plataforma, render
from .db import CaixaEmail, Config, Lead, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
CAMPOS = ("caixa_host", "caixa_porta", "caixa_smtp_host", "caixa_smtp_porta", "caixa_user", "caixa_senha")
PADRAO = {"caixa_host": "imap.hostinger.com", "caixa_porta": "993", "caixa_smtp_host": "smtp.hostinger.com",
          "caixa_smtp_porta": "465"}
PRIMEIRA_CARGA = 300          # na primeira leitura, traz só os e-mails mais recentes
POR_PAGINA = 40
MAX_ANEXOS = 20 * 1024 * 1024
PASTAS = {"entrada": "Caixa de entrada", "enviados": "Enviados"}


# ------------------------------------------------------------------ configuração
def _cfg(db: Session, chave: str) -> str:
    c = db.get(Config, chave)
    return c.valor if c else ""


def _gravar(db: Session, chave: str, valor: str) -> None:
    c = db.get(Config, chave) or Config(chave=chave, valor="")
    c.valor = valor
    db.add(c)


def dados(db: Session | None = None) -> dict:
    if db is None:
        with SessionLocal() as s:
            return dados(s)
    return {k: _cfg(db, k) or PADRAO.get(k, "") for k in CAMPOS}


def configurado(db: Session | None = None) -> bool:
    d = dados(db)
    return bool(d["caixa_host"] and d["caixa_user"] and d["caixa_senha"])


# ------------------------------------------------------------------ servidor (os testes trocam estas funções)
def _conectar(d: dict) -> imaplib.IMAP4_SSL:
    con = imaplib.IMAP4_SSL(d["caixa_host"], int(d["caixa_porta"] or 993), ssl_context=ssl.create_default_context(),
                            timeout=30)
    con.login(d["caixa_user"], d["caixa_senha"])
    return con


def _buscar(d: dict, ultimo_uid: int, validade: str) -> tuple[str, list[tuple[int, bytes, bool]]]:
    """Novos e-mails da caixa de entrada: (UIDVALIDITY, [(uid, bruto, já lido)])."""
    con = _conectar(d)
    try:
        con.select("INBOX", readonly=True)
        resp = con.response("UIDVALIDITY")[1]
        val = (resp[0].decode() if resp and resp[0] else "")
        if val != validade:
            ultimo_uid = 0
        typ, dados_ = con.uid("SEARCH", None, f"UID {ultimo_uid + 1}:*" if ultimo_uid else "ALL")
        uids = [int(x) for x in (dados_[0] or b"").split() if int(x) > ultimo_uid]
        if not ultimo_uid:
            uids = uids[-PRIMEIRA_CARGA:]
        saida = []
        for i in range(0, len(uids), 25):
            lote = ",".join(str(x) for x in uids[i:i + 25])
            typ, partes = con.uid("FETCH", lote, "(FLAGS BODY.PEEK[])")
            for p in partes:
                if isinstance(p, tuple) and len(p) == 2:
                    cab = p[0].decode(errors="replace")
                    m = re.search(r"UID (\d+)", cab)
                    if m:
                        saida.append((int(m.group(1)), p[1], "\\Seen" in cab))
        return val, sorted(saida)
    finally:
        try:
            con.logout()
        except Exception:
            pass


def _baixar(d: dict, uid: int) -> bytes:
    con = _conectar(d)
    try:
        con.select("INBOX", readonly=True)
        typ, partes = con.uid("FETCH", str(uid), "(BODY.PEEK[])")
        for p in partes:
            if isinstance(p, tuple):
                return p[1]
        return b""
    finally:
        con.logout()


def _marcar_lido(d: dict, uid: int, lido: bool = True) -> None:
    try:
        con = _conectar(d)
        con.select("INBOX")
        con.uid("STORE", str(uid), "+FLAGS" if lido else "-FLAGS", "(\\Seen)")
        con.logout()
    except Exception as ex:
        log.warning("Caixa: não marquei o e-mail %s como lido (%s)", uid, type(ex).__name__)


def _pasta_enviados(con) -> str:
    typ, linhas = con.list()
    for ln in linhas or []:
        t = ln.decode(errors="replace") if isinstance(ln, bytes) else str(ln)
        if "\\Sent" in t:
            return t.rsplit(" ", 1)[-1].strip('"')
    for ln in linhas or []:
        t = ln.decode(errors="replace") if isinstance(ln, bytes) else str(ln)
        nome = t.rsplit(" ", 1)[-1].strip('"')
        if nome.lower().endswith("sent"):
            return nome
    return "INBOX.Sent"


def _enviar(d: dict, msg: EmailMessage) -> None:
    porta = int(d["caixa_smtp_porta"] or 465)
    ctx = ssl.create_default_context()
    if porta == 465:
        with smtplib.SMTP_SSL(d["caixa_smtp_host"], porta, context=ctx, timeout=30) as s:
            s.login(d["caixa_user"], d["caixa_senha"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(d["caixa_smtp_host"], porta, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(d["caixa_user"], d["caixa_senha"])
            s.send_message(msg)
    try:   # cópia na pasta Enviados do servidor, para aparecer também no webmail
        con = _conectar(d)
        con.append(_pasta_enviados(con), "(\\Seen)", imaplib.Time2Internaldate(time.time()), bytes(msg))
        con.logout()
    except Exception as ex:
        log.warning("Caixa: enviado, mas sem cópia em Enviados (%s)", type(ex).__name__)


# ------------------------------------------------------------------ leitura das mensagens
def _texto_do_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", h)
    t = unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def _partes_anexo(msg):
    """(índice no walk, parte) de cada anexo: o mesmo índice serve para baixar depois."""
    for i, p in enumerate(msg.walk()):
        if p.is_multipart():
            continue
        nome = p.get_filename()
        disp = p.get_content_disposition()
        if nome or disp == "attachment":
            yield i, p


def ler(bruto: bytes) -> dict:
    msg = message_from_bytes(bruto, policy=policy.default)

    def cab(n):
        try:
            return str(msg.get(n, "") or "")
        except Exception:
            return ""
    de = getaddresses([cab("From")])
    try:
        dt = parsedate_to_datetime(cab("Date"))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        dt = banco.agora()
    texto = html = ""
    try:
        corpo = msg.get_body(preferencelist=("plain",))
        texto = corpo.get_content() if corpo else ""
        corpo = msg.get_body(preferencelist=("html",))
        html = corpo.get_content() if corpo else ""
    except Exception:
        pass
    if not texto and html:
        texto = _texto_do_html(html)
    anexos = []
    for i, p in _partes_anexo(msg):
        try:
            tam = len(p.get_payload(decode=True) or b"")
        except Exception:
            tam = 0
        anexos.append({"n": i, "nome": (p.get_filename() or "anexo")[:200], "tipo": p.get_content_type(), "tam": tam})
    return {"message_id": cab("Message-ID")[:300], "em_resposta_a": cab("In-Reply-To")[:300],
            "referencias": cab("References")[:4000],
            "de_nome": (de[0][0] if de else "")[:200], "de_email": (de[0][1] if de else "").lower()[:200],
            "para": cab("To")[:4000], "cc": cab("Cc")[:4000], "assunto": cab("Subject")[:500] or "(sem assunto)",
            "texto": texto[:200_000], "html": html[:500_000], "anexos": json.dumps(anexos, ensure_ascii=False),
            "data": dt}


def _ligar_lead(db: Session, m: CaixaEmail, entrada: bool, autor: Usuario | None = None) -> Lead | None:
    """E-mail de/para um lead do CRM entra no histórico dele."""
    from . import crm
    alvo = m.de_email if entrada else ""
    if not entrada:
        enderecos = [a.lower() for _, a in getaddresses([m.para, m.cc])]
        alvo = enderecos[0] if enderecos else ""
    if not alvo:
        return None
    ld = db.scalar(select(Lead).where(func.lower(Lead.email) == alvo).order_by(Lead.id.desc()).limit(1))
    if not ld:
        return None
    m.lead_id = ld.id
    resumo = re.sub(r"\s+", " ", m.texto or "")[:300]
    if entrada:
        crm.registrar(db, ld, f"E-mail recebido: {m.assunto}\n{resumo}", "email")
    else:
        crm.registrar(db, ld, f"E-mail enviado: {m.assunto}\n{resumo}", "email", autor)
    return ld


def equipe_email(db: Session) -> list[Usuario]:
    from . import crm
    pid = crm.empresa_plataforma_id(db)
    if not pid:
        return []
    return [x for x in db.scalars(select(Usuario).where(Usuario.empresa_id == pid, Usuario.ativo.is_(True)))
            if "email" in x.areas_master]


def sincronizar() -> int:
    """Chamado a cada minuto pelo agendador e pelo botão Atualizar. Devolve quantos e-mails chegaram."""
    with SessionLocal() as db:
        if not configurado(db):
            return 0
        d = dados(db)
        ultimo = int(_cfg(db, "caixa_ultimo_uid") or 0)
        validade = _cfg(db, "caixa_uidvalidity")
        try:
            val, novos = _buscar(d, ultimo, validade)
        except Exception as ex:
            _gravar(db, "caixa_erro", f"{type(ex).__name__}: {str(ex)[:200]}")
            _gravar(db, "caixa_sinc_em", banco.agora().isoformat())
            db.commit()
            log.warning("Caixa: falha ao ler o e-mail (%s)", type(ex).__name__)
            return 0
        if val != validade and ultimo:
            db.query(CaixaEmail).filter(CaixaEmail.pasta == "entrada").delete()   # a caixa foi recriada
        avisos = []
        ja = set(db.scalars(select(CaixaEmail.uid).where(CaixaEmail.pasta == "entrada",
                                                          CaixaEmail.uid.in_([u for u, _, _ in novos] or [0]))))
        primeira = not ultimo
        for uid, bruto, lido in novos:
            if uid in ja:
                continue
            try:
                info = ler(bruto)
            except Exception:
                continue
            m = CaixaEmail(pasta="entrada", uid=uid, lido=lido, **info)
            db.add(m)
            db.flush()
            ld = _ligar_lead(db, m, True)
            if not lido and not primeira:
                avisos.append((m, ld))
            ultimo = max(ultimo, uid)
        _gravar(db, "caixa_ultimo_uid", str(max([ultimo] + [u for u, _, _ in novos])))
        _gravar(db, "caixa_uidvalidity", val)
        _gravar(db, "caixa_erro", "")
        _gravar(db, "caixa_sinc_em", banco.agora().isoformat())
        db.commit()
        if avisos:
            from . import push
            gente = [x.id for x in equipe_email(db)]
            lembretes = []
            for m, ld in avisos[:10]:
                quem = m.de_nome or m.de_email
                for uid in gente:
                    lembretes.append((uid, f"E-mail de {quem}", m.assunto, f"/master/caixa/{m.id}"))
                if ld and ld.dono_id and ld.dono_id not in gente:
                    lembretes.append((ld.dono_id, f"E-mail do lead {ld.nome or quem}", m.assunto, f"/master/crm/{ld.id}"))
            try:
                push.avisar_usuarios(lembretes)
            except Exception:
                pass
        return len(avisos)


def nao_lidos() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count(CaixaEmail.id)).where(CaixaEmail.pasta == "entrada",
                                                                  CaixaEmail.lido.is_(False))) or 0


# ------------------------------------------------------------------ telas
def pode_configurar(u: Usuario) -> bool:
    return u.is_admin or "config" in u.areas_master


@rota.get("/master/caixa")
def caixa(request: Request, pasta: str = "entrada", q: str = "", pag: int = 1, filtro: str = "",
          u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if not configurado(db):
        if pode_configurar(u):
            return ir("/master/caixa/config")
        avisar(request, "A caixa de e-mail ainda não foi ligada. Peça ao dono da plataforma.", "erro")
        return ir("/master/crm" if "crm" in u.areas_master else "/painel")
    pasta = pasta if pasta in PASTAS else "entrada"
    sel = select(CaixaEmail).where(CaixaEmail.pasta == pasta)
    if filtro == "nao_lidos":
        sel = sel.where(CaixaEmail.lido.is_(False))
    elif filtro == "leads":
        sel = sel.where(CaixaEmail.lead_id.is_not(None))
    termo = q.strip()
    if termo:
        like = f"%{termo}%"
        sel = sel.where(or_(CaixaEmail.assunto.ilike(like), CaixaEmail.de_email.ilike(like),
                            CaixaEmail.de_nome.ilike(like), CaixaEmail.para.ilike(like), CaixaEmail.texto.ilike(like)))
    total = db.scalar(select(func.count()).select_from(sel.subquery())) or 0
    pag = max(1, pag)
    lista = list(db.scalars(sel.order_by(CaixaEmail.data.desc()).offset((pag - 1) * POR_PAGINA).limit(POR_PAGINA)))
    sinc = _cfg(db, "caixa_sinc_em")
    return render(request, "master/caixa.html", u=u, e=u.empresa, aba="master", sub="caixa", lista=lista,
                  pasta=pasta, q=q, pag=pag, filtro=filtro, total=total, paginas=max(1, -(-total // POR_PAGINA)),
                  PASTAS=PASTAS, caixa_email=dados(db)["caixa_user"], erro=_cfg(db, "caixa_erro"),
                  sinc=datetime.fromisoformat(sinc) if sinc else None, nao_lidos=nao_lidos(),
                  pode_config=pode_configurar(u), json=json)


@rota.post("/master/caixa/atualizar", dependencies=CSRF)
def atualizar(request: Request, u: Usuario = Depends(admin_logado)):
    n = sincronizar()
    with SessionLocal() as db:
        erro = _cfg(db, "caixa_erro")
    if erro:
        avisar(request, f"Não consegui ler a caixa: {erro}", "erro")
    else:
        avisar(request, f"{n} e-mail(s) novo(s)." if n else "Nenhum e-mail novo.")
    return ir("/master/caixa")


@rota.get("/master/caixa/config")
def config_tela(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if not pode_configurar(u):
        return ir("/master/caixa")
    d = dados(db)
    from . import email_envio
    envio = email_envio.dados()
    return render(request, "master/caixa_config.html", u=u, e=u.empresa, aba="master", sub="caixa",
                  d={k: v for k, v in d.items() if k != "caixa_senha"}, tem_senha=bool(d["caixa_senha"]),
                  sugestao=envio.get("smtp_user", ""), erro=_cfg(db, "caixa_erro"), ok=configurado(db))


@rota.post("/master/caixa/config", dependencies=CSRF)
def config_salvar(request: Request, host: str = Form(""), porta: str = Form("993"), smtp_host: str = Form(""),
                  smtp_porta: str = Form("465"), usuario: str = Form(""), senha: str = Form(""),
                  u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if not pode_configurar(u):
        return ir("/master/caixa")
    usuario = usuario.strip().lower()
    if not host.strip() or not smtp_host.strip() or not email_valido(usuario) \
            or not porta.strip().isdigit() or not smtp_porta.strip().isdigit():
        avisar(request, "Confira os servidores, as portas e o e-mail da caixa.", "erro")
        return ir("/master/caixa/config")
    if not senha and not _cfg(db, "caixa_senha"):
        avisar(request, "Informe a senha da caixa.", "erro")
        return ir("/master/caixa/config")
    trocou = usuario != _cfg(db, "caixa_user") or host.strip() != (_cfg(db, "caixa_host") or PADRAO["caixa_host"])
    teste = {"caixa_host": host.strip(), "caixa_porta": porta.strip(), "caixa_smtp_host": smtp_host.strip(),
             "caixa_smtp_porta": smtp_porta.strip(), "caixa_user": usuario,
             "caixa_senha": senha or _cfg(db, "caixa_senha")}
    try:
        _conectar(teste).logout()
    except Exception as ex:
        avisar(request, f"Não consegui entrar na caixa ({type(ex).__name__}). Confira o e-mail, a senha e o servidor.",
               "erro")
        return ir("/master/caixa/config")
    for k, v in teste.items():
        _gravar(db, k, v)
    if trocou:   # outra caixa: começa a cópia local do zero
        db.query(CaixaEmail).delete()
        _gravar(db, "caixa_ultimo_uid", "0")
        _gravar(db, "caixa_uidvalidity", "")
    regras.registrar(db, u.empresa_id, u.nome, "caixa de e-mail ligada", f"{usuario} em {host.strip()}.")
    db.commit()
    sincronizar()
    avisar(request, "Caixa de e-mail ligada. Os e-mails chegam aqui a cada minuto.")
    return ir("/master/caixa")


def _endereco_principal(m: CaixaEmail) -> str:
    if m.pasta == "entrada":
        return m.de_email
    end = getaddresses([m.para])
    return end[0][1].lower() if end else ""


@rota.get("/master/caixa/novo")
def novo(request: Request, para: str = "", responder: int = 0, encaminhar: int = 0, todos: int = 0, lead: int = 0,
         u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if not configurado(db):
        return ir("/master/caixa")
    orig = db.get(CaixaEmail, responder or encaminhar) if (responder or encaminhar) else None
    assunto, corpo, cc = "", "", ""
    if orig:
        quando = regras.para_local(u.empresa, orig.data).strftime("%d/%m/%Y %H:%M")
        citado = "\n".join("> " + ln for ln in (orig.texto or "").splitlines()[:200])
        quem = formataddr((orig.de_nome, orig.de_email)) if orig.de_email else ""
        if responder:
            para = para or (orig.de_email if orig.pasta == "entrada" else _endereco_principal(orig))
            assunto = orig.assunto if re.match(r"(?i)^\s*re:", orig.assunto) else f"Re: {orig.assunto}"
            corpo = f"\n\n\nEm {quando}, {quem} escreveu:\n{citado}"
            if todos:
                eu = dados(db)["caixa_user"]
                outros = [a for _, a in getaddresses([orig.para, orig.cc]) if a and a.lower() not in (eu, para.lower())]
                cc = ", ".join(outros)
        else:
            assunto = orig.assunto if re.match(r"(?i)^\s*(enc|fwd?):", orig.assunto) else f"Enc: {orig.assunto}"
            corpo = (f"\n\n\n---------- Mensagem encaminhada ----------\nDe: {quem}\nData: {quando}\n"
                     f"Assunto: {orig.assunto}\nPara: {orig.para}\n\n{orig.texto}")
    ld = db.get(Lead, lead) if lead else None
    if ld and not para:
        para = ld.email
    return render(request, "master/caixa_novo.html", u=u, e=u.empresa, aba="master", sub="caixa", para=para,
                  cc=cc, assunto=assunto, corpo=corpo, orig=orig, responder=responder, encaminhar=encaminhar,
                  lead=ld, anexos_orig=json.loads(orig.anexos or "[]") if orig and encaminhar else [])


def _validos(lista: str) -> list[str] | None:
    end = [a.strip() for _, a in getaddresses([lista]) if a.strip()]
    return end if all(email_valido(a) for a in end) else None


@rota.post("/master/caixa/enviar", dependencies=CSRF)
async def enviar(request: Request, tarefas_bg: BackgroundTasks, para: str = Form(""), cc: str = Form(""),
                 assunto: str = Form(""), texto: str = Form(""), responder: int = Form(0), encaminhar: int = Form(0),
                 manter_anexos: int = Form(0), anexos: list[UploadFile] = File(default=[]),
                 u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    d = dados(db)
    lp, lc = _validos(para), _validos(cc)
    if not configurado(db) or not lp or lc is None or not assunto.strip():
        avisar(request, "Confira os destinatários e o assunto.", "erro")
        return ir("/master/caixa")
    msg = EmailMessage()
    msg["From"] = formataddr((plataforma()["nome"], d["caixa_user"]))
    msg["To"] = ", ".join(lp)
    if lc:
        msg["Cc"] = ", ".join(lc)
    msg["Subject"] = assunto.strip()[:500]
    msg["Date"] = datetime.now(timezone.utc)
    msg["Message-ID"] = make_msgid(domain=d["caixa_user"].split("@")[-1])
    orig = db.get(CaixaEmail, responder) if responder else None
    if orig and orig.message_id:
        msg["In-Reply-To"] = orig.message_id
        msg["References"] = (orig.referencias + " " + orig.message_id).strip()[-3000:]
    corpo = texto.replace("\r\n", "\n")
    assinatura = f"\n\n--\n{u.nome}\n{plataforma()['nome']}"
    msg.set_content(corpo + assinatura)
    total, nomes = 0, []
    for a in anexos:
        if not a or not a.filename:
            continue
        conteudo = await a.read()
        total += len(conteudo)
        if total > MAX_ANEXOS:
            avisar(request, "Os anexos passam de 20 MB.", "erro")
            return ir("/master/caixa")
        tipo = (a.content_type or "application/octet-stream").split("/", 1)
        msg.add_attachment(conteudo, maintype=tipo[0], subtype=tipo[1] if len(tipo) > 1 else "octet-stream",
                           filename=a.filename[:200])
        nomes.append({"nome": a.filename[:200], "tipo": a.content_type or "", "tam": len(conteudo)})
    fonte = db.get(CaixaEmail, encaminhar) if encaminhar and manter_anexos else None
    if fonte and fonte.uid and json.loads(fonte.anexos or "[]"):
        try:
            orig_msg = message_from_bytes(_baixar(d, fonte.uid), policy=policy.default)
            for i, p in _partes_anexo(orig_msg):
                conteudo = p.get_payload(decode=True) or b""
                total += len(conteudo)
                if total > MAX_ANEXOS:
                    break
                tipo = p.get_content_type().split("/", 1)
                msg.add_attachment(conteudo, maintype=tipo[0], subtype=tipo[1], filename=p.get_filename() or "anexo")
                nomes.append({"nome": p.get_filename() or "anexo", "tipo": p.get_content_type(), "tam": len(conteudo)})
        except Exception:
            avisar(request, "Não consegui buscar os anexos originais; o e-mail foi sem eles.", "erro")
    try:
        _enviar(d, msg)
    except Exception as ex:
        log.warning("Caixa: envio falhou (%s)", type(ex).__name__)
        avisar(request, f"O envio falhou ({type(ex).__name__}). Tente de novo em instantes.", "erro")
        return ir(f"/master/caixa/{responder}" if responder else "/master/caixa")
    m = CaixaEmail(pasta="enviados", uid=0, message_id=msg["Message-ID"], em_resposta_a=msg.get("In-Reply-To", "") or "",
                   referencias=msg.get("References", "") or "", de_nome=plataforma()["nome"], de_email=d["caixa_user"],
                   para=msg["To"], cc=msg.get("Cc", "") or "", assunto=msg["Subject"], texto=corpo + assinatura,
                   anexos=json.dumps(nomes, ensure_ascii=False), data=banco.agora(), lido=True, autor_id=u.id)
    db.add(m)
    db.flush()
    _ligar_lead(db, m, False, u)
    if orig and not orig.lido:
        orig.lido = True
    db.commit()
    avisar(request, "E-mail enviado.")
    return ir(f"/master/caixa/{responder}" if responder else "/master/caixa?pasta=enviados")


@rota.get("/master/caixa/{mid}")
def abrir(mid: int, request: Request, tarefas_bg: BackgroundTasks, u: Usuario = Depends(admin_logado),
          db: Session = Depends(get_db)):
    m = db.get(CaixaEmail, mid)
    if not m:
        avisar(request, "E-mail não encontrado.", "erro")
        return ir("/master/caixa")
    if not m.lido:
        m.lido = True
        db.commit()
        if m.uid:
            tarefas_bg.add_task(_marcar_lido, dados(db), m.uid, True)
    outro = _endereco_principal(m)
    conversa = []
    if outro:
        like = f"%{outro}%"
        conversa = [x for x in db.scalars(select(CaixaEmail).where(
            or_(CaixaEmail.de_email == outro, CaixaEmail.para.ilike(like), CaixaEmail.cc.ilike(like)))
            .order_by(CaixaEmail.data.desc()).limit(15)) if x.id != m.id]
    ld = db.get(Lead, m.lead_id) if m.lead_id else None
    autor = db.get(Usuario, m.autor_id) if m.autor_id else None
    return render(request, "master/caixa_ler.html", u=u, e=u.empresa, aba="master", sub="caixa", m=m,
                  anexos=json.loads(m.anexos or "[]"), conversa=conversa, ld=ld, autor=autor, outro=outro)


@rota.get("/master/caixa/{mid}/html")
def corpo_html(mid: int, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    """O HTML do e-mail num quadro isolado: sem scripts, sem formulários, links abrem em outra aba."""
    m = db.get(CaixaEmail, mid)
    if not m or not m.html:
        return Response("", media_type="text/html")
    pagina = ('<!doctype html><meta charset="utf-8"><base target="_blank">'
              '<style>body{font-family:system-ui,sans-serif;font-size:15px;margin:0;word-wrap:break-word}'
              'img{max-width:100%;height:auto}</style>' + m.html)
    return Response(pagina, media_type="text/html", headers={
        "Content-Security-Policy": "default-src 'none'; img-src https: http: data: cid:; style-src 'unsafe-inline' https:; "
                                   "font-src https: data:; form-action 'none'; frame-ancestors 'self'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "SAMEORIGIN"})   # o quadro da tela de leitura é do mesmo site


@rota.get("/master/caixa/{mid}/anexo/{n}")
def anexo(mid: int, n: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    m = db.get(CaixaEmail, mid)
    if not m or not m.uid:
        avisar(request, "Anexo não encontrado.", "erro")
        return ir("/master/caixa")
    try:
        msg = message_from_bytes(_baixar(dados(db), m.uid), policy=policy.default)
    except Exception as ex:
        avisar(request, f"Não consegui baixar o anexo ({type(ex).__name__}).", "erro")
        return ir(f"/master/caixa/{mid}")
    for i, p in _partes_anexo(msg):
        if i == n:
            nome = re.sub(r'[\r\n"\\]', "_", p.get_filename() or "anexo")
            from urllib.parse import quote
            return Response(p.get_payload(decode=True) or b"", media_type="application/octet-stream", headers={
                "Content-Disposition": f"attachment; filename=\"{nome.encode('ascii', 'replace').decode()}\"; "
                                       f"filename*=UTF-8''{quote(nome)}",
                "X-Content-Type-Options": "nosniff"})
    avisar(request, "Anexo não encontrado.", "erro")
    return ir(f"/master/caixa/{mid}")


@rota.post("/master/caixa/{mid}/nao-lido", dependencies=CSRF)
def nao_lido(mid: int, request: Request, tarefas_bg: BackgroundTasks, u: Usuario = Depends(admin_logado),
             db: Session = Depends(get_db)):
    m = db.get(CaixaEmail, mid)
    if m:
        m.lido = False
        db.commit()
        if m.uid:
            tarefas_bg.add_task(_marcar_lido, dados(db), m.uid, False)
    return ir("/master/caixa")


@rota.post("/master/caixa/{mid}/lead", dependencies=CSRF)
def criar_lead(mid: int, request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    """Cria (ou liga) o lead do CRM a partir de quem mandou o e-mail."""
    from . import crm
    m = db.get(CaixaEmail, mid)
    alvo = _endereco_principal(m) if m else ""
    if not m or not alvo:
        return ir("/master/caixa")
    ld = db.scalar(select(Lead).where(func.lower(Lead.email) == alvo).limit(1))
    if not ld:
        nome = m.de_nome if m.pasta == "entrada" else (getaddresses([m.para])[0][0] if m.para else "")
        ld = Lead(nome=(nome or alvo.split("@")[0])[:80], email=alvo, origem="email", etapa="novo",
                  dono_id=u.id if "crm" in u.areas_master else None, criado_em=banco.agora(),
                  atualizado_em=banco.agora())
        db.add(ld)
        db.flush()
        crm.registrar(db, ld, f"Lead criado a partir do e-mail \"{m.assunto}\".", "sistema", u)
    for x in db.scalars(select(CaixaEmail).where(or_(CaixaEmail.de_email == alvo, CaixaEmail.para.ilike(f"%{alvo}%")),
                                                 CaixaEmail.lead_id.is_(None))):
        x.lead_id = ld.id
    db.commit()
    if "crm" in u.areas_master:
        return ir(f"/master/crm/{ld.id}")
    avisar(request, "Lead criado no CRM.")
    return ir(f"/master/caixa/{mid}")

