"""Logo da empresa: enviada em Configurações e usada no topo dos impressos (folha de ponto,
resumo e tabela do fechamento, holerites)."""
import re
import secrets

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import avisar, exigir_csrf, gestor_logado, ir
from .db import Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

LIMITE = 1024 * 1024                     # 1 MB
PASTA = banco.PASTA_DADOS / "logos"
NOME = re.compile(r"^[0-9]+-[0-9a-f]{12}\.(jpg|png|webp)$")
TIPOS = {b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png", b"RIFF": "webp"}


def tipo_da_imagem(dados: bytes) -> str | None:
    """Confere pelo conteúdo (não pelo nome do arquivo) que é JPG, PNG ou WebP."""
    ext = next((x for sig, x in TIPOS.items() if dados.startswith(sig)), None)
    if ext == "webp" and dados[8:12] != b"WEBP":
        return None
    return ext


def _apagar(nome: str):
    if nome and NOME.match(nome):
        (PASTA / nome).unlink(missing_ok=True)


@rota.post("/painel/config/logo", dependencies=CSRF)
def logo_salvar(request: Request, arquivo: UploadFile | None = File(None), remover: str = Form(""),
                u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    volta = "/painel/config#logo"
    if remover:
        _apagar(e.logo)
        e.logo = ""
        regras.registrar(db, e.id, u.nome, "logo removida", "A logo saiu dos impressos.")
        db.commit()
        avisar(request, "Logo removida dos impressos.")
        return ir(volta)
    dados = arquivo.file.read(LIMITE + 1) if arquivo else b""
    if not dados:
        avisar(request, "Escolha a imagem da logo.", "erro")
        return ir(volta)
    if len(dados) > LIMITE:
        avisar(request, "A imagem passa de 1 MB. Diminua o tamanho e envie de novo.", "erro")
        return ir(volta)
    ext = tipo_da_imagem(dados)
    if not ext:
        avisar(request, "Envie a logo em JPG, PNG ou WebP.", "erro")
        return ir(volta)
    PASTA.mkdir(parents=True, exist_ok=True)
    nome = f"{e.id}-{secrets.token_hex(6)}.{ext}"
    (PASTA / nome).write_bytes(dados)
    _apagar(e.logo)
    e.logo = nome
    regras.registrar(db, e.id, u.nome, "logo enviada", "Nova logo nos impressos.")
    db.commit()
    avisar(request, "Logo salva. Ela já aparece nos impressos.")
    return ir(volta)


@rota.get("/logos/{nome}")
def logo_arquivo(nome: str):
    """A logo é pública como a de um site: o nome é aleatório e só serve imagem conferida."""
    if not NOME.match(nome) or not (PASTA / nome).is_file():
        return Response(status_code=404)
    return FileResponse(PASTA / nome, headers={"Cache-Control": "public,max-age=86400",
                                               "X-Content-Type-Options": "nosniff"})
