"""Medição de anúncios e origem dos clientes.

- Master -> Rastreamento: IDs do Pixel do Meta, do Google (GA4 e Google Ads) e do Pixel do TikTok. Os códigos
  dessas empresas só carregam na página de vendas e no cadastro, e só depois que o visitante aceita o aviso de
  cookies (LGPD). O cadastro de uma conta de teste dispara o evento de conversão na primeira tela do painel.
- Origem: um cookie próprio (dw_origem, 90 dias) guarda de onde o visitante chegou (UTM, ID de clique do
  anúncio ou o site que mandou). Ela vai para o lead do CRM, para a empresa cadastrada e para o relatório
  Master -> CRM -> Canais. Não é enviada a ninguém de fora.
"""
import base64
import json
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import core, instagram
from . import db as banco
from .core import admin_logado, avisar, exigir_csrf, ir, render
from .db import Config, Empresa, Lead, Pagamento, SessionLocal, Usuario, VisitaCanal, get_db

rota = APIRouter()
COOKIE = "dw_origem"
DIAS_COOKIE = 90
CAMPOS = {
    "rast_meta_pixel": (r"\d{8,20}", "ID do Pixel do Meta"),
    "rast_ga4": (r"G-[A-Z0-9]{4,16}", "ID de medição do Google Analytics 4"),
    "rast_google_ads": (r"AW-\d{6,14}", "ID da conta do Google Ads"),
    "rast_google_ads_rotulo": (r"[A-Za-z0-9_\-]{6,40}", "Rótulo da conversão do Google Ads"),
    "rast_tiktok_pixel": (r"[A-Z0-9]{10,30}", "ID do Pixel do TikTok"),
}
UTM = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
CLIQUES = {"gclid": "google", "gbraid": "google", "wbraid": "google", "fbclid": "meta", "ttclid": "tiktok",
           "li_fat_id": "linkedin"}
# páginas públicas onde os códigos de anúncio podem carregar (com o aceite)
_PUBLICAS = re.compile(r"^/($|cadastro|cupom/|planos|precos|parceiros/?$)")
# de onde a origem é registrada: tudo, menos o sistema por dentro e arquivos
_SEM_ORIGEM = re.compile(r"^/(painel|master|app|static|midia|api|totem|conta|vendedor|manifest|sw\.js|favicon)")

_cache: dict | None = None


# ------------------------------------------------------------------ configuração
def ids() -> dict:
    global _cache
    if _cache is None:
        with SessionLocal() as db:
            _cache = {k: (db.get(Config, k).valor if db.get(Config, k) else "") for k in CAMPOS}
    return _cache


def ligado() -> bool:
    d = ids()
    return any(d[k] for k in ("rast_meta_pixel", "rast_ga4", "rast_google_ads", "rast_tiktok_pixel"))


def contexto(request: Request) -> dict | None:
    """O que o template precisa: os IDs, se esta página pode carregar os códigos e se há uma conversão
    a enviar (cadastro recém-feito)."""
    if not ligado():
        return None
    evento = request.session.pop("evento_cadastro", None) if "evento_cadastro" in request.session else None
    publica = bool(_PUBLICAS.match(request.url.path))
    if not (publica or evento):
        return None
    return {**ids(), "publica": publica, "evento": bool(evento)}


# ------------------------------------------------------------------ origem do visitante
def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def origem_da_visita(request: Request) -> dict | None:
    """Monta a origem desta visita, se ela trouxer algo (UTM, ID de clique ou um site de fora)."""
    q = request.query_params
    o = {k.removeprefix("utm_"): q[k][:120] for k in UTM if q.get(k)}
    for k, rede in CLIQUES.items():
        if q.get(k):
            o["clique"] = rede
            break
    ref = _host(request.headers.get("referer", ""))
    meu = (request.url.hostname or "").lower().removeprefix("www.")
    if ref and ref != meu and not ref.endswith("." + meu):
        o["ref"] = ref[:120]
    if not o:
        return None
    o["pagina"] = request.url.path[:80]
    o["em"] = date.today().isoformat()
    return o


def gravar_cookie(request: Request, resposta) -> None:
    """Primeira visita com origem: guarda. Uma visita nova com campanha (UTM ou clique) substitui."""
    if request.method != "GET" or _SEM_ORIGEM.match(request.url.path):
        return
    o = origem_da_visita(request)
    if not o:
        return
    antes = ler(request) if request.cookies.get(COOKIE) else {}
    if antes and not ({"source", "clique"} & o.keys()):
        return
    if {"source", "clique"} & o.keys() and (canal(antes), antes.get("campaign", "")) != (canal(o), o.get("campaign", "")):
        contar_visita(o)
    valor = base64.urlsafe_b64encode(json.dumps(o, separators=(",", ":")).encode()).decode().rstrip("=")
    resposta.set_cookie(COOKIE, valor,
                        max_age=DIAS_COOKIE * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")


def ler(request: Request) -> dict:
    try:
        txt = request.cookies.get(COOKIE, "") or ""
        o = json.loads(base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4)).decode()) if txt else {}
        return {k: str(v)[:120] for k, v in o.items()} if isinstance(o, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def ler_json(request: Request) -> str:
    o = ler(request)
    return json.dumps(o, ensure_ascii=False) if o else ""


REDES = [("google", "Google"), ("instagram", "Instagram"), ("facebook", "Facebook"), ("fb", "Facebook"),
         ("ig", "Instagram"), ("meta", "Instagram/Facebook"), ("tiktok", "TikTok"), ("youtube", "YouTube"),
         ("linkedin", "LinkedIn"), ("whatsapp", "WhatsApp"), ("bing", "Bing")]
PAGOS = {"cpc", "ppc", "paid", "pago", "ads", "paid_social", "cpm", "anuncio", "anúncio"}


def contar_visita(o: dict) -> None:
    """Soma 1 no contador do dia para o canal e a campanha (só quando o navegador ainda não tinha essa origem)."""
    nome, camp = canal(o)[:60], (o.get("campaign") or "")[:60].lower()
    try:
        with SessionLocal() as db:
            v = db.scalar(select(VisitaCanal).where(VisitaCanal.dia == date.today(), VisitaCanal.canal == nome,
                                                    VisitaCanal.campanha == camp))
            if v:
                v.visitas += 1
            else:
                db.add(VisitaCanal(dia=date.today(), canal=nome, campanha=camp, visitas=1))
            db.commit()
    except Exception:
        pass


def canal(o: dict | str | None) -> str:
    """Nome do canal para o relatório: rede + (anúncio | orgânico), ou Direto."""
    if isinstance(o, str):
        try:
            o = json.loads(o) if o else {}
        except ValueError:
            o = {}
    o = o or {}
    fonte = (o.get("source") or "").lower()
    meio = (o.get("medium") or "").lower()
    ref = (o.get("ref") or "").lower()
    clique = o.get("clique", "")
    nome = ""
    for chave, rotulo in REDES:
        if chave in fonte or (not fonte and len(chave) > 3 and chave in ref):
            nome = rotulo
            break
    if not nome and clique:
        nome = {"google": "Google", "meta": "Instagram/Facebook", "tiktok": "TikTok", "linkedin": "LinkedIn"}[clique]
    if not nome:
        if fonte:
            return fonte[:40].capitalize()
        return "Outros sites" if ref else "Direto"
    pago = bool(clique) or meio in PAGOS or any(p in meio for p in ("cpc", "paid", "ads"))
    return f"{nome} · {'anúncio' if pago else 'orgânico'}"


def _dic(o: dict | str | None) -> dict:
    if isinstance(o, str):
        try:
            o = json.loads(o) if o else {}
        except ValueError:
            o = {}
    return o if isinstance(o, dict) else {}


def campanha(o: dict | str | None) -> str:
    """Nome da campanha (utm_campaign), em minúsculas, ou ""."""
    return (_dic(o).get("campaign") or "")[:60].lower()


def resumo(o: dict | str | None) -> str:
    """Uma linha para a ficha: canal, campanha e anúncio."""
    if isinstance(o, str):
        try:
            o = json.loads(o) if o else {}
        except ValueError:
            o = {}
    if not o:
        return ""
    partes = [canal(o)] + [o[k] for k in ("campaign", "content", "term") if o.get(k)]
    return " · ".join(partes)


# ------------------------------------------------------------------ Master -> Rastreamento
@rota.get("/master/rastreamento")
def tela(request: Request, u: Usuario = Depends(admin_logado)):
    return render(request, "master/rastreamento.html", u=u, e=u.empresa, aba="master", sub="rastreamento",
                  d=ids(), CAMPOS=CAMPOS, ig_ligado=instagram.ligado(),
                  ig_token=bool(os.environ.get("META_IG_TOKEN", "").strip()),
                  ig_url=(core.PUBLIC_URL or str(request.base_url).rstrip("/")) + "/webhooks/instagram")


@rota.post("/master/rastreamento", dependencies=[Depends(exigir_csrf)])
async def salvar(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    global _cache
    form = await request.form()
    erros = []
    for k, (padrao, nome) in CAMPOS.items():
        v = re.sub(r"\s+", "", str(form.get(k, "")))
        if k == "rast_tiktok_pixel":
            v = v.upper()
        if v and not re.fullmatch(padrao, v):
            erros.append(nome)
            continue
        c = db.get(Config, k) or Config(chave=k, valor="")
        c.valor = v
        db.add(c)
    db.commit()
    _cache = None
    if erros:
        avisar(request, "Confira o formato de: " + ", ".join(erros) + ". Os outros campos foram salvos.", "erro")
    else:
        avisar(request, "Rastreamento salvo. Os códigos carregam na página de vendas e no cadastro "
                        "para quem aceitar os cookies.")
    return ir("/master/rastreamento")


# ------------------------------------------------------------------ Master -> CRM -> Canais
def _mes(txt: str) -> date:
    try:
        return datetime.strptime(txt, "%Y-%m").date()
    except ValueError:
        h = date.today()
        return h.replace(day=1)


def semanas_instagram(db: Session, semanas: int = 8) -> list[dict]:
    """Leads do Instagram por semana (segunda a domingo) e quantos viraram conta de teste e cliente."""
    hoje = banco.agora().date()
    seg = hoje - timedelta(days=hoje.weekday())
    inicio = seg - timedelta(weeks=semanas - 1)
    linhas = [{"de": inicio + timedelta(weeks=i), "ate": inicio + timedelta(weeks=i, days=6),
               "leads": 0, "testes": 0, "clientes": 0} for i in range(semanas)]
    q = select(Lead.origem, Lead.utm, Lead.empresa_id, Lead.etapa, Lead.criado_em).where(
        Lead.criado_em >= datetime.combine(inicio, datetime.min.time()))
    for origem, utm, eid, etapa, criado in db.execute(q):
        if origem != "instagram" and not canal(utm).startswith("Instagram ·"):
            continue
        li = linhas[min((criado.date() - inicio).days // 7, semanas - 1)]
        li["leads"] += 1
        li["testes"] += 1 if eid else 0
        li["clientes"] += 1 if etapa == "cliente" else 0
    return list(reversed(linhas))


@rota.get("/master/crm/canais")
def canais(request: Request, mes: str = "", u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    from . import crm
    ini = _mes(mes)
    fim = (ini.replace(day=28) + timedelta(days=4)).replace(day=1)
    di, df = datetime.combine(ini, datetime.min.time()), datetime.combine(fim, datetime.min.time())
    pid = crm.empresa_plataforma_id(db)
    linhas: dict[str, dict] = {}
    camps: dict[tuple, dict] = {}
    zero = {"visitas": 0, "leads": 0, "testes": 0, "pagantes": 0, "receita": 0}

    def linha(nome):
        return linhas.setdefault(nome, {"canal": nome, **zero})

    def camp(nome, c):
        return camps.setdefault((nome, c), {"canal": nome, "campanha": c, **zero})

    for nome, c, n in db.execute(select(VisitaCanal.canal, VisitaCanal.campanha, func.sum(VisitaCanal.visitas))
                                 .where(VisitaCanal.dia >= ini, VisitaCanal.dia < fim)
                                 .group_by(VisitaCanal.canal, VisitaCanal.campanha)):
        linha(nome)["visitas"] += n or 0
        if c:
            camp(nome, c)["visitas"] += n or 0
    for o, in db.execute(select(Lead.utm).where(Lead.criado_em >= di, Lead.criado_em < df)):
        nome = canal(o)
        linha(nome)["leads"] += 1
        if campanha(o):
            camp(nome, campanha(o))["leads"] += 1
    empresas = list(db.scalars(select(Empresa).where(Empresa.criado_em >= di, Empresa.criado_em < df,
                                                     Empresa.id != (pid or 0))))
    pagas = {}
    if empresas:
        pagas = dict(db.execute(select(Pagamento.empresa_id, func.sum(Pagamento.valor_centavos))
                                .where(Pagamento.empresa_id.in_([x.id for x in empresas]),
                                       Pagamento.status == "pago")
                                .group_by(Pagamento.empresa_id)).all())
    for x in empresas:
        nome, c = canal(x.origem), campanha(x.origem)
        alvos = [linha(nome)] + ([camp(nome, c)] if c else [])
        for li in alvos:
            li["testes"] += 1
            if pagas.get(x.id):
                li["pagantes"] += 1
                li["receita"] += pagas[x.id] or 0
    ordem = lambda r: (-r["pagantes"], -r["testes"], -r["leads"], -r["visitas"], r["canal"])  # noqa: E731
    tabela = sorted(linhas.values(), key=ordem)
    por_campanha = sorted(camps.values(), key=lambda r: (r["canal"], -r["pagantes"], -r["testes"], -r["visitas"],
                                                         r["campanha"]))
    meses = []
    m = date.today().replace(day=1)
    for _ in range(12):
        meses.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    return render(request, "master/crm_canais.html", u=u, e=u.empresa, aba="master", sub="canais",
                  tabela=tabela, por_campanha=por_campanha, ig_semanas=semanas_instagram(db), ini=ini, meses=meses, configurado=ligado(),
                  total={k: sum(r[k] for r in tabela) for k in ("visitas", "leads", "testes", "pagantes", "receita")})
