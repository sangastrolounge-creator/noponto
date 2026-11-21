"""Setores da empresa (Cozinha, Salão, Caixa...): cadastro, e o filtro por setor usado
na Equipe, na Escala, no Ponto, nos Relatórios e na tabela de horas do Fechamento."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import regras
from .core import avisar, da_empresa, exigir_csrf, gestor_logado, ir, render
from .db import Setor, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
SEM_SETOR = -1   # valor do filtro para "pessoas sem setor"


def setores_da(db: Session, empresa_id: int, so_ativos: bool = True) -> list[Setor]:
    filtro = [Setor.empresa_id == empresa_id]
    if so_ativos:
        filtro.append(Setor.ativo.is_(True))
    return list(db.scalars(select(Setor).where(*filtro).order_by(Setor.ativo.desc(), Setor.nome)))


def ler_filtro(db: Session, empresa_id: int, valor) -> int:
    """Valor do ?setor= conferido: 0 = todos, -1 = sem setor, ou o id de um setor da empresa."""
    try:
        sid = int(valor or 0)
    except (TypeError, ValueError):
        return 0
    if sid in (0, SEM_SETOR):
        return sid
    s = db.get(Setor, sid)
    return sid if s and s.empresa_id == empresa_id else 0


def no_setor(u: Usuario, sid: int) -> bool:
    if not sid:
        return True
    if sid == SEM_SETOR:
        return not u.setor_id
    return u.setor_id == sid


def filtro_sql(sid: int):
    """Condição SQL equivalente a no_setor (None = sem filtro)."""
    if not sid:
        return None
    return Usuario.setor_id.is_(None) if sid == SEM_SETOR else Usuario.setor_id == sid


def nomes(db: Session, empresa_id: int) -> dict[int, str]:
    return {s.id: s.nome for s in db.scalars(select(Setor).where(Setor.empresa_id == empresa_id))}


def descrever_alcance(db: Session, u: Usuario) -> str:
    """Texto curto com os setores/locais de um gerente limitado ("" = vê todos)."""
    from .core import alcance_locais, alcance_setores, restrito
    from .db import Local
    if not restrito(u):
        return ""
    s = [x.nome for x in setores_da(db, u.empresa_id, so_ativos=False) if x.id in alcance_setores(u)]
    l = [u.empresa.nome if i == 0 else (db.get(Local, i).nome if db.get(Local, i) else "")
         for i in sorted(alcance_locais(u))]
    return ", ".join(n for n in s + l if n)


@rota.get("/painel/setores")
def tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    lista = setores_da(db, u.empresa_id, so_ativos=False)
    contagem = dict(db.execute(select(Usuario.setor_id, func.count()).where(
        Usuario.empresa_id == u.empresa_id, Usuario.ativo.is_(True), Usuario.setor_id.is_not(None))
        .group_by(Usuario.setor_id)).all())
    sem = db.scalar(select(func.count()).select_from(Usuario).where(
        Usuario.empresa_id == u.empresa_id, Usuario.ativo.is_(True), Usuario.setor_id.is_(None))) or 0
    return render(request, "painel_setores.html", u=u, e=u.empresa, aba="equipe",
                  setores=lista, contagem=contagem, sem_setor=sem)


def _nome_ok(request: Request, db: Session, empresa_id: int, nome: str, outro: int = 0) -> str | None:
    nome = " ".join(nome.split())[:60]
    if not nome:
        avisar(request, "Dê um nome ao setor (ex.: Cozinha).", "erro")
        return None
    repetido = db.scalar(select(Setor.id).where(
        Setor.empresa_id == empresa_id, func.lower(Setor.nome) == nome.lower(), Setor.id != outro))
    if repetido:
        avisar(request, f"Já existe um setor chamado “{nome}”.", "erro")
        return None
    return nome


@rota.post("/painel/setores", dependencies=CSRF)
def novo(request: Request, nome: str = Form(""), u: Usuario = Depends(gestor_logado),
         db: Session = Depends(get_db)):
    nome = _nome_ok(request, db, u.empresa_id, nome)
    if nome:
        db.add(Setor(empresa_id=u.empresa_id, nome=nome))
        regras.registrar(db, u.empresa_id, u.nome, "setor criado", nome + ".")
        db.commit()
        avisar(request, f"Setor “{nome}” criado. Escolha o setor de cada pessoa na ficha dela.")
    return ir("/painel/setores")


@rota.post("/painel/setores/{sid}", dependencies=CSRF)
def editar(request: Request, sid: int, acao: str = Form("salvar"), nome: str = Form(""),
           u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    s = da_empresa(db, Setor, sid, u)
    pessoas = list(db.scalars(select(Usuario).where(Usuario.empresa_id == u.empresa_id,
                                                    Usuario.setor_id == s.id)))
    if acao == "salvar":
        novo_nome = _nome_ok(request, db, u.empresa_id, nome, s.id)
        if novo_nome and novo_nome != s.nome:
            regras.registrar(db, u.empresa_id, u.nome, "setor renomeado", f"{s.nome} → {novo_nome}.")
            s.nome = novo_nome
            avisar(request, "Setor renomeado.")
    elif acao in ("desativar", "reativar"):
        s.ativo = acao == "reativar"
        regras.registrar(db, u.empresa_id, u.nome, "setor reativado" if s.ativo else "setor desativado",
                         s.nome + ".")
        avisar(request, f"Setor “{s.nome}” reativado." if s.ativo else
               f"Setor “{s.nome}” desativado. As pessoas continuam nele até você trocar na ficha.")
    elif acao == "excluir":
        for p in pessoas:
            p.setor_id = None
        regras.registrar(db, u.empresa_id, u.nome, "setor excluído",
                         f"{s.nome} ({len(pessoas)} pessoa(s) ficaram sem setor).")
        avisar(request, f"Setor “{s.nome}” excluído. {len(pessoas)} pessoa(s) ficaram sem setor."
               if pessoas else f"Setor “{s.nome}” excluído.")
        db.delete(s)
    db.commit()
    return ir("/painel/setores")
