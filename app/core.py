"""Configuração, templates, sessão/CSRF, login e funções de apoio."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from pathlib import Path

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import db as banco
from . import i18n
from .db import Config, Empresa, Plano, Plataforma, SessionLocal, Usuario, criar_tabelas, get_db

log = logging.getLogger("turno")

APP_NOME = os.environ.get("APP_NOME", "").strip() or "Turno+"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
CADASTRO_ABERTO = os.environ.get("CADASTRO_ABERTO", "true").lower() == "true"
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    log.warning("SECRET_KEY não definida: os logins caem a cada reinício.")

AQUI = Path(__file__).parent
templates = Jinja2Templates(directory=AQUI / "templates")

criar_tabelas()

# ---------- revenda: plataforma e assinatura ----------
CARENCIA_DIAS = 3  # padrão; o dono da plataforma ajusta em Master -> Plataforma
SITUACOES = i18n.Traduzido({"ativa": "Ativa", "teste": "Em teste", "gratis": "Grátis",
                             "cortesia": "Cortesia", "vencida": "Vencida",
                             "bloqueada": "Bloqueada"})
_plat_cache: dict | None = None


def plataforma() -> dict:
    """Configuração da plataforma (nome, suporte, teste...), com cache em memória."""
    global _plat_cache
    if _plat_cache is None:
        with SessionLocal() as db:
            p = db.get(Plataforma, 1)
            _plat_cache = {c.name: getattr(p, c.name) for c in Plataforma.__table__.columns}
            _plat_cache["nome"] = _plat_cache["nome"] or APP_NOME
    return _plat_cache


def plataforma_mudou():
    global _plat_cache, _google_cache
    _plat_cache = None
    _google_cache = None


_google_cache: tuple[str, str] | None = None


def google_credenciais() -> tuple[str, str]:
    """(ID do cliente, chave secreta) do login com o Google. As salvas no Painel Master valem
    mais que as do .env (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET). ("", "") = desligado."""
    global _google_cache
    if _google_cache is None:
        with SessionLocal() as db:
            gid, sec = db.get(Config, "google_client_id"), db.get(Config, "google_client_secret")
            gid, sec = (gid.valor if gid else ""), (sec.valor if sec else "")
        if not (gid and sec):
            gid, sec = os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", "")
        _google_cache = (gid.strip(), sec.strip())
    return _google_cache


def google_ligado() -> bool:
    gid, sec = google_credenciais()
    return bool(gid and sec)


def _carencia(campo: str) -> int:
    v = plataforma().get(campo)
    return CARENCIA_DIAS if v is None and campo == "carencia_dias" else int(v or 0)


def fim_da_carencia(e: Empresa) -> datetime | None:
    """Se a empresa já passou do vencimento (ou do fim do teste) e está usando a carência,
    devolve até quando ela vale. Senão, None."""
    agora = banco.agora()
    s = situacao(e)
    if s == "ativa" and e.pago_ate and e.pago_ate < agora:
        return e.pago_ate + timedelta(days=_carencia("carencia_dias"))
    if s == "teste" and e.teste_ate and e.teste_ate < agora:
        return e.teste_ate + timedelta(days=_carencia("carencia_teste_dias"))
    return None


def situacao(e: Empresa) -> str:
    if not e.ativo:
        return "bloqueada"
    if e.isenta:
        return "cortesia"
    hoje = banco.agora()
    if e.pago_ate and e.pago_ate + timedelta(days=_carencia("carencia_dias")) >= hoje:
        return "ativa"
    if e.teste_ate and e.teste_ate + timedelta(days=_carencia("carencia_teste_dias")) >= hoje:
        return "teste"
    if e.plano and e.plano.gratis:  # plano grátis: nunca vence, com os limites do plano
        return "gratis"
    return "vencida"


def pode_usar(e: Empresa) -> bool:
    return situacao(e) in ("ativa", "teste", "cortesia", "gratis")


def pagamento_liberado(e: Empresa) -> bool:
    """Tela Pagamento e revisão do Claude: no teste vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or e.plano.com_pagamento


def alertas_liberados(e: Empresa) -> bool:
    """Alertas de atraso e de não comparecimento: no teste vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or e.plano.com_alertas


def cerca_liberada(e: Empresa) -> bool:
    """Cerca desenhada no mapa e conferência no turno: no teste vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or e.plano.com_cerca


def gps_liberado(e: Empresa) -> bool:
    """Ponto conferido pela localização: no teste vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or e.plano.com_gps


def foto_liberada(e: Empresa) -> bool:
    """Foto ao bater o ponto: no teste vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or e.plano.com_foto


def facial_liberado(e: Empresa) -> bool:
    """Reconhecimento facial no ponto: no teste vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or e.plano.com_facial


# Recursos ligados ao plano (além dos de cima): chave do plano -> nome para o aviso.
RECURSOS = {"com_escala": "a escala", "com_fechamento": "o fechamento, o banco de horas e os relatórios",
            "com_rh": "os dados de RH, as férias e o desligamento", "com_totem": "o totem (ponto no tablet)",
            "com_gerentes": "gerentes e setores", "com_cmo": "o faturamento e o custo de mão de obra"}
# Caminho do painel -> recurso do plano necessário.
RECURSO_ROTAS = (
    ("/painel/escala", "com_escala"), ("/painel/ofertas", "com_escala"),
    ("/painel/fechamento", "com_fechamento"), ("/painel/folha", "com_fechamento"),
    ("/painel/relatorios", "com_fechamento"), ("/painel/banco", "com_fechamento"),
    ("/painel/ferias", "com_rh"), ("/painel/totens", "com_totem"),
    ("/painel/gerentes", "com_gerentes"), ("/painel/setores", "com_gerentes"),
    ("/painel/faturamento", "com_cmo"), ("/painel/vendas", "com_cmo"), ("/painel/config/faturamento", "com_cmo"),
)


def recurso_liberado(e: Empresa, chave: str) -> bool:
    """Recurso do plano: no teste e na cortesia vale tudo; depois, conforme o plano."""
    return situacao(e) in ("teste", "cortesia") or not e.plano or bool(getattr(e.plano, chave, True))


def recurso_da_rota(caminho: str) -> str | None:
    if caminho.startswith("/painel/equipe/") and caminho.endswith("/desligar"):
        return "com_rh"
    for prefixo, chave in RECURSO_ROTAS:
        if caminho == prefixo or caminho.startswith(prefixo + "/") or caminho.startswith(prefixo + "."):
            return chave
    return None


def exige_foto(e: Empresa) -> bool:
    """A empresa ligou a foto e o plano dela inclui."""
    return bool(e.exigir_foto) and foto_liberada(e)


def limite_locais(e: Empresa) -> int:
    """Quantos locais de ponto a empresa pode ter, contando o principal. 0 = ilimitado."""
    return 0 if e.isenta or not e.plano else e.plano.limite_locais


def validade(e: Empresa) -> datetime | None:
    s = situacao(e)
    if s == "ativa":
        return e.pago_ate
    if s == "teste":
        return e.teste_ate
    if s == "vencida":
        return e.pago_ate or e.teste_ate
    return None


def dias_restantes(e: Empresa) -> int | None:
    v = validade(e)
    if not v or situacao(e) not in ("ativa", "teste"):
        return None
    # Arredonda para cima: logo depois do cadastro de um teste de 7 dias, restam 7 (não 6).
    return max(0, -(-int((v - banco.agora()).total_seconds()) // 86400))


def limite_do_plano(p: Plano) -> int:
    """Funcionários que o plano aceita, com a tolerância. Plano de um grupo (faixas): vale a maior
    faixa à venda, porque a empresa muda de faixa sozinha. 0 = ilimitado."""
    if not p.grupo:
        return p.limite_efetivo
    from sqlalchemy import select
    from sqlalchemy.orm import object_session
    s = object_session(p)
    faixas = [p] + (list(s.scalars(select(Plano).where(Plano.grupo == p.grupo, Plano.ativo.is_(True))))
                    if s is not None else [])
    limites = [f.limite_efetivo for f in faixas]
    return 0 if 0 in limites else max(limites)


def limite_funcionarios(e: Empresa) -> int:
    """0 = ilimitado (cortesia e empresa sem plano também)."""
    return 0 if e.isenta or not e.plano else limite_do_plano(e.plano)


def funcionarios_ativos(db: Session, empresa_id: int) -> int:
    from sqlalchemy import func, select
    return db.scalar(select(func.count()).select_from(Usuario).where(
        Usuario.empresa_id == empresa_id, Usuario.papel == "funcionario",
        Usuario.ativo.is_(True)).execution_options(alcance_livre=True)) or 0


def dinheiro(centavos: int | None) -> str:
    c = int(centavos or 0)
    if c < 0:
        return "−" + dinheiro(-c)
    return f"R$ {c // 100:,}".replace(",", ".") + f",{c % 100:02d}"


def ler_centavos(txt: str) -> int | None:
    """'49,90', 'R$ 1.234,50' ou '49.90' -> centavos. None se não for um valor."""
    t = (txt or "").replace("R$", "").replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return int(v * 100) if 0 <= v < 1_000_000 else None


def data_br(dt: datetime | None, formato: str = "%d/%m/%Y %H:%M") -> str:
    """Data em horário de Brasília (as telas do master não têm uma empresa de referência)."""
    return (dt - timedelta(hours=3)).strftime(formato) if dt else ""


templates.env.globals.update(situacao=situacao, SITUACOES=SITUACOES, validade=validade,
                             fim_da_carencia=fim_da_carencia,
                             dias_restantes=dias_restantes, _=i18n._, idioma=i18n.idioma,
                             IDIOMAS=i18n.IDIOMAS, caminho_atual=i18n.caminho_atual,
                             textos_js=i18n.textos_js, recurso_liberado=recurso_liberado)
templates.env.filters.update(traduz=i18n._)
templates.env.filters.update(dinheiro=dinheiro, data=data_br, fromjson=json.loads)


class Redirecionar(Exception):
    def __init__(self, url: str):
        self.url = url


# ---------- senha (scrypt, da biblioteca padrão) ----------
def gerar_hash(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode(), salt=sal, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${sal.hex()}${h.hex()}"


def conferir_senha(senha: str, guardado: str | None) -> bool:
    try:
        _, sal_hex, h_hex = (guardado or "").split("$")
        h = hashlib.scrypt(senha.encode(), salt=bytes.fromhex(sal_hex),
                           n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


# ---------- texto ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def email_valido(e: str) -> bool:
    return bool(EMAIL_RE.match(e or "")) and len(e) <= 200


def celular_limpo(tel: str) -> str:
    """Só dígitos, com DDD e sem o 55 do país."""
    d = re.sub(r"\D", "", tel or "")
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    return d


def celular_valido(d: str) -> bool:
    return len(d) in (10, 11)


# ---------- limite de tentativas (em memória; basta para 1 processo) ----------
_janelas: dict[str, deque] = defaultdict(deque)


def dentro_do_limite(chave: str, maximo: int, segundos: int) -> bool:
    agora = time.monotonic()
    fila = _janelas[chave]
    while fila and agora - fila[0] > segundos:
        fila.popleft()
    if len(fila) >= maximo:
        return False
    fila.append(agora)
    if len(_janelas) > 50_000:
        _janelas.clear()
    return True


def ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


# ---------- sessão, CSRF, avisos ----------
def csrf_token(request: Request) -> str:
    t = request.session.get("csrf")
    if not t:
        t = request.session["csrf"] = secrets.token_urlsafe(24)
    return t


async def exigir_csrf(request: Request):
    enviado = request.headers.get("x-csrf")
    if not enviado:
        enviado = (await request.form()).get("csrf")
    esperado = request.session.get("csrf")
    if not esperado or not enviado or not hmac.compare_digest(str(enviado), esperado):
        raise HTTPException(403, "Sessão expirada. Volte, recarregue a página e tente de novo.")


def avisar(request: Request, texto: str, tipo: str = "ok"):
    request.session["flash"] = {"texto": i18n._(texto), "tipo": tipo}


_marca_atual: ContextVar = ContextVar("marca_atual", default=None)


def marca_atual() -> dict | None:
    """Marca própria do parceiro na tela que está sendo montada (domínio dele) ou None."""
    return _marca_atual.get()


templates.env.globals["marca_atual"] = marca_atual


APP_IOS = "DataWorkeIOS"


def app_ios(request: Request) -> bool:
    """O app do iPhone se identifica no User-Agent. Nele não há login do Google, cadastro de empresa
    nem assinatura (regras 3.1.1 e 4.8 da App Store): a empresa contrata pelo site."""
    return APP_IOS in request.headers.get("user-agent", "")


def render(request: Request, nome: str, status_code: int = 200, **ctx):
    from . import parceiros
    plat = plataforma()
    marca = parceiros.marca_do_host(request)
    _marca_atual.set(marca)
    if marca:   # domínio de um parceiro com marca própria: nome e cor dele, sem login do Google
        plat = {**plat, "nome": marca["nome"], "nome_curto": marca["nome"],
                "entrada_cor1": marca["cor"] or plat["entrada_cor1"],
                "entrada_cor2": marca["cor"] or plat["entrada_cor2"]}
    base = {"app_nome": plat["nome"], "csrf": csrf_token(request), "plat": plat,
            "flash": request.session.pop("flash", None),
            "cadastro_aberto": plat["cadastro_aberto"],
            "google_ok": google_ligado() and not marca, "marca": marca,
            "ent_imagens": [n for n in (plat.get("entrada_imagens") or "").split(",") if n],
            "suporte": bool(request.session.get("master_uid")),
            "app_ios": app_ios(request)}
    if base["app_ios"]:
        base["google_ok"] = base["cadastro_aberto"] = False
    if nome == "cadastro.html":   # cupom e parceiro que vieram pelo link
        with SessionLocal() as db:
            par = parceiros.parceiro_da_visita(request, db)
            base["cad_parceiro"] = (par.escritorio or par.nome) if par else ""
        base["cad_cupom"] = request.session.get("cupom", "")
    tudo = {**base, **ctx}
    if marca or base["app_ios"]:
        tudo["google_ok"] = False
    if base["app_ios"]:
        tudo["cadastro_aberto"] = False
    if marca:
        if marca["cor"]:
            tudo["ent_cor1"] = tudo["ent_cor2"] = marca["cor"]
    return templates.TemplateResponse(request, nome, tudo, status_code=status_code)


def usuario_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    uid = request.session.get("uid")
    u = db.get(Usuario, uid) if uid else None
    if not u or not u.ativo or u.conta_excluida_em:
        request.session.pop("uid", None)
        raise Redirecionar("/entrar")
    if not u.empresa.ativo:  # empresa bloqueada pelo master: ninguém entra
        request.session.pop("uid", None)
        avisar(request, "Esta conta está bloqueada. Fale com o suporte.", "erro")
        raise Redirecionar("/entrar")
    return u


def _ids(txt: str) -> set[int]:
    return {int(x) for x in (txt or "").split(",") if x.strip().isdigit()}


def alcance_setores(u: Usuario) -> set[int]:
    return _ids(u.alcance_setores)


def alcance_locais(u: Usuario) -> set[int]:
    """Ids dos locais do gerente; 0 = endereço principal da empresa."""
    return _ids(u.alcance_locais)


def restrito(u: Usuario) -> bool:
    """Gerente que só enxerga as pessoas de alguns setores e/ou locais (o dono nunca)."""
    return bool(u.gestor and not u.dono and (alcance_setores(u) or alcance_locais(u)))


def ve_pessoa(u: Usuario, p) -> bool:
    """p: Usuario ou qualquer objeto com id, setor_id e local_id."""
    if not restrito(u) or p.id == u.id:
        return True
    s, l = alcance_setores(u), alcance_locais(u)
    return (not s or p.setor_id in s) and (not l or (p.local_id or 0) in l)


def encaixar_no_alcance(u: Usuario, p: Usuario) -> None:
    """Pessoa cadastrada ou editada por um gerente limitado fica dentro do alcance dele."""
    if not restrito(u) or p.id == u.id:
        return
    s, l = alcance_setores(u), alcance_locais(u)
    if s and p.setor_id not in s:
        p.setor_id = min(s)
    if l and (p.local_id or 0) not in l:
        p.local_id = min(l) or None


# O que mexe na empresa inteira fica com quem não tem alcance limitado.
SO_SEM_ALCANCE = ("/painel/fechamento", "/painel/pagamento", "/painel/feriados", "/painel/locais",
                  "/painel/totens", "/painel/cerca", "/painel/setores", "/painel/importar-equipe")


def gestor_logado(request: Request, u: Usuario = Depends(usuario_logado),
                  db: Session = Depends(get_db)) -> Usuario:
    if not u.gestor:
        raise Redirecionar("/app")
    if restrito(u) and not request.session.get("master_uid"):
        from sqlalchemy import select
        # Só as colunas (não os objetos), para não deixar pessoas fora do alcance na sessão.
        todos = db.execute(select(Usuario.id, Usuario.setor_id, Usuario.local_id)
                           .where(Usuario.empresa_id == u.empresa_id)
                           .execution_options(alcance_livre=True)).all()
        db.info["alcance"] = (u.empresa_id, {p.id for p in todos if ve_pessoa(u, p)})
        caminho = request.url.path
        bloqueado = request.method == "POST" and (
            any(caminho == r or caminho.startswith(r + "/") for r in SO_SEM_ALCANCE[1:])
            or caminho == "/painel/fechamento" or caminho.endswith("/reabrir"))
        if bloqueado:
            avisar(request, "Você gerencia só alguns setores ou locais; esta ação vale para a empresa "
                            "inteira e fica com o dono da conta.", "erro")
            from urllib.parse import urlsplit
            ref = urlsplit(request.headers.get("referer") or "")
            raise Redirecionar(ref.path + (f"?{ref.query}" if ref.query else "")
                               if ref.netloc == request.url.netloc and ref.path.startswith("/painel") else "/painel")
    # Assinatura vencida: o gestor só enxerga a tela Assinatura (o suporte entra mesmo assim).
    if (not pode_usar(u.empresa) and not request.session.get("master_uid")
            and not request.url.path.startswith("/painel/assinatura")):
        raise Redirecionar("/painel/assinatura")
    recurso = recurso_da_rota(request.url.path)
    if recurso and not recurso_liberado(u.empresa, recurso) and not request.session.get("master_uid"):
        avisar(request, f"Seu plano {u.empresa.plano.nome} não inclui {RECURSOS[recurso]}. "
                        "Veja os planos abaixo para liberar.", "erro")
        raise Redirecionar("/painel/assinatura")
    chave = permissao_da_rota(request.url.path, request.method)
    if chave and not pode(u, chave) and not request.session.get("master_uid"):
        avisar(request, "Você não tem acesso a esta parte do painel. Fale com o dono da conta.", "erro")
        raise Redirecionar("/painel")
    return u


# O que um gerente pode fazer (o dono pode tudo). A ordem é a do menu.
PERMISSOES = {
    "escala": "Escala e trocas de turno",
    "ponto": "Ponto (batidas, ajustes e histórico)",
    "mural": "Mural",
    "tarefas": "Tarefas",
    "ausencias": "Ausências, atestados e feriados",
    "fechamento": "Fechamento do período",
    "pagamento": "Folha de pagamento (salários)",
    "equipe": "Equipe (funcionários)",
    "locais": "Locais e totens",
}
# Caminho do painel -> permissão necessária. "dono" = só o dono. Sem entrada = qualquer gestor.
ROTAS_PERMISSAO = (
    ("/painel/escala", "escala"), ("/painel/ofertas", "escala"),
    ("/painel/ponto", "ponto"), ("/painel/ajustes", "ponto"), ("/painel/historico", "ponto"),
    ("/painel/mural", "mural"), ("/painel/tarefas", "tarefas"),
    ("/painel/ausencias", "ausencias"), ("/painel/feriados", "ausencias"), ("/painel/ferias", "ausencias"),
    ("/painel/fechamento", "fechamento"), ("/painel/folha", "fechamento"),
    ("/painel/pagamento", "pagamento"),
    ("/painel/equipe", "equipe"), ("/painel/setores", "equipe"),
    ("/painel/locais", "locais"), ("/painel/totens", "locais"),
    ("/painel/relatorios", "ponto"), ("/painel/conformidade", "ponto"), ("/painel/importar-equipe", "equipe"), ("/painel/perguntas", "ponto"), ("/painel/cerca", "locais"),
    ("/painel/gerentes", "dono"), ("/painel/config", "dono"), ("/painel/portaria671", "ponto"),
)


def permissoes_de(u: Usuario) -> set[str]:
    return {p for p in (u.permissoes or "").split(",") if p in PERMISSOES}


def pode(u: Usuario, chave: str) -> bool:
    """O gestor pode usar esta parte do painel?"""
    if not u.gestor:
        return False
    if u.dono:
        return True
    return chave != "dono" and chave in permissoes_de(u)


def permissao_da_rota(caminho: str, metodo: str = "GET") -> str | None:
    if caminho.startswith("/painel/assinatura"):
        return None if metodo == "GET" else "dono"   # todos veem; só o dono paga ou muda
    for prefixo, chave in ROTAS_PERMISSAO:
        if caminho == prefixo or caminho.startswith(prefixo + "/") or caminho.startswith(prefixo + "."):
            return chave
    return None


def admin_logado(u: Usuario = Depends(usuario_logado)) -> Usuario:
    if not u.is_admin:
        raise Redirecionar("/painel" if u.gestor else "/app")
    return u


def ir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def data_iso(s: str, padrao: date | None) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return padrao


def da_empresa(db: Session, modelo, id_: int, u: Usuario):
    """Busca um registro garantindo que ele é da empresa de quem está logado."""
    obj = db.get(modelo, id_)
    if not obj or obj.empresa_id != u.empresa_id:
        raise Redirecionar("/painel")
    alcance = db.info.get("alcance")
    if alcance:   # gerente limitado: a pessoa (ou o dono do registro) precisa estar no alcance dele
        dono_id = obj.id if isinstance(obj, Usuario) else getattr(obj, "usuario_id", None)
        if dono_id is not None and dono_id not in alcance[1]:
            raise Redirecionar("/painel")
    return obj
