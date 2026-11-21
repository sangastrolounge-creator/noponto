"""Reconhecimento facial no ponto (opção 1: a comparação do rosto é feita no celular).

Como funciona:
- O gestor liga em Configurações e baixa o termo de consentimento (LGPD, art. 11, I).
- Cada pessoa lê o termo no app e aceita ou recusa. Quem recusa continua batendo o ponto
  normalmente, sem prejuízo.
- Quem aceita cadastra o rosto: o celular transforma o rosto num modelo matemático de 128
  números (a foto não sai do aparelho) e só esse modelo vem para o servidor.
- Em cada batida, o celular gera o modelo de novo (com prova de vida: piscar) e o servidor
  compara com o cadastrado. Se não conferir, o ponto é registrado mesmo assim e marcado
  para o gestor conferir. Nada é bloqueado.
- Ao revogar o consentimento, ou no desligamento, o modelo é apagado.
"""
import json
import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import db as banco
from . import i18n, regras
from .core import avisar, da_empresa, exigir_csrf, gestor_logado, ir, render, usuario_logado
from .db import Alerta, Batida, Empresa, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

TAMANHO = 128
LIMIAR = 0.55          # distância máxima entre os dois modelos para dizer que é a mesma pessoa


def ler_descritor(valor) -> list[float] | None:
    """Confere que chegou um modelo de rosto válido: 128 números finitos, de tamanho plausível."""
    try:
        v = [float(x) for x in valor]
    except (TypeError, ValueError):
        return None
    if len(v) != TAMANHO or not all(math.isfinite(x) and abs(x) < 2 for x in v):
        return None
    norma = math.sqrt(sum(x * x for x in v))
    return v if 0.3 < norma < 3 else None


def distancia(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def usa(u: Usuario) -> bool:
    """A pessoa bate o ponto com o rosto (empresa ligou, ela aceitou e cadastrou)."""
    return bool(u.empresa.facial_ativo and u.facial_consentimento == "aceito" and u.facial_descritor)


def pendente(u: Usuario) -> str:
    """O que falta a pessoa fazer: "termo", "cadastro" ou ""."""
    if not u.empresa.facial_ativo:
        return ""
    if u.facial_consentimento == "":
        return "termo"
    if u.facial_consentimento == "aceito" and not u.facial_descritor:
        return "cadastro"
    return ""


def conferir_batida(db: Session, u: Usuario, b: Batida, enviado) -> None:
    """Marca a batida conforme o rosto enviado. Não bloqueia: só avisa o gestor se não conferir."""
    if not usa(u):
        return
    novo = ler_descritor(enviado) if enviado is not None else None
    if not novo:
        b.facial = "sem"
        return
    b.facial_dist = round(distancia(novo, json.loads(u.facial_descritor)), 3)
    b.facial = "ok" if b.facial_dist <= LIMIAR else "diverge"
    if b.facial == "diverge":
        hora = regras.para_local(u.empresa, b.momento).strftime("%H:%M")
        db.add(Alerta(empresa_id=u.empresa_id, usuario_id=u.id, tipo="facial", criado_em=banco.agora(),
                      texto=f"O rosto não conferiu na batida de {u.nome} às {hora}. Confira em Ajuste de ponto."))


def apagar(u: Usuario):
    u.facial_descritor = ""


def resumo_equipe(db: Session, e: Empresa) -> dict:
    linhas = db.execute(select(Usuario.facial_consentimento, Usuario.facial_descritor != "", func.count())
                        .where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.papel == "funcionario")
                        .group_by(Usuario.facial_consentimento, Usuario.facial_descritor != "")).all()
    r = {"cadastrados": 0, "aceitaram": 0, "recusaram": 0, "pendentes": 0}
    for cons, tem, n in linhas:
        if cons == "aceito":
            r["aceitaram"] += n
            r["cadastrados"] += n if tem else 0
        elif cons == "recusado":
            r["recusaram"] += n
        else:
            r["pendentes"] += n
    return r


# ---------------------------------------------------------------- gestor
@rota.post("/painel/config/facial", dependencies=CSRF)
def config_facial(request: Request, ativo: str = Form(""), u: Usuario = Depends(gestor_logado),
                  db: Session = Depends(get_db)):
    e = u.empresa
    e.facial_ativo = bool(ativo)
    regras.registrar(db, e.id, u.nome, "reconhecimento facial",
                     "Ligado no ponto." if e.facial_ativo else "Desligado no ponto.")
    db.commit()
    avisar(request, "Reconhecimento facial ligado. Cada pessoa vai ler o termo no app e escolher se aceita."
           if e.facial_ativo else "Reconhecimento facial desligado.")
    return ir("/painel/config#facial")


@rota.get("/painel/config/termo-facial")
def termo_gestor(request: Request, u: Usuario = Depends(gestor_logado)):
    return render(request, "termo_facial.html", u=u, e=u.empresa, pessoa=None, imprimir=True,
                  volta="/painel/config#facial")


@rota.post("/painel/equipe/{fid}/facial-refazer", dependencies=CSRF)
def refazer(request: Request, fid: int, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, fid, u)
    apagar(f)
    regras.registrar(db, u.empresa_id, u.nome, "reconhecimento facial",
                     f"Cadastro do rosto de {f.nome} apagado para ser refeito.", usuario_id=f.id)
    db.commit()
    avisar(request, f"O cadastro do rosto de {f.primeiro_nome} foi apagado. Na próxima vez que abrir o app, "
                    "ele(a) cadastra de novo.")
    return ir(f"/painel/equipe#p{f.id}")


# ---------------------------------------------------------------- funcionário
@rota.get("/app/facial")
def app_facial(request: Request, u: Usuario = Depends(usuario_logado)):
    if not u.empresa.facial_ativo:
        return ir("/app")
    return render(request, "app_facial.html", u=u, e=u.empresa, aba="hoje", pessoa=u)


@rota.get("/app/facial/termo")
def termo_app(request: Request, u: Usuario = Depends(usuario_logado)):
    return render(request, "termo_facial.html", u=u, e=u.empresa, pessoa=u, imprimir=True, volta="/app/facial")


@rota.post("/app/facial/consentimento", dependencies=CSRF)
def consentimento(request: Request, escolha: str = Form(""), u: Usuario = Depends(usuario_logado),
                  db: Session = Depends(get_db)):
    if not u.empresa.facial_ativo or escolha not in ("aceito", "recusado"):
        return ir("/app")
    u.facial_consentimento, u.facial_consentido_em = escolha, banco.agora()
    if escolha == "recusado":
        apagar(u)
    regras.registrar(db, u.empresa_id, u.nome, "reconhecimento facial",
                     f"{u.nome} aceitou o termo de reconhecimento facial." if escolha == "aceito"
                     else f"{u.nome} recusou o termo de reconhecimento facial.", usuario_id=u.id)
    db.commit()
    if escolha == "recusado":
        avisar(request, "Tudo certo: você continua batendo o ponto como antes, sem o rosto.")
        return ir("/app")
    return ir("/app/facial")


@rota.post("/app/facial/cadastro", dependencies=CSRF)
async def cadastro(request: Request, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    if not u.empresa.facial_ativo or u.facial_consentimento != "aceito":
        return i18n.resposta_erro({"erro": "Aceite o termo antes de cadastrar o rosto."}, 400)
    try:
        dados = await request.json()
    except Exception:
        dados = {}
    v = ler_descritor(dados.get("descritor"))
    if not v:
        return i18n.resposta_erro({"erro": "Não consegui ler o rosto. Tente de novo num lugar claro."}, 400)
    u.facial_descritor = json.dumps([round(x, 6) for x in v])
    regras.registrar(db, u.empresa_id, u.nome, "reconhecimento facial", f"{u.nome} cadastrou o rosto.",
                     usuario_id=u.id)
    db.commit()
    return JSONResponse({"ok": True})


@rota.post("/app/facial/testar", dependencies=CSRF)
async def testar(request: Request, u: Usuario = Depends(usuario_logado)):
    """Confere o rosto sem bater o ponto (para a pessoa testar depois de cadastrar)."""
    if not usa(u):
        return i18n.resposta_erro({"erro": "Cadastre o rosto antes de testar."}, 400)
    try:
        dados = await request.json()
    except Exception:
        dados = {}
    v = ler_descritor(dados.get("descritor"))
    if not v:
        return i18n.resposta_erro({"erro": "Não consegui ler o rosto. Tente de novo num lugar claro."}, 400)
    d = distancia(v, json.loads(u.facial_descritor))
    return JSONResponse({"confere": d <= LIMIAR})


@rota.post("/app/facial/revogar", dependencies=CSRF)
def revogar(request: Request, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    apagar(u)
    u.facial_consentimento, u.facial_consentido_em = "recusado", banco.agora()
    regras.registrar(db, u.empresa_id, u.nome, "reconhecimento facial",
                     f"{u.nome} revogou o consentimento; o cadastro do rosto foi apagado.", usuario_id=u.id)
    db.commit()
    avisar(request, "Consentimento revogado e cadastro do rosto apagado. Você continua batendo o ponto normalmente.")
    return ir("/app")
