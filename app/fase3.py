"""Fase 3: locais extras (filiais), totem (tablet da empresa com código do minuto, QR e
ponto por PIN) e atalhos de WhatsApp."""
import io
import re
import secrets
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from . import i18n, push, regras
from .core import (COOKIE_SECURE, avisar, base_url, conferir_senha, da_empresa,
                   dentro_do_limite, exigir_csrf, gerar_hash, gestor_logado, ir, limite_locais, render,
                   usuario_logado)
from .db import Local, Totem, Turno, Usuario, get_db

try:
    import qrcode
    import qrcode.image.svg
except Exception:  # sem a biblioteca o totem funciona, só sem o QR
    qrcode = None

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
COOKIE_TOTEM = "turno_totem"
PIN_RE = re.compile(r"^\d{4,6}$")
MAX_ERROS_PIN = 5


def zap(celular: str | None, texto: str) -> str:
    """Link que abre o WhatsApp do gestor com a mensagem já escrita (ele é quem envia)."""
    return f"https://wa.me/55{celular}?text={quote(texto)}" if celular else ""


def _numero(s: str) -> float | None:
    try:
        return float(str(s).strip().replace(",", "."))
    except ValueError:
        return None


def locais_da(db: Session, empresa_id: int, so_ativos: bool = True) -> list[Local]:
    filtros = [Local.empresa_id == empresa_id]
    if so_ativos:
        filtros.append(Local.ativo.is_(True))
    return list(db.scalars(select(Local).where(*filtros).order_by(Local.ativo.desc(), Local.nome)))


# ============================================================ gestor: locais e totens
def sem_local(db: Session, e) -> str | None:
    """Mensagem de erro se o plano não permite mais um local (o principal conta como um)."""
    lim = limite_locais(e)
    if lim and 1 + len(locais_da(db, e.id)) >= lim:
        return (f"Seu plano {e.plano.nome} permite {lim} local(is) de ponto, contando o "
                "endereço principal. Mude de plano em Configurações → Assinatura e plano.")
    return None


def limite_totens(db: Session, e) -> int:
    """No máximo um totem por local de ponto ativo (o endereço principal conta como um)."""
    return 1 + len(locais_da(db, e.id))


def totens_da(db: Session, empresa_id: int) -> int:
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(Totem).where(Totem.empresa_id == empresa_id)) or 0


@rota.get("/painel/locais")
def locais(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    totens = list(db.scalars(select(Totem).where(Totem.empresa_id == u.empresa_id)
                             .order_by(Totem.nome)))
    return render(request, "painel_locais.html", u=u, e=u.empresa, aba="locais",
                  locais=locais_da(db, u.empresa_id, so_ativos=False), totens=totens,
                  link_totem=request.session.pop("totem_link", None),
                  limite_totens=limite_totens(db, u.empresa))


def _ler_local(request: Request, nome: str, lat: str, lng: str, raio_m: str):
    la, lo, raio = _numero(lat), _numero(lng), _numero(raio_m)
    nome = nome.strip()[:80]
    if not nome:
        avisar(request, "Dê um nome ao local (ex.: Filial Centro).", "erro")
    elif la is None or lo is None or not -90 <= la <= 90 or not -180 <= lo <= 180:
        avisar(request, "Latitude ou longitude inválida. Use o botão “Usar minha localização”.", "erro")
    elif raio is None or not 30 <= raio <= 5000:
        avisar(request, "O raio precisa ficar entre 30 e 5000 metros.", "erro")
    else:
        return nome, la, lo, int(raio)
    return None


@rota.post("/painel/locais", dependencies=CSRF)
def local_novo(request: Request, nome: str = Form(""), endereco: str = Form(""),
               lat: str = Form(""), lng: str = Form(""), raio_m: str = Form("150"),
               u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    erro = sem_local(db, u.empresa)
    dados = None if erro else _ler_local(request, nome, lat, lng, raio_m)
    if erro:
        avisar(request, erro, "erro")
    if dados:
        db.add(Local(empresa_id=u.empresa_id, nome=dados[0], endereco=endereco.strip()[:300],
                     lat=dados[1], lng=dados[2], raio_m=dados[3]))
        regras.registrar(db, u.empresa_id, u.nome, "local criado",
                         f"{dados[0]}: {dados[1]}, {dados[2]}; raio {dados[3]} m.")
        db.commit()
        avisar(request, f"Local “{dados[0]}” criado. O ponto já vale lá.")
    return ir("/painel/locais")


@rota.post("/painel/locais/{lid}", dependencies=CSRF)
def local_editar(request: Request, lid: int, acao: str = Form("salvar"), nome: str = Form(""),
                 endereco: str = Form(""), lat: str = Form(""), lng: str = Form(""),
                 raio_m: str = Form("150"), u: Usuario = Depends(gestor_logado),
                 db: Session = Depends(get_db)):
    l = da_empresa(db, Local, lid, u)
    if acao == "reativar" and not l.ativo and sem_local(db, u.empresa):
        avisar(request, sem_local(db, u.empresa), "erro")
    elif acao in ("desativar", "reativar"):
        l.ativo = acao == "reativar"
        regras.registrar(db, u.empresa_id, u.nome, f"local {'reativado' if l.ativo else 'desativado'}",
                         l.nome + ".")
        db.commit()
    else:
        dados = _ler_local(request, nome, lat, lng, raio_m)
        if dados:
            l.nome, l.lat, l.lng, l.raio_m = dados
            l.endereco = endereco.strip()[:300]
            regras.registrar(db, u.empresa_id, u.nome, "local alterado",
                             f"{l.nome}: {l.lat}, {l.lng}; raio {l.raio_m} m.")
            db.commit()
            avisar(request, "Local salvo.")
    return ir("/painel/locais")


def _novo_link(request: Request, t: Totem):
    token = secrets.token_urlsafe(32)
    t.token_hash = regras.hash_aparelho(token)
    request.session["totem_link"] = {"nome": t.nome,
                                     "link": f"{base_url(request)}/totem/ativar/{token}"}


@rota.post("/painel/totens", dependencies=CSRF)
def totem_novo(request: Request, nome: str = Form(""), local_id: str = Form(""),
               u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    nome = nome.strip()[:80]
    if not nome:
        avisar(request, "Dê um nome ao totem (ex.: Tablet da entrada).", "erro")
        return ir("/painel/locais#totens")
    lim = limite_totens(db, u.empresa)
    if totens_da(db, u.empresa_id) >= lim:
        avisar(request, f"Você pode ter até {lim} totem(ns): um por local de ponto. Crie outro "
                        "local ou remova um totem antes.", "erro")
        return ir("/painel/locais#totens")
    local = da_empresa(db, Local, int(local_id), u) if local_id.isdigit() else None
    t = Totem(empresa_id=u.empresa_id, nome=nome, local_id=local.id if local else None,
              segredo=secrets.token_hex(32), token_hash="", criado_em=banco.agora())
    _novo_link(request, t)
    db.add(t)
    regras.registrar(db, u.empresa_id, u.nome, "totem criado", nome + ".")
    db.commit()
    return ir("/painel/locais#totens")


@rota.post("/painel/totens/{tid}", dependencies=CSRF)
def totem_editar(request: Request, tid: int, acao: str = Form(...),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    t = da_empresa(db, Totem, tid, u)
    if acao == "remover":
        db.delete(t)
        regras.registrar(db, u.empresa_id, u.nome, "totem removido", t.nome + ".")
        avisar(request, "Totem removido. O tablet que estava com ele parou de funcionar.")
    elif acao == "novo_link":
        _novo_link(request, t)
        regras.registrar(db, u.empresa_id, u.nome, "totem: novo link", t.nome + ".")
    db.commit()
    return ir("/painel/locais#totens")


# ============================================================ o totem (tablet, sem login)
def totem_atual(request: Request, db: Session) -> Totem | None:
    token = request.cookies.get(COOKIE_TOTEM, "")
    if not token:
        return None
    t = db.scalar(select(Totem).where(Totem.token_hash == regras.hash_aparelho(token)))
    return t if t and t.empresa.ativo else None


@rota.get("/totem/ativar/{token}")
def totem_ativar(token: str, db: Session = Depends(get_db)):
    resp = ir("/totem")
    if db.scalar(select(Totem.id).where(Totem.token_hash == regras.hash_aparelho(token))):
        resp.set_cookie(COOKIE_TOTEM, token, max_age=60 * 60 * 24 * 730, httponly=True,
                        samesite="lax", secure=COOKIE_SECURE)
    return resp


def _estado(t: Totem) -> dict:
    agora_u = banco.agora()
    al = regras.para_local(t.empresa, agora_u)
    return {"codigo": regras.codigo_totem(t), "resta": 60 - agora_u.second,
            "hora": al.strftime("%H:%M:%S")}


@rota.get("/totem")
def totem(request: Request, db: Session = Depends(get_db)):
    t = totem_atual(request, db)
    if not t:
        return render(request, "totem.html", t=None, e=None, pessoas=[], estado=None)
    t.visto_em = banco.agora()
    db.commit()
    pessoas = list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == t.empresa_id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True))
        .order_by(Usuario.nome)))
    return render(request, "totem.html", t=t, e=t.empresa, pessoas=pessoas, estado=_estado(t),
                  tem_qr=qrcode is not None)


@rota.get("/totem/estado")
def totem_estado(request: Request, db: Session = Depends(get_db)):
    t = totem_atual(request, db)
    if not t:
        return i18n.resposta_erro({"erro": "Totem desativado."}, 403)
    return _estado(t)


@rota.get("/totem/qr.svg")
def totem_qr(request: Request, db: Session = Depends(get_db)):
    t = totem_atual(request, db)
    if not t or qrcode is None:
        return Response("", 404)
    q = qrcode.QRCode(border=2)
    q.add_data(f"{base_url(request)}/app?codigo={regras.codigo_totem(t)}")
    q.make(fit=True)
    saida = io.BytesIO()
    q.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(saida)
    return Response(saida.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@rota.post("/totem/ponto", dependencies=CSRF)
async def totem_ponto(request: Request, tarefas_bg: BackgroundTasks,
                      db: Session = Depends(get_db)):
    t = totem_atual(request, db)
    if not t:
        return i18n.resposta_erro({"erro": "Este totem foi desativado. Fale com o gestor."}, 403)
    try:
        d = await request.json()
        uid, pin = int(d["uid"]), str(d["pin"])
    except Exception:
        return i18n.resposta_erro({"erro": "Escolha o seu nome e digite o PIN."}, 400)
    if not dentro_do_limite(f"totem:{t.id}", 40, 600):
        return i18n.resposta_erro({"erro": "Muitas tentativas. Aguarde alguns minutos."}, 429)
    u = db.get(Usuario, uid)
    if not u or u.empresa_id != t.empresa_id or not u.ativo or not u.na_escala:
        return i18n.resposta_erro({"erro": "Funcionário não encontrado."}, 400)
    if not u.pin_hash:
        return i18n.resposta_erro({"erro": "Você ainda não criou o seu PIN. Crie no aplicativo, "
                                     "na tela do ponto, em “PIN para o totem”."}, 400)
    if u.pin_erros >= MAX_ERROS_PIN:
        return i18n.resposta_erro({"erro": "PIN bloqueado por excesso de erros. Crie um novo PIN "
                                     "no aplicativo."}, 400)
    if not conferir_senha(pin, u.pin_hash):
        u.pin_erros += 1
        db.commit()
        return i18n.resposta_erro({"erro": "PIN incorreto."}, 400)
    u.pin_erros = 0
    try:
        b, alertas = regras.bater(db, u, None, None, None, totem=t, no_totem=True)
    except regras.ErroPonto as ex:
        db.commit()
        return i18n.resposta_erro({"erro": str(ex)}, 400)
    tarefas_bg.add_task(push.avisar_alertas, [a.id for a in alertas])
    hora = regras.para_local(u.empresa, b.momento).strftime("%H:%M")
    return {"ok": True, "tipo": b.tipo,
            "mensagem": f"{u.primeiro_nome}, {'entrada' if b.tipo == 'entrada' else 'saída'} "
                        f"registrada às {hora}."}


# ============================================================ funcionário: PIN do totem
@rota.post("/app/pin", dependencies=CSRF)
def app_pin(request: Request, senha: str = Form(""), pin: str = Form(""),
            u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    if not dentro_do_limite(f"pin:{u.id}", 8, 600):
        avisar(request, "Muitas tentativas. Aguarde alguns minutos.", "erro")
    elif not PIN_RE.match(pin) or len(set(pin)) == 1:
        avisar(request, "O PIN precisa ter de 4 a 6 números, não todos iguais.", "erro")
    elif not conferir_senha(senha, u.senha_hash):
        avisar(request, "Senha incorreta. O PIN não foi alterado.", "erro")
    else:
        u.pin_hash, u.pin_erros = gerar_hash(pin), 0
        regras.registrar(db, u.empresa_id, u.nome, "PIN do totem definido", "Pelo próprio funcionário.")
        db.commit()
        avisar(request, "PIN criado. Use no tablet da empresa para bater o ponto.")
    return ir("/app")


def texto_escala(nome: str, empresa: str, dias: list, turnos: dict, uid: int) -> str:
    """Mensagem com a escala da semana de um funcionário, para mandar pelo WhatsApp."""
    linhas = [f"Olá {nome}! Sua escala na {empresa}, de {dias[0]:%d/%m} a {dias[-1]:%d/%m}:"]
    for d in dias:
        t: Turno | None = turnos.get((uid, d))
        if not t:
            continue
        onde = f" ({t.local.nome})" if t.local else ""
        linhas.append(f"{regras.DIAS_SEMANA[d.weekday()]} {d:%d/%m}: "
                      + ("folga" if t.folga else f"{t.inicio} às {t.fim}{onde}"
                         + (f", intervalo às {t.intervalo_inicio}" if t.intervalo_inicio else "")))
    return "\n".join(linhas) if len(linhas) > 1 else ""
