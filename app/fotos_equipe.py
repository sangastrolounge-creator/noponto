"""Fotos da equipe: o gestor envia na ficha da pessoa, ou a própria pessoa nos Ajustes do app.
A imagem chega já reduzida pelo navegador (quadrada, até 320 px) e só aparece para quem é da
mesma empresa. A empresa escolhe em Configurações se as fotos aparecem nas telas."""
import base64
import re
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, Response
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import avisar, da_empresa, exigir_csrf, gestor_logado, ir, pode, usuario_logado
from .db import Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

LIMITE = 400 * 1024                       # a foto reduzida tem uns 20-60 KB
PASTA = banco.PASTA_DADOS / "perfis"
NOME = re.compile(r"^([0-9]+)-([0-9]+)-[0-9a-f]{12}\.(jpg|png|webp)$")
TIPOS = {b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png", b"RIFF": "webp"}
TIPO_HTTP = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _tipo(dados: bytes) -> str | None:
    ext = next((x for sig, x in TIPOS.items() if dados.startswith(sig)), None)
    if ext == "webp" and dados[8:12] != b"WEBP":
        return None
    return ext


def _apagar(nome: str):
    if nome and NOME.match(nome):
        (PASTA / nome).unlink(missing_ok=True)


def _decodificar(dados_url: str) -> bytes | None:
    """A foto vem como data URL (data:image/jpeg;base64,...) gerada pelo navegador."""
    m = re.match(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=\s]+)$", dados_url or "")
    if not m or len(m.group(2)) > LIMITE * 4 // 3 + 8:
        return None
    try:
        return base64.b64decode(m.group(2), validate=False)
    except ValueError:
        return None


def guardar(db: Session, pessoa: Usuario, dados_url: str, quem: str) -> str | None:
    """Grava a foto nova (e apaga a anterior). Devolve a mensagem de erro, ou None se deu certo."""
    dados = _decodificar(dados_url)
    if not dados or len(dados) > LIMITE:
        return "Não deu para ler a foto. Escolha outra imagem."
    ext = _tipo(dados)
    if not ext:
        return "Envie a foto em JPG, PNG ou WebP."
    PASTA.mkdir(parents=True, exist_ok=True)
    nome = f"{pessoa.empresa_id}-{pessoa.id}-{secrets.token_hex(6)}.{ext}"
    (PASTA / nome).write_bytes(dados)
    _apagar(pessoa.foto_perfil)
    pessoa.foto_perfil = nome
    regras.registrar(db, pessoa.empresa_id, quem, "foto da pessoa", f"Nova foto de {pessoa.nome}.")
    return None


def remover(db: Session, pessoa: Usuario, quem: str):
    _apagar(pessoa.foto_perfil)
    pessoa.foto_perfil = ""
    regras.registrar(db, pessoa.empresa_id, quem, "foto da pessoa", f"Foto de {pessoa.nome} removida.")


AUMENTO = 2   # as fotos aparecem com o dobro do tamanho pedido em cada tela


def iniciais(nome: str) -> str:
    partes = (nome or "?").split()
    return ((partes[0][:1] + (partes[-1][:1] if len(partes) > 1 else "")) or "?").upper()


def avatar(p, tam: int = 30, sempre: bool = False) -> Markup:
    """Foto redonda da pessoa (ou as iniciais, se não tiver foto). Vazio se a empresa desligou
    as fotos nas telas, a não ser com sempre=True (a própria ficha e os Ajustes)."""
    if p is None:
        return Markup("")
    e = getattr(p, "empresa", None)
    if not sempre and e is not None and not getattr(e, "mostrar_fotos", True):
        return Markup("")
    tam = int(tam) * AUMENTO
    estilo = f"--av:{tam}px"
    if p.foto_perfil and NOME.match(p.foto_perfil):
        return Markup(f'<img class="avatar-foto" src="/equipe/foto/{escape(p.foto_perfil)}" alt="" '
                      f'width="{int(tam)}" height="{int(tam)}" loading="lazy" style="{estilo}">')
    return Markup(f'<span class="avatar-foto avatar-ini" aria-hidden="true" style="{estilo}">'
                  f'{escape(iniciais(p.nome))}</span>')


@rota.get("/equipe/foto/{nome}")
def foto_arquivo(nome: str, u: Usuario = Depends(usuario_logado)):
    """Só quem é da mesma empresa vê a foto (é dado pessoal)."""
    m = NOME.match(nome)
    if not m or int(m.group(1)) != u.empresa_id or not (PASTA / nome).is_file():
        return Response(status_code=404)
    return FileResponse(PASTA / nome, media_type=TIPO_HTTP[m.group(3)],
                        headers={"Cache-Control": "private,max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


@rota.post("/painel/equipe/{pid}/foto", dependencies=CSRF)
def foto_da_pessoa(request: Request, pid: int, dados: str = Form(""), remover_foto: str = Form(""),
                   u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    volta = f"/painel/equipe/{pid}/ficha#foto"
    if not pode(u, "equipe"):
        return ir("/painel")
    p = da_empresa(db, Usuario, pid, u)
    if p.dono and not u.dono and p.id != u.id:
        avisar(request, "Só o dono muda a foto do dono.", "erro")
        return ir(volta)
    if remover_foto:
        remover(db, p, u.nome)
        db.commit()
        avisar(request, "Foto removida.")
        return ir(volta)
    erro = guardar(db, p, dados, u.nome)
    if erro:
        avisar(request, erro, "erro")
        return ir(volta)
    db.commit()
    avisar(request, "Foto salva.")
    return ir(volta)


@rota.post("/app/foto", dependencies=CSRF)
def minha_foto(request: Request, dados: str = Form(""), remover_foto: str = Form(""),
               u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    volta = "/app/ajustes#foto"
    eu = db.get(Usuario, u.id)
    if remover_foto:
        remover(db, eu, eu.nome)
        db.commit()
        avisar(request, "Foto removida.")
        return ir(volta)
    erro = guardar(db, eu, dados, eu.nome)
    if erro:
        avisar(request, erro, "erro")
        return ir(volta)
    db.commit()
    avisar(request, "Foto salva.")
    return ir(volta)


@rota.post("/painel/config/fotos", dependencies=CSRF)
def config_fotos(request: Request, mostrar: str = Form(""), u: Usuario = Depends(gestor_logado),
                 db: Session = Depends(get_db)):
    if not pode(u, "dono"):
        return ir("/painel")
    e = u.empresa
    e.mostrar_fotos = bool(mostrar)
    texto = "Fotos da equipe ligadas nas telas." if e.mostrar_fotos else "Fotos da equipe desligadas nas telas."
    regras.registrar(db, e.id, u.nome, "fotos da equipe", texto)
    db.commit()
    avisar(request, texto)
    return ir("/painel/config#fotos")


ICONE_ZAP = "/static/whatsapp.png?v=2"


def zap_icone(href: str, titulo: str = "WhatsApp", tam: int = 22, texto: str = "") -> Markup:
    """Ícone clicável do WhatsApp (no lugar do link escrito). `titulo` aparece ao passar o mouse
    e é lido pelo leitor de tela; `texto` opcional vai ao lado do ícone."""
    if not href:
        return Markup("")
    rotulo = escape(titulo)
    extra = f"<span>{escape(texto)}</span>" if texto else ""
    return Markup(f'<a class="zap-icone{" com-texto" if texto else ""}" href="{escape(href)}" target="_blank" '
                  f'rel="noopener" title="{rotulo}" aria-label="{rotulo}">'
                  f'<img src="{ICONE_ZAP}" alt="" width="{int(tam)}" height="{int(tam)}">{extra}</a>')
