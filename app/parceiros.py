"""Programa de parceiros (contadores e revendas) e marca própria (white-label).

O parceiro tem login próprio (fora das empresas), um link /p/<código> e ganha comissão sobre o que
as empresas dele pagam. O nível sobe sozinho com o número de clientes pagantes:
Indicador 15% · Parceiro 20% · Parceiro Pro 25% · Revenda 35% · Marca Própria 45%.
Com a permissão da empresa, o parceiro baixa o resumo de horas do mês (para fazer a folha).
Marca própria: liberada pelo master; no domínio do parceiro as telas mostram o nome, a logo e a
cor dele, e as empresas que se cadastram ali ficam ligadas a ele.
"""
import csv
import io
import re
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import (Redirecionar, admin_logado, avisar, celular_limpo, conferir_senha,
                   dentro_do_limite, email_valido, exigir_csrf, gerar_hash, gestor_logado, ip, ir,
                   log, render, situacao)
from .db import Comissao, Empresa, Pagamento, Parceiro, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

# (nível, nome, clientes pagantes a partir de, % de comissão)
NIVEIS = [(1, "Indicador", 0, 15), (2, "Parceiro", 4, 20), (3, "Parceiro Pro", 11, 25),
          (4, "Revenda", 31, 35), (5, "Marca Própria", 101, 45)]
PASTA_MARCA = banco.PASTA_DADOS / "marcas"
NOME_LOGO = re.compile(r"^p[0-9]+-[0-9a-f]{12}\.(jpg|png|webp)$")
DOMINIO = re.compile(r"^(?=.{4,120}$)([a-z0-9](-?[a-z0-9])*\.)+[a-z]{2,}$")


# ------------------------------------------------------------------ níveis e comissões
def clientes_pagantes(db: Session, par: Parceiro) -> list[Empresa]:
    return [e for e in db.scalars(select(Empresa).where(Empresa.parceiro_id == par.id))
            if situacao(e) == "ativa"]


def nivel(db: Session, par: Parceiro, n: int | None = None) -> tuple[int, str, int, int]:
    """(nível, nome, mínimo de clientes, %)."""
    if par.nivel_manual:
        return NIVEIS[max(1, min(5, par.nivel_manual)) - 1]
    n = len(clientes_pagantes(db, par)) if n is None else n
    return [x for x in NIVEIS if n >= x[2]][-1]


def proximo_nivel(atual: tuple) -> tuple | None:
    return NIVEIS[atual[0]] if atual[0] < len(NIVEIS) else None


def registrar_comissao(db: Session, e: Empresa, reg: Pagamento):
    """Pagamento confirmado de uma empresa de parceiro: guarda a comissão (uma por pagamento)."""
    if not e.parceiro_id or not reg.id or (reg.valor_centavos or 0) <= 0:
        return
    par = db.get(Parceiro, e.parceiro_id)
    if not par or not par.ativo or db.scalar(select(Comissao.id).where(Comissao.pagamento_id == reg.id)):
        return
    pct = nivel(db, par)[3]
    db.add(Comissao(parceiro_id=par.id, empresa_id=e.id, pagamento_id=reg.id,
                    valor_pago_centavos=reg.valor_centavos, pct=pct,
                    valor_centavos=round(reg.valor_centavos * pct / 100), criado_em=banco.agora()))
    db.commit()


def estornar(db: Session, reg: Pagamento):
    """Pagamento estornado: a comissão ainda não paga é cancelada."""
    c = db.scalar(select(Comissao).where(Comissao.pagamento_id == reg.id))
    if c and c.status == "a_pagar":
        c.status = "cancelada"


def totais(db: Session, par_id: int) -> dict:
    linhas = dict(db.execute(select(Comissao.status, func.sum(Comissao.valor_centavos)).where(
        Comissao.parceiro_id == par_id).group_by(Comissao.status)).all())
    return {"a_pagar": linhas.get("a_pagar") or 0, "paga": linhas.get("paga") or 0}


# ------------------------------------------------------------------ visita e marca própria
_dominios: dict[str, dict] | None = None


def marcas_mudaram():
    global _dominios
    _dominios = None


def _host(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].strip().lower()


def marca_do_host(request: Request) -> dict | None:
    """Marca própria do parceiro quando o acesso vem pelo domínio dele (com cache)."""
    global _dominios
    if _dominios is None:
        with SessionLocal() as db:
            _dominios = {p.dominio: {"id": p.id, "nome": p.marca_nome or p.escritorio or p.nome,
                                     "logo": p.marca_logo, "cor": p.marca_cor}
                         for p in db.scalars(select(Parceiro).where(
                             Parceiro.dominio != "", Parceiro.marca_liberada.is_(True),
                             Parceiro.ativo.is_(True)))}
    h = _host(request)
    return _dominios.get(h) or _dominios.get(h.removeprefix("www."))


def parceiro_da_visita(request: Request, db: Session) -> Parceiro | None:
    """Parceiro do link /p/<código> (nesta sessão) ou do domínio da marca própria."""
    m = marca_do_host(request)
    if m:
        return db.get(Parceiro, m["id"])
    cod = request.session.get("parceiro") or ""
    if cod:
        par = db.scalar(select(Parceiro).where(Parceiro.codigo == cod, Parceiro.ativo.is_(True)))
        if par:
            return par
    return None


@rota.get("/p/{codigo}")
def link_parceiro(codigo: str, request: Request):
    request.session["parceiro"] = re.sub(r"[^a-z0-9]", "", codigo.lower())[:12]
    return ir("/cadastro")


@rota.get("/marca-logo/{nome}")
def logo_marca(nome: str):
    if not NOME_LOGO.match(nome) or not (PASTA_MARCA / nome).is_file():
        return Response(status_code=404)
    return FileResponse(PASTA_MARCA / nome, headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------ login do parceiro
def parceiro_logado(request: Request, db: Session = Depends(get_db)) -> Parceiro:
    pid = request.session.get("parceiro_uid")
    par = db.get(Parceiro, pid) if pid else None
    if not par or not par.ativo:
        request.session.pop("parceiro_uid", None)
        raise Redirecionar("/parceiros/entrar")
    return par


def _novo_codigo(db: Session) -> str:
    while True:
        c = secrets.token_hex(4)[:7]
        if not db.scalar(select(Parceiro.id).where(Parceiro.codigo == c)):
            return c


@rota.get("/parceiros")
def pagina_parceiros(request: Request):
    return render(request, "parceiros.html", niveis=NIVEIS, erro=None, v={})


@rota.post("/parceiros", dependencies=CSRF)
def cadastrar_parceiro(request: Request, nome: str = Form(""), escritorio: str = Form(""),
                       email: str = Form(""), celular: str = Form(""), senha: str = Form(""),
                       aceito: str = Form(""), db: Session = Depends(get_db)):
    nome, escritorio = " ".join(nome.split())[:120], " ".join(escritorio.split())[:120]
    email, cel = email.strip().lower()[:200], celular_limpo(celular)
    erro = None
    if not dentro_do_limite(f"parc-cad:{ip(request)}", 5, 3600):
        erro = "Muitas tentativas. Aguarde um pouco."
    elif not nome:
        erro = "Informe o seu nome."
    elif not email_valido(email):
        erro = "E-mail inválido."
    elif len(senha) < 8:
        erro = "A senha precisa ter pelo menos 8 caracteres."
    elif db.scalar(select(Parceiro.id).where(Parceiro.email == email)):
        erro = "Já existe um parceiro com este e-mail. Entre com a sua senha."
    elif not aceito:
        erro = "Aceite as regras do programa de parceiros para continuar."
    if erro:
        return render(request, "parceiros.html", 400, niveis=NIVEIS, erro=erro,
                      v={"nome": nome, "escritorio": escritorio, "email": email, "celular": celular})
    par = Parceiro(nome=nome, escritorio=escritorio, email=email, celular=cel,
                   senha_hash=gerar_hash(senha), codigo=_novo_codigo(db), aceite_em=banco.agora())
    db.add(par)
    db.commit()
    log.info("Novo parceiro %s (%s)", par.id, email)
    request.session["parceiro_uid"] = par.id
    avisar(request, "Cadastro feito. Este é o seu painel: copie o seu link e mande para os seus clientes.")
    return ir("/parceiro")


@rota.get("/parceiros/entrar")
def entrar_form(request: Request):
    return render(request, "parceiro_entrar.html", erro=None, email="")


@rota.post("/parceiros/entrar", dependencies=CSRF)
def entrar(request: Request, email: str = Form(""), senha: str = Form(""), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not dentro_do_limite(f"parc-login:{ip(request)}", 10, 600):
        return render(request, "parceiro_entrar.html", 429, email=email,
                      erro="Muitas tentativas. Aguarde alguns minutos.")
    par = db.scalar(select(Parceiro).where(Parceiro.email == email))
    if not par or not par.ativo or not conferir_senha(senha, par.senha_hash):
        return render(request, "parceiro_entrar.html", 400, email=email,
                      erro="Dados incorretos. Confira e tente de novo.")
    request.session["parceiro_uid"] = par.id
    return ir("/parceiro")


@rota.post("/parceiros/sair", dependencies=CSRF)
def sair(request: Request):
    request.session.pop("parceiro_uid", None)
    return ir("/parceiros/entrar")


# ------------------------------------------------------------------ painel do parceiro
@rota.get("/parceiro")
def painel(request: Request, par: Parceiro = Depends(parceiro_logado), db: Session = Depends(get_db)):
    empresas = list(db.scalars(select(Empresa).where(Empresa.parceiro_id == par.id)
                               .order_by(Empresa.criado_em.desc())))
    pagantes = [e for e in empresas if situacao(e) == "ativa"]
    atual = nivel(db, par, len(pagantes))
    comissoes = list(db.scalars(select(Comissao).where(Comissao.parceiro_id == par.id)
                                .order_by(Comissao.criado_em.desc()).limit(60)))
    nomes = {e.id: e.nome for e in empresas}
    hoje = banco.agora().date()
    mes_passado = (hoje.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return render(request, "parceiro_painel.html", par=par, empresas=empresas, pagantes=len(pagantes),
                  nivel=atual, proximo=proximo_nivel(atual), comissoes=comissoes, nomes=nomes,
                  totais=totais(db, par.id), mes=mes_passado, niveis=NIVEIS,
                  link=f"{str(request.base_url).rstrip('/')}/p/{par.codigo}",
                  situacao_de=situacao)


@rota.post("/parceiro/dados", dependencies=CSRF)
def salvar_dados(request: Request, nome: str = Form(""), escritorio: str = Form(""),
                 celular: str = Form(""), cpf_cnpj: str = Form(""), chave_pix: str = Form(""),
                 par: Parceiro = Depends(parceiro_logado), db: Session = Depends(get_db)):
    if nome.strip():
        par.nome = " ".join(nome.split())[:120]
    par.escritorio = " ".join(escritorio.split())[:120]
    par.celular = celular_limpo(celular)
    par.cpf_cnpj = re.sub(r"\D", "", cpf_cnpj)[:14]
    par.chave_pix = chave_pix.strip()[:120]
    db.commit()
    avisar(request, "Dados salvos.")
    return ir("/parceiro#dados")


@rota.get("/parceiro/empresa/{empresa_id}/horas.csv")
def horas_csv(empresa_id: int, mes: str = "", par: Parceiro = Depends(parceiro_logado),
              db: Session = Depends(get_db)):
    """Resumo de horas do mês por funcionário (para a folha), se a empresa permitiu."""
    from . import relatorios
    e = db.get(Empresa, empresa_id)
    if not e or e.parceiro_id != par.id or not e.contador_acesso:
        return Response("Esta empresa não permitiu o acesso aos relatórios.", status_code=403)
    try:
        ini = date.fromisoformat((mes or "")[:7] + "-01")
    except ValueError:
        ini = (banco.agora().date().replace(day=1) - timedelta(days=1)).replace(day=1)
    fim = (ini.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    r = relatorios.montar(db, e, ini, fim)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")

    def h(m):
        return str(round(m / 60, 2)).replace(".", ",")
    w.writerow(["Funcionário", "Cargo", "Turnos", "Horas na escala", "Horas trabalhadas", "Diferença",
                "Atrasos", "Faltas", "Horas noturnas"])
    for x in r["linhas"]:
        w.writerow([x["u"].nome, x["u"].cargo or "", x["turnos"], h(x["escala"]), h(x["trab"]),
                    h(x["dif"]), x["atrasos"], x["faltas"], h(x["noturno"])])
    regras.registrar(db, e.id, f"contador {par.nome}"[:120], "relatório baixado",
                     f"O parceiro {par.nome} baixou o resumo de horas de {ini.strftime('%m/%Y')}.")
    db.commit()
    nome = re.sub(r"[^a-z0-9]+", "-", e.nome.lower()).strip("-")[:40] or "empresa"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="horas-{nome}-{ini.strftime("%Y-%m")}.csv"'})


@rota.post("/parceiro/marca", dependencies=CSRF)
def salvar_marca(request: Request, marca_nome: str = Form(""), marca_cor: str = Form(""),
                 dominio: str = Form(""), arquivo: UploadFile | None = File(None),
                 par: Parceiro = Depends(parceiro_logado), db: Session = Depends(get_db)):
    from .logo_empresa import tipo_da_imagem
    if not par.marca_liberada:
        avisar(request, "A marca própria é liberada no nível Marca Própria. Fale com a gente.", "erro")
        return ir("/parceiro#marca")
    dominio = dominio.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    if dominio and not DOMINIO.match(dominio):
        avisar(request, "Domínio inválido. Exemplo: ponto.seuescritorio.com.br", "erro")
        return ir("/parceiro#marca")
    if dominio and db.scalar(select(Parceiro.id).where(Parceiro.dominio == dominio, Parceiro.id != par.id)):
        avisar(request, "Este domínio já está em uso.", "erro")
        return ir("/parceiro#marca")
    cor = marca_cor.strip().lower()
    par.marca_nome = " ".join(marca_nome.split())[:60]
    par.marca_cor = cor if re.fullmatch(r"#[0-9a-f]{6}", cor) else ""
    par.dominio = dominio
    dados = arquivo.file.read(1024 * 1024 + 1) if arquivo and arquivo.filename else b""
    if dados:
        ext = tipo_da_imagem(dados)
        if len(dados) > 1024 * 1024 or not ext:
            avisar(request, "A logo precisa ser JPG, PNG ou WebP com até 1 MB.", "erro")
            return ir("/parceiro#marca")
        PASTA_MARCA.mkdir(parents=True, exist_ok=True)
        if par.marca_logo and NOME_LOGO.match(par.marca_logo):
            (PASTA_MARCA / par.marca_logo).unlink(missing_ok=True)
        par.marca_logo = f"p{par.id}-{secrets.token_hex(6)}.{ext}"
        (PASTA_MARCA / par.marca_logo).write_bytes(dados)
    db.commit()
    marcas_mudaram()
    avisar(request, "Marca salva." + (" Agora aponte o domínio para o nosso servidor (veja abaixo)." if dominio else ""))
    return ir("/parceiro#marca")


# ------------------------------------------------------------------ empresa: contador
@rota.post("/painel/config/contador", dependencies=CSRF)
def contador_empresa(request: Request, codigo: str = Form(""), acesso: str = Form(""),
                     desligar: str = Form(""), u: Usuario = Depends(gestor_logado),
                     db: Session = Depends(get_db)):
    e = u.empresa
    if desligar:
        e.contador_acesso = False
        regras.registrar(db, e.id, u.nome, "contador", "Acesso do contador aos relatórios desligado.")
    elif not e.parceiro_id:
        cod = re.sub(r"[^a-z0-9]", "", codigo.lower())[:12]
        par = db.scalar(select(Parceiro).where(Parceiro.codigo == cod, Parceiro.ativo.is_(True))) if cod else None
        if not par:
            avisar(request, "Código de parceiro não encontrado. Peça o código ao seu contador.", "erro")
            return ir("/painel/config#contador")
        e.parceiro_id, e.contador_acesso = par.id, bool(acesso)
        regras.registrar(db, e.id, u.nome, "contador", f"Empresa ligada ao parceiro {par.escritorio or par.nome}.")
    else:
        e.contador_acesso = bool(acesso)
        regras.registrar(db, e.id, u.nome, "contador",
                         "Acesso do contador aos relatórios " + ("ligado." if e.contador_acesso else "desligado."))
    db.commit()
    avisar(request, "Pronto.")
    return ir("/painel/config#contador")


def contador_da_empresa(db: Session, e: Empresa) -> Parceiro | None:
    return db.get(Parceiro, e.parceiro_id) if e.parceiro_id else None


# ------------------------------------------------------------------ master
@rota.get("/master/parceiros")
def master_parceiros(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    lista = []
    for par in db.scalars(select(Parceiro).order_by(Parceiro.criado_em.desc())):
        empresas = list(db.scalars(select(Empresa).where(Empresa.parceiro_id == par.id)))
        pag = [e for e in empresas if situacao(e) == "ativa"]
        lista.append({"p": par, "empresas": len(empresas), "pagantes": len(pag),
                      "nivel": nivel(db, par, len(pag)), "totais": totais(db, par.id)})
    return render(request, "master/parceiros.html", u=u, e=u.empresa, aba="master", sub="parceiros",
                  lista=lista, niveis=NIVEIS)


@rota.post("/master/parceiros/{parceiro_id}", dependencies=CSRF)
def master_parceiro_salvar(parceiro_id: int, request: Request, ativo: str = Form(""),
                           nivel_manual: int = Form(0), marca_liberada: str = Form(""),
                           dominio: str = Form(""), u: Usuario = Depends(admin_logado),
                           db: Session = Depends(get_db)):
    par = db.get(Parceiro, parceiro_id)
    if not par:
        return ir("/master/parceiros")
    dominio = dominio.strip().lower()
    if dominio and not DOMINIO.match(dominio):
        avisar(request, "Domínio inválido.", "erro")
        return ir("/master/parceiros")
    par.ativo, par.marca_liberada = bool(ativo), bool(marca_liberada)
    par.nivel_manual = max(0, min(5, nivel_manual))
    par.dominio = dominio
    db.commit()
    marcas_mudaram()
    avisar(request, f"Parceiro {par.nome} salvo.")
    return ir("/master/parceiros")


@rota.post("/master/parceiros/{parceiro_id}/pagar", dependencies=CSRF)
def master_pagar_comissoes(parceiro_id: int, request: Request, u: Usuario = Depends(admin_logado),
                           db: Session = Depends(get_db)):
    """Marca como pagas as comissões a pagar (o master faz o Pix por fora)."""
    agora = banco.agora()
    total = 0
    for c in db.scalars(select(Comissao).where(Comissao.parceiro_id == parceiro_id,
                                               Comissao.status == "a_pagar")):
        c.status, c.paga_em = "paga", agora
        total += c.valor_centavos
    db.commit()
    avisar(request, f"Comissões marcadas como pagas: R$ {total / 100:.2f}".replace(".", ","))
    return ir("/master/parceiros")
