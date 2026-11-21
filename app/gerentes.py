"""Gerentes da empresa: o dono convida outras pessoas para o painel e escolhe o que cada uma
pode fazer (core.PERMISSOES). Só o dono mexe aqui, em Configurações e na Assinatura."""
import secrets

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import regras
from .core import (PERMISSOES, alcance_locais, alcance_setores, avisar, base_url, celular_limpo, celular_valido, da_empresa,
                   email_valido, exigir_csrf, gestor_logado, ir, permissoes_de, render)
from .db import Local, Usuario, get_db
from .fase3 import locais_da
from .setores import setores_da

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]


def _lista(form) -> str:
    return ",".join(k for k in PERMISSOES if form.get(f"p_{k}"))


def _alcance(db: Session, e, form) -> tuple[str, str]:
    """Setores e locais marcados (só os da empresa). "0" = endereço principal."""
    s = [str(x.id) for x in setores_da(db, e.id) if form.get(f"s_{x.id}")]
    l = (["0"] if form.get("l_0") else []) + [str(x.id) for x in locais_da(db, e.id) if form.get(f"l_{x.id}")]
    return ",".join(s), ",".join(l)


def link(request: Request, g: Usuario) -> str:
    return f"{base_url(request)}/convite/{g.convite_token}" if g.convite_token else ""


@rota.get("/painel/gerentes")
def gerentes(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    lista = list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == u.empresa_id, Usuario.papel == "gestor")
        .order_by(Usuario.dono.desc(), Usuario.ativo.desc(), Usuario.nome)))
    return render(request, "painel_gerentes.html", u=u, e=u.empresa, aba="equipe",
                  setores=setores_da(db, u.empresa_id), locais=locais_da(db, u.empresa_id),
                  alc_s={g.id: alcance_setores(g) for g in lista}, alc_l={g.id: alcance_locais(g) for g in lista},
                  gerentes=lista, links={g.id: link(request, g) for g in lista},
                  perms={g.id: permissoes_de(g) for g in lista})


@rota.post("/painel/gerentes", dependencies=CSRF)
async def gerente_novo(request: Request, u: Usuario = Depends(gestor_logado),
                       db: Session = Depends(get_db)):
    form = await request.form()
    nome = str(form.get("nome", "")).strip()[:120]
    email = str(form.get("email", "")).strip().lower()
    cel = celular_limpo(str(form.get("celular", "")))
    na_escala = bool(form.get("na_escala"))
    erro = None
    if not nome or not email_valido(email):
        erro = "Informe o nome e um e-mail válido (é o login do gerente)."
    elif db.scalar(select(Usuario.id).where(Usuario.email == email).execution_options(alcance_livre=True)):
        erro = "Já existe uma conta com este e-mail."
    elif cel and not celular_valido(cel):
        erro = "Celular inválido: use o DDD (ex.: 98 99999-1234)."
    elif cel and db.scalar(select(Usuario.id).where(Usuario.celular == cel).execution_options(alcance_livre=True)):
        erro = "Este celular já está cadastrado."
    if erro:
        avisar(request, erro, "erro")
        return ir("/painel/gerentes")
    g = Usuario(empresa_id=u.empresa_id, nome=nome, email=email, celular=cel or None,
                cargo=str(form.get("cargo", "")).strip()[:80], papel="gestor", dono=False,
                na_escala=na_escala, permissoes=_lista(form),
                convite_token=secrets.token_urlsafe(18))
    g.alcance_setores, g.alcance_locais = _alcance(db, u.empresa, form)
    db.add(g)
    regras.registrar(db, u.empresa_id, u.nome, "gerente cadastrado",
                     f"{nome} ({email}): {g.permissoes or 'sem permissões'}.")
    db.commit()
    avisar(request, f"{nome} cadastrado(a). Envie o link de acesso para criar a senha.")
    return ir(f"/painel/gerentes#g{g.id}")


@rota.post("/painel/gerentes/{gid}", dependencies=CSRF)
async def gerente_editar(request: Request, gid: int, u: Usuario = Depends(gestor_logado),
                         db: Session = Depends(get_db)):
    form = await request.form()
    acao = str(form.get("acao", "salvar"))
    g = da_empresa(db, Usuario, gid, u)
    volta = f"/painel/gerentes#g{g.id}"
    if g.papel != "gestor" or g.dono:
        avisar(request, "A conta do dono não muda por aqui.", "erro")
        return ir("/painel/gerentes")
    if acao == "salvar":
        g.permissoes = _lista(form)
        g.na_escala = bool(form.get("na_escala"))
        g.alcance_setores, g.alcance_locais = _alcance(db, u.empresa, form)
        nomes = [x.nome for x in setores_da(db, u.empresa_id) if x.id in alcance_setores(g)]
        nomes += [u.empresa.nome if lid == 0 else (db.get(Local, lid).nome if db.get(Local, lid) else "")
                  for lid in sorted(alcance_locais(g))]
        perms = g.permissoes or "sem permissões"
        nomes_txt = ", ".join(n for n in nomes if n)
        regras.registrar(db, u.empresa_id, u.nome, "permissões do gerente",
                         f"{g.nome}: {perms}; só {nomes_txt}." if nomes else f"{g.nome}: {perms}; equipe inteira.")
        avisar(request, f"Permissões de {g.primeiro_nome} salvas.")
    elif acao in ("desativar", "reativar"):
        g.ativo = acao == "reativar"
        regras.registrar(db, u.empresa_id, u.nome, f"gerente {'reativado' if g.ativo else 'desativado'}",
                         g.nome + ".")
        avisar(request, f"{g.primeiro_nome} pode entrar de novo." if g.ativo
               else f"{g.primeiro_nome} não entra mais no painel.")
    elif acao == "novo_link":
        g.convite_token = secrets.token_urlsafe(18)
        g.senha_hash = None
        regras.registrar(db, u.empresa_id, u.nome, "gerente: novo link de acesso", g.nome + ".")
        avisar(request, f"Novo link gerado. A senha antiga de {g.primeiro_nome} deixou de valer.")
    db.commit()
    return ir(volta)
