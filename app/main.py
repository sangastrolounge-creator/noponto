"""Gestor de escalas, ponto por geolocalização e tarefas de equipe — multiempresa.

Gestor em /painel (computador ou celular); funcionário em /app (celular, instalável).
"""
import asyncio
import base64
import csv
import io
import os
import secrets
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import core
from . import db as banco
from . import crescimento, entrada, parceiros, rep
from . import setores as setores_mod
from . import (apuracao, asaas, caixa, instagram, whatsapp, cobranca, conta, crm, rastreio, desligamento, lojas, vendedor, facial, logo_empresa, fase2, fase3, i18n, master, pagamento, push, regras,
               vendas, avisos_master, custos, email_envio, extra, ficha, gerentes, menu, mural,
               ponto_problemas, trocas, disponibilidade, avaliacao, relatorios, legal, salario_minimo, escala_justa, menores, conformidade, triagem, confirmacao, importar, perguntas, cerca, ferias, apropriacao, calendario, dashboard, faturamento, paises, fotos_equipe)
from .core import (APP_NOME, AQUI, CADASTRO_ABERTO, COOKIE_SECURE, HORA_RE,
                   SECRET_KEY, Redirecionar, app_ios, destino_inicial, avisar, base_url, celular_limpo,
                   da_empresa, data_iso, ir,
                   celular_valido, conferir_senha, dentro_do_limite,
                   email_valido, exigir_csrf, gerar_hash, gestor_logado, ip,
                   log, render, templates, usuario_logado)
from .db import (PASTA_FOTOS, Ajuste, Alerta, Aparelho, Ausencia, Batida, Empresa, Feriado,
                 Historico, PushSub, SessionLocal, Tarefa, TarefaFeita, Totem, Turno,
                 Usuario, get_db)
from .regras import VALIDA

COOKIE_APARELHO = "turno_aparelho"

CSRF = [Depends(exigir_csrf)]


def ciclo_alertas():
    with SessionLocal() as db:
        novos, lembretes = regras.verificar_turnos(db)
        novos += extra.verificar(db)
        novos += menores.verificar(db)
        from .paises.us import menores as menores_us
        novos += menores_us.verificar(db)
        n2, l2 = confirmacao.verificar(db)
        novos += n2
        lembretes += l2
        regras.limpar_fotos(db)
        desligamento.limpar_excluidos(db, banco.agora().date())   # prazos legais e histórico de 1 ano
        cobranca.ajustar_valores(db)
        _us_assinatura.ajustar_valores(db)
    vendedor.limpar_antigas()   # planos por pessoa: uma vez por dia
    try:
        crm.sincronizar()           # leads do chat e contas que assinaram
    except Exception:
        pass
    try:
        caixa.sincronizar()         # e-mails novos da caixa de contato (Master -> E-mail)
    except Exception:
        pass
    push.avisar_alertas([a.id for a in novos])
    push.avisar_usuarios(lembretes)
    avisos_master.resumo_testes()   # uma vez por dia, depois das 09:00 de Brasília


def base_url_publica() -> str:
    from .core import PUBLIC_URL
    return PUBLIC_URL


async def _agendador():
    while True:
        try:
            await asyncio.to_thread(ciclo_alertas)
        except Exception:
            log.exception("Falha ao verificar turnos")
        await asyncio.sleep(60)


@asynccontextmanager
async def _vida(_: FastAPI):
    tarefa = None if os.environ.get("SEM_AGENDADOR") else asyncio.create_task(_agendador())
    if asaas.configurado():  # cadastra no Asaas o aviso de pagamentos para este servidor
        import threading
        threading.Thread(target=asaas.garantir_webhook, daemon=True, args=(
            base_url_publica(), cobranca.email_do_dono(), master.plataforma()["nome"])).start()
    await asyncio.to_thread(rep.evento_servico, "07")   # Portaria 671: serviço disponível
    yield
    if tarefa:
        tarefa.cancel()
    await asyncio.to_thread(rep.evento_servico, "08")   # serviço indisponível (desligando)


app = FastAPI(title=APP_NOME, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_vida)
# Telas com versão por país (ex.: pagamento dos EUA). Fica por dentro da sessão para ler o login.
from .paises.rotas import RotasPorPais  # noqa: E402
app.add_middleware(RotasPorPais)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY,
                   session_cookie="turno_sessao", same_site="lax",
                   https_only=COOKIE_SECURE, max_age=60 * 60 * 24 * 90)
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
app.include_router(fase2.rota)
app.include_router(fase3.rota)
app.include_router(master.rota)
app.include_router(cobranca.rota)
app.include_router(vendas.router)
app.include_router(pagamento.rota)
app.include_router(trocas.rota)
app.include_router(mural.rota)
app.include_router(gerentes.rota)
app.include_router(email_envio.rota)
app.include_router(desligamento.rota)
app.include_router(logo_empresa.rota)
app.include_router(fotos_equipe.rota)
app.include_router(facial.rota)
app.include_router(avisos_master.rota)
app.include_router(custos.rota)
app.include_router(ficha.rota)
app.include_router(ponto_problemas.rota)
app.include_router(disponibilidade.rota)
app.include_router(avaliacao.rota)
app.include_router(relatorios.rota)
app.include_router(legal.rota)
app.include_router(salario_minimo.rota)
app.include_router(escala_justa.rota)
app.include_router(conformidade.rota)
app.include_router(triagem.rota)
app.include_router(confirmacao.rota)
app.include_router(importar.rota)
app.include_router(perguntas.rota)
app.include_router(cerca.rota)
app.include_router(ferias.rota)
app.include_router(apropriacao.rota)
app.include_router(faturamento.rota)
app.include_router(setores_mod.rota)
app.include_router(entrada.rota)
app.include_router(crescimento.rota)
app.include_router(parceiros.rota)
app.include_router(rep.rota)
app.include_router(conta.rota)
app.include_router(lojas.rota)
app.include_router(vendedor.rota)
# Rotas só das empresas dos EUA (empresa de outro país recebe 404)
from .paises.us import descanso as _us_descanso, escala_justa as _us_fw  # noqa: E402
from .paises.us import pto as _us_pto, salario_minimo as _us_sm, telas as _us_telas  # noqa: E402
for _r in (_us_descanso.rota, _us_fw.rota, _us_pto.rota, _us_sm.rota):
    app.include_router(_r, dependencies=[Depends(_us_telas.so_eua)])
from .paises.us import pagamento as _us_pagamento  # noqa: E402
app.include_router(_us_pagamento.rota, prefix="/eua", dependencies=[Depends(_us_telas.so_eua)])
from .paises.us import assinatura as _us_assinatura  # noqa: E402
app.include_router(_us_assinatura.rota, prefix="/eua", dependencies=[Depends(_us_telas.so_eua)])
app.include_router(_us_assinatura.rota_geral)
from .paises.us import conformidade as _us_conformidade  # noqa: E402
app.include_router(_us_conformidade.rota, prefix="/eua", dependencies=[Depends(_us_telas.so_eua)])
app.include_router(rastreio.rota)   # antes do CRM: /master/crm/canais não é um lead
app.include_router(crm.rota)
app.include_router(instagram.rota)
app.include_router(whatsapp.rota)    # /webhooks/whatsapp e a conversa do WhatsApp na ficha do lead   # /webhooks/instagram: direct do Instagram vira lead
app.include_router(caixa.rota)
app.mount("/midia", StaticFiles(directory=vendas.MIDIA_DIR), name="midia")

templates.env.globals.update(
    vendedor_ligado=vendedor.ligado,
    caixa_nao_lidos=caixa.nao_lidos,
    origem_resumo=rastreio.resumo,
    local=lambda e, dt, f="%H:%M": regras.para_local(e, dt).strftime(f) if dt else "",
    fmt_min=regras.fmt_min, fmt_min_raw=regras.minutos_turno, DIAS=i18n.DIAS, fmt_saldo=apuracao.fmt_saldo, com_acrescimo=apuracao.com_acrescimo,
    TIPOS_AUSENCIA=i18n.Traduzido(apuracao.TIPOS_AUSENCIA), zap=fase3.zap,
    mural_novas=mural.novas, SITUACOES_OFERTA=i18n.Traduzido(trocas.SITUACOES),
    alertas_liberados=core.alertas_liberados, contadores_menu=menu.contadores, gps_liberado=core.gps_liberado,
    foto_liberada=core.foto_liberada, facial_liberado=core.facial_liberado, cerca_liberada=core.cerca_liberada, exige_foto=core.exige_foto, pode=core.pode,
    PERMISSOES=i18n.Traduzido(core.PERMISSOES), funcoes_empresa=triagem.funcoes_da, modelos_turno=triagem.modelos_da,
    avatar=fotos_equipe.avatar, zap_icone=fotos_equipe.zap_icone)


@app.exception_handler(Redirecionar)
async def _redirecionar(_, exc: Redirecionar):
    return RedirectResponse(exc.url, status_code=303)


# Endereços antigos ou alternativos (ex.: www, o endereço provisório) que levam ao PUBLIC_URL.
HOSTS_REDIRECIONAR = {h.strip().lower() for h in
                      os.environ.get("HOSTS_REDIRECIONAR", "").split(",") if h.strip()}


# App do iPhone: contratação e login do Google só pelo site (App Store 3.1.1 e 4.8), sem link para fora.
IOS_BLOQUEADOS = {"/cadastro", "/entrar/google", "/painel/assinatura", "/parceiros", "/planos"}
IOS_BLOQUEADOS_PREFIXO = ("/painel/assinatura/", "/i/", "/p/", "/parceiro/", "/entrar/google/")
IOS_PAGINA_SITE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Data Worke</title>
<body style="font-family:-apple-system,system-ui,sans-serif;max-width:420px;margin:18vh auto;padding:0 24px;
text-align:center;color:#1d2140"><h1 style="font-size:1.3rem">Disponível pelo site</h1>
<p style="color:#5b607a;line-height:1.5">A contratação e a gestão do plano da empresa são feitas pelo site do
Data Worke, num computador ou no navegador. No aplicativo você entra e usa o ponto, a escala e os relatórios.</p>
<p><a href="javascript:history.back()" style="color:#4f6bf5">Voltar</a></p></body></html>"""


@app.middleware("http")
async def _cabecalhos(request: Request, call_next):
    caminho = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    i18n.definir(i18n.escolher(request.cookies.get(i18n.COOKIE),
                               request.headers.get("accept-language"),
                               paises.pais_do_host(request)), caminho)
    host = request.headers.get("host", "").split(":")[0].lower()
    publico = base_url_publica()
    if (publico and host in HOSTS_REDIRECIONAR and request.method in ("GET", "HEAD")
            and request.url.path != "/saude"):
        destino = publico + request.url.path
        if request.url.query:
            destino += "?" + request.url.query
        return RedirectResponse(destino, status_code=301)
    if app_ios(request):
        p = request.url.path
        if p == "/":
            return RedirectResponse("/entrar", status_code=303)
        if p in IOS_BLOQUEADOS or any(p.startswith(x) for x in IOS_BLOQUEADOS_PREFIXO):
            return HTMLResponse(IOS_PAGINA_SITE, status_code=200)
    resp = await call_next(request)
    try:
        rastreio.gravar_cookie(request, resp)   # de qual rede ou anúncio o visitante chegou
    except Exception:
        pass
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self)")
    return resp


# ============================================================ site e login
@app.get("/saude")
def saude():
    return {"ok": True}


@app.get("/sw.js")
def sw():
    return FileResponse(AQUI / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest(request: Request):
    plat = master.plataforma()
    nome, curto = plat["nome"], plat.get("nome_curto") or plat["nome"]
    lingua, descricao = "pt-BR", "Escala, ponto e tarefas da equipe."
    if paises.pais_do_host(request) == "US":   # site dos EUA: marca sem o "Brasil", em inglês
        nome = curto = core.nome_eua(plat["nome"])
        with i18n.no_idioma("en"):
            lingua, descricao = "en-US", i18n._(descricao)
    return JSONResponse({
        "name": nome,
        "short_name": curto,
        "id": "/", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#ffffff", "theme_color": "#4f6bf5",
        "lang": lingua, "description": descricao,
        "categories": ["business", "productivity"],
        "icons": [
            {"src": "/static/icone-192.png?v=4", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icone-512.png?v=4", "sizes": "512x512", "type": "image/png"},
        ]}, media_type="application/manifest+json")


@app.get("/idioma/{cod}")
def trocar_idioma(cod: str, volta: str = "/"):
    """Seletor de idioma do topo: grava a escolha e volta para a mesma página."""
    if not volta.startswith("/") or volta.startswith("//") or "\\" in volta:
        volta = "/"
    resp = ir(volta)
    if cod in i18n.IDIOMAS:
        resp.set_cookie(i18n.COOKIE, cod, max_age=60 * 60 * 24 * 365, samesite="lax",
                        secure=COOKIE_SECURE)
    return resp


@app.get("/offline")
def offline(request: Request):
    return render(request, "offline.html")


@app.get("/")
def inicio(request: Request, db: Session = Depends(get_db)):
    u = db.get(Usuario, request.session.get("uid") or 0)
    if u and u.ativo:
        return ir(destino_inicial(u))
    if paises.pais_do_host(request) == "US":   # site americano (dataworke.com)
        return render(request, "us/site_inicio.html", pais_tela="US",
                      planos=master.planos_a_venda(db, "US"),
                      cadastro_eua=master.plataforma()["cadastro_eua_aberto"])
    publicado = vendas.conteudo_publicado(db)
    if publicado:  # página de vendas longa, editada no Painel Master
        return render(request, "vendas.html", previa=False,
                      **vendas.contexto_da_pagina(db, publicado))
    return render(request, "inicio.html", planos=master.planos_a_venda(db))


@app.get("/cadastro")
def cadastro_form(request: Request, plano: int = 0, google: int = 0, db: Session = Depends(get_db)):
    if paises.pais_do_host(request) == "US":
        if not master.plataforma()["cadastro_eua_aberto"]:   # antes da cobrança: lista de espera
            return render(request, "us/lista_espera.html", pais_tela="US", erro=None, enviado=False, v={})
        p = db.get(master.Plano, plano)
        return render(request, "cadastro.html", pais_tela="US", erro=None, google=None, google_ok=False,
                      plano=p if p and p.pais == "US" else None, v={})
    if not master.plataforma()["cadastro_aberto"]:
        return ir("/entrar")
    g = entrada.google_pendente(request) if google else None
    if not google:
        request.session.pop("google_novo", None)
    return render(request, "cadastro.html", erro=None, plano=db.get(master.Plano, plano), google=g,
                  v={"nome": g["nome"], "email": g["email"]} if g else {})


@app.post("/cadastro", dependencies=CSRF)
def cadastro(request: Request, tarefas_bg: BackgroundTasks, empresa: str = Form(""), nome: str = Form(""),
             email: str = Form(""), senha: str = Form(""), plano: int = Form(0),
             aceito: str = Form(""), cupom: str = Form(""), contador: str = Form(""),
             db: Session = Depends(get_db)):
    pais = paises.pais_do_host(request)
    aberto = master.plataforma()["cadastro_eua_aberto" if pais == "US" else "cadastro_aberto"]
    if not aberto:
        return ir("/cadastro" if pais == "US" else "/entrar")
    empresa, nome, email = empresa.strip()[:120], nome.strip()[:120], email.strip().lower()
    erro = None
    if not dentro_do_limite(f"cad:{ip(request)}", 5, 3600):
        erro = "Muitas tentativas. Aguarde um pouco e tente de novo."
    elif not empresa or not nome:
        erro = "Informe o nome da empresa e o seu nome."
    elif not email_valido(email):
        erro = "E-mail inválido."
    elif len(senha) < 6:
        erro = "A senha precisa ter pelo menos 6 caracteres."
    elif db.scalar(select(Usuario.id).where(Usuario.email == email).execution_options(alcance_livre=True)):
        erro = "Já existe uma conta com este e-mail."
    elif not aceito:
        erro = "Para criar a conta, aceite os Termos de Uso e a Política de Privacidade."
    if erro:
        return render(request, "cadastro.html", 400, erro=erro, plano=db.get(master.Plano, plano),
                      v={"empresa": empresa, "nome": nome, "email": email},
                      **({"pais_tela": "US", "google_ok": False} if pais == "US" else {}))
    u = master.criar_empresa(db, empresa, nome, email, senha, plano, pais)
    legal.registrar_aceite(u, pais)
    db.commit()
    crescimento.ao_criar_empresa(request, db, u, cupom, bool(contador))
    tarefas_bg.add_task(avisos_master.nova_empresa, u.empresa_id)
    request.session["uid"] = u.id
    avisar(request, "Conta criada. Responda 7 perguntas rápidas e deixamos tudo pronto.")
    return ir("/painel/comecar")


@app.post("/lista-de-espera", dependencies=CSRF)
def lista_espera(request: Request, empresa: str = Form(""), nome: str = Form(""), email: str = Form(""),
                 telefone: str = Form(""), funcionarios: str = Form(""), autorizo: str = Form(""),
                 db: Session = Depends(get_db)):
    """Site americano antes de abrir o cadastro: o interessado vira lead no CRM (país EUA)."""
    v = {"empresa": empresa.strip()[:80], "nome": nome.strip()[:80], "email": email.strip().lower()[:120],
         "telefone": re.sub(r"\D", "", telefone)[:20], "funcionarios": funcionarios.strip()}
    erro = None
    if not dentro_do_limite(f"espera:{ip(request)}", 5, 3600):
        erro = "Muitas tentativas. Aguarde um pouco e tente de novo."
    elif not v["empresa"] or not v["nome"]:
        erro = "Informe o nome da empresa e o seu nome."
    elif not email_valido(v["email"]):
        erro = "E-mail inválido."
    elif not autorizo:
        erro = "Autorize o contato para entrar na lista."
    if erro:
        return render(request, "us/lista_espera.html", 400, pais_tela="US", erro=erro, enviado=False, v=v)
    n = int(v["funcionarios"]) if v["funcionarios"].isdigit() else 0
    crm.lead_da_lista_espera(db, v["nome"], v["empresa"], v["email"], v["telefone"], min(n, 100000))
    return render(request, "us/lista_espera.html", pais_tela="US", erro=None, enviado=True, v={})


@app.get("/entrar")
def entrar_form(request: Request):
    return render(request, "entrar.html", erro=None, login="")


@app.post("/entrar", dependencies=CSRF)
def entrar(request: Request, login: str = Form(""), senha: str = Form(""),
           db: Session = Depends(get_db)):
    login = login.strip()
    if not dentro_do_limite(f"login:{ip(request)}", 10, 600):
        return render(request, "entrar.html", 429, login=login,
                      erro="Muitas tentativas. Aguarde alguns minutos.")
    if "@" in login:
        u = db.scalar(select(Usuario).where(Usuario.email == login.lower()))
    else:
        u = db.scalar(select(Usuario).where(Usuario.celular == celular_limpo(login)))
    if not u or not u.ativo or not conferir_senha(senha, u.senha_hash):
        return render(request, "entrar.html", 400, login=login,
                      erro="Dados incorretos. Confira e tente de novo.")
    if not u.empresa.ativo:
        return render(request, "entrar.html", 403, login=login,
                      erro="Esta conta está bloqueada. Fale com o suporte.")
    request.session.pop("master_uid", None)
    request.session["uid"] = u.id
    return ir(destino_inicial(u))


@app.post("/sair", dependencies=CSRF)
def sair(request: Request):
    request.session.clear()
    return ir("/entrar")


@app.get("/convite/{token}")
def convite_form(request: Request, token: str, db: Session = Depends(get_db)):
    u = db.scalar(select(Usuario).where(Usuario.convite_token == token))
    if not u or not u.ativo:
        return render(request, "convite.html", 404, u=None, erro=None, token=token)
    return render(request, "convite.html", u=u, erro=None, token=token)


@app.post("/convite/{token}", dependencies=CSRF)
def convite(request: Request, token: str, senha: str = Form(""), consentimento: str = Form(""),
            db: Session = Depends(get_db)):
    u = db.scalar(select(Usuario).where(Usuario.convite_token == token))
    if not u or not u.ativo:
        return render(request, "convite.html", 404, u=None, erro=None, token=token)
    if len(senha) < 6:
        return render(request, "convite.html", 400, u=u, token=token,
                      erro="A senha precisa ter pelo menos 6 caracteres.")
    # LGPD: a localização e a foto do ponto têm como base a execução do contrato e a obrigação
    # legal de controlar a jornada (art. 7º, II e V); a tela pede a ciência e guarda quando foi dada.
    u.senha_hash, u.convite_token = gerar_hash(senha), None
    u.conta_excluida_em = None      # convite novo depois de a pessoa ter excluído a conta
    if consentimento:
        u.consentimento_em = banco.agora()
    legal.registrar_aceite(u)
    db.commit()
    request.session["uid"] = u.id
    return ir(destino_inicial(u))


# ============================================================ painel: hoje
def linhas_de_hoje(db: Session, e: Empresa, dia: date) -> list[dict]:
    al = regras.agora_local(e)
    turnos = list(db.scalars(select(Turno).join(Usuario).where(
        Turno.empresa_id == e.id, Turno.data == dia, Usuario.ativo.is_(True))
        .order_by(Turno.folga, Turno.inicio, Usuario.nome)))
    bats: dict[int, list[Batida]] = {}
    soltas = []
    for b in db.scalars(select(Batida).where(Batida.empresa_id == e.id, Batida.dia == dia, VALIDA)
                        .order_by(Batida.momento)):
        if b.turno_id:
            bats.setdefault(b.turno_id, []).append(b)
        else:
            soltas.append(b)
    ausentes = apuracao.ausencias_por_dia(db, e, dia, dia)
    linhas = []
    for t in turnos:
        bs = bats.get(t.id, [])
        sit = regras.situacao_turno(t, bs, al, e.tolerancia_min)
        aus = ausentes.get((t.usuario_id, dia))
        if aus and not bs and not t.folga:
            sit = {"cod": "ausencia", "cor": "azul",
                   "texto": apuracao.TIPOS_AUSENCIA.get(aus.tipo, "Ausência")}
        linhas.append({"t": t, "u": t.usuario, "batidas": bs, "sit": sit})
    return linhas, soltas


@app.get("/painel")
def painel(request: Request, u: Usuario = Depends(gestor_logado),
           db: Session = Depends(get_db)):
    e = u.empresa
    dia = regras.hoje(e)
    linhas, soltas = linhas_de_hoje(db, e, dia)
    cods = [l["sit"]["cod"] for l in linhas]
    alertas = list(db.scalars(select(Alerta).where(
        Alerta.empresa_id == e.id, Alerta.lido.is_(False),
        Alerta.criado_em >= banco.agora() - timedelta(days=7))
        .order_by(Alerta.criado_em.desc()).limit(30)))
    tarefas = regras.tarefas_do_dia(db, e, dia)
    sm = paises.regras_de(e).salario_minimo.situacao(db, e, dia) if core.pode(u, "pagamento") or core.pode(u, "equipe") else None
    return render(
        request, "painel_hoje.html", u=u, e=e, aba="hoje", dia=dia, linhas=linhas,
        soltas=soltas, alertas=alertas, tarefas=tarefas, sm=sm,
        banners=master.banners_no_ar(db, dia, e.pais or "BR"),
        saudacao=dashboard.saudacao(regras.agora_local(e)),
        n_problemas=len(ponto_problemas.problemas(db, e)) if core.pode(u, "ponto") else 0,
        sugestoes=dashboard.sugestoes(db, e, u, dia),
        semana=dashboard.semana(db, e, dia, None) if core.pode(u, "ponto") else None,
        cmo=(cmo := faturamento.historico(db, e, dia) if e.usa_faturamento and core.pode(u, "pagamento") else None),
        cmo_grafico=dashboard.grafico_cmo(cmo, e.meta_cmo or 30) if cmo else None,
        cmo_orientacao=faturamento.orientacao(e), REGIMES=faturamento.REGIMES,
        agora_sit=dashboard.situacao_agora(linhas),
        n={"escalados": sum(c not in ("folga", "ausencia") for c in cods),
           "presentes": sum(c in ("trabalhando", "sem_saida") for c in cods),
           "atrasados": sum(l["sit"]["cor"] == "alerta" and l["sit"]["cod"] != "sem_saida"
                            for l in linhas),
           "ausentes": sum(c in ("sem_ponto", "falta") for c in cods),
           "tarefas_feitas": sum(1 for t in tarefas if t["feita"])},
        tem_equipe=bool(db.scalar(select(Usuario.id).where(
            Usuario.empresa_id == e.id, Usuario.papel == "funcionario").limit(1))),
        push_chave=push.chave_publica(db),
        ajustes_pendentes=db.scalar(select(func.count()).select_from(Ajuste).where(
            Ajuste.empresa_id == e.id, Ajuste.situacao == "pendente")) or 0,
        aparelhos_pendentes=db.scalar(select(func.count()).select_from(Aparelho).join(Usuario).where(
            Usuario.empresa_id == e.id, Aparelho.situacao == "pendente")) or 0)


@app.post("/painel/guia/visto", dependencies=CSRF)
def guia_visto(u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    """O gestor respondeu ao convite do guia: não mostrar de novo."""
    u.guia_visto = True
    db.commit()
    return {"ok": True}


@app.get("/painel/avisos")
def avisos(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    """Avisos da operação (atrasos, faltas, aparelho novo...) dos últimos 7 dias."""
    e = u.empresa
    alertas = list(db.scalars(select(Alerta).where(
        Alerta.empresa_id == e.id, Alerta.criado_em >= banco.agora() - timedelta(days=7))
        .order_by(Alerta.criado_em.desc()).limit(60)))
    return render(request, "painel_avisos.html", u=u, e=e, aba="hoje", alertas=alertas,
                  novos=sum(1 for a in alertas if not a.lido))


@app.post("/painel/alertas/lidos", dependencies=CSRF)
def alertas_lidos(u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    for a in db.scalars(select(Alerta).where(Alerta.empresa_id == u.empresa_id,
                                             Alerta.lido.is_(False))):
        a.lido = True
    db.commit()
    return ir("/painel/avisos")


@app.post("/push", dependencies=CSRF)
async def push_inscrever(request: Request, u: Usuario = Depends(usuario_logado),
                         db: Session = Depends(get_db)):
    d = await request.json()
    chaves = d.get("keys") or {}
    endpoint = str(d.get("endpoint") or "")[:600]
    if not endpoint.startswith("https://") or not chaves.get("p256dh") or not chaves.get("auth"):
        return i18n.resposta_erro({"erro": "Inscrição inválida."}, 400)
    s = db.scalar(select(PushSub).where(PushSub.endpoint == endpoint)) or PushSub(endpoint=endpoint)
    s.usuario_id, s.p256dh, s.auth = u.id, str(chaves["p256dh"])[:200], str(chaves["auth"])[:100]
    db.add(s)
    db.commit()
    enviados = await asyncio.to_thread(_push_teste, u.id, u.gestor)
    return {"ok": True, "enviados": enviados}


def _push_teste(usuario_id: int, gestor: bool) -> int:
    with SessionLocal() as db:
        return push.enviar_usuario(
            db, usuario_id, "Avisos ativados",
            "Você vai receber aqui os atrasos e pontos não batidos." if gestor
            else "Você vai receber aqui os lembretes de ponto.",
            "/painel" if gestor else "/app")


# ============================================================ painel: escala
def equipe_da_escala(db: Session, e: Empresa) -> list[Usuario]:
    return list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True))
        .order_by(Usuario.nome)))


@app.get("/painel/escala")
def escala(request: Request, semana: str = "", dia: str = "", setor: str | None = None, local: str | None = None,
           u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    # O setor escolhido fica guardado: depois de salvar um turno a escala volta no mesmo setor.
    sid = setores_mod.ler_filtro(db, e.id, setor if setor is not None else request.session.get("setor_escala", 0))
    request.session["setor_escala"] = sid
    # Filtro por local: "" = todos, "principal" = endereço principal, ou o id de uma filial (fica guardado).
    lista_locais = fase3.locais_da(db, e.id)
    loc = local if local is not None else request.session.get("local_escala", "")
    if loc not in ("", "principal") and not (loc.isdigit() and any(l.id == int(loc) for l in lista_locais)):
        loc = ""
    request.session["local_escala"] = loc
    hoje = regras.hoje(e)
    base = data_iso(semana, hoje)
    seg = base - timedelta(days=base.weekday())
    dias = [seg + timedelta(days=i) for i in range(7)]
    equipe = equipe_da_escala(db, e)
    regras.gerar_da_fixa(db, e, dias[0], dias[-1])
    fixas = {(r.usuario_id, r.dia_semana) for r in db.scalars(select(banco.EscalaFixa).where(
        banco.EscalaFixa.empresa_id == e.id))}
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(dias[0], dias[-1])))}
    horas = {f.id: sum(regras.minutos_turno(turnos[(f.id, d)]) for d in dias
                       if (f.id, d) in turnos) for f in equipe}
    feriados = {f.data: f.nome for f in db.scalars(select(Feriado).where(
        Feriado.empresa_id == e.id, Feriado.data.between(dias[0], dias[-1])))}
    zaps = {f.id: fase3.zap(f.celular, fase3.texto_escala(f.primeiro_nome, e.nome, dias, turnos, f.id))
            for f in equipe}
    ofertas = trocas.do_painel(db, e)
    ver_custo = core.pode(u, "pagamento")
    custo = {(f.id, d): custos.custo_previsto(turnos.get((f.id, d)), f, e) for f in equipe for d in dias} if ver_custo else {}
    publicar_ids, publicacao = custos.mudados(db, e, seg, custos.assinaturas(turnos, equipe, dias))
    lista_setores = setores_mod.setores_da(db, e.id)
    nomes_setor = setores_mod.nomes(db, e.id)
    visiveis = [f for f in equipe if setores_mod.no_setor(f, sid)]
    if loc:
        # quem é do local (ficha) e quem foi escalado lá em algum dia desta semana
        lid = int(loc) if loc.isdigit() else None
        escalados = {uid for (uid, _), t in turnos.items()
                     if not t.folga and ((lid and t.local_id == lid) or (not lid and t.so_principal))}
        visiveis = [f for f in visiveis if f.local_id == lid or f.id in escalados]
    if lista_setores and not sid:   # agrupa por setor; quem não tem setor vai por último
        visiveis.sort(key=lambda f: (f.setor_id not in nomes_setor, nomes_setor.get(f.setor_id, "").lower(), f.nome.lower()))
    return render(request, "painel_escala.html", u=u, e=e, aba="escala", dias=dias,
                  setores=lista_setores, setor=sid, nomes_setor=nomes_setor,
                  agrupar=bool(lista_setores and not sid), equipe_toda=len(equipe),
                  destaque=data_iso(dia, None),
                  locais=lista_locais, local_filtro=loc, zaps=zaps, fixas=fixas,
                  hoje=hoje, equipe=visiveis, turnos=turnos, horas=horas, seg=seg,
                  ant=seg - timedelta(days=7), prox=seg + timedelta(days=7),
                  feriados=feriados, fechado=apuracao.fechado_ate(db, e),
                  ausencias=apuracao.ausencias_por_dia(db, e, dias[0], dias[-1]),
                  ofertas=ofertas, avisos_extra=trocas.avisos_extra(db, e, ofertas),
                  ver_custo=ver_custo, custo=custo, publicar_n=len(publicar_ids), publicacao=publicacao,
                  disp=disponibilidade.da_empresa(db, e.id), fora_disp=disponibilidade.fora,
                  fw=paises.regras_de(e).escala_justa.situacao_semana(db, e, seg),
                  curtos=paises.regras_de(e).escala_justa.descansos_curtos_na_grade(e, turnos, equipe, dias),
                  menor=paises.regras_de(e).menores.conferir_grade(db, e, equipe, turnos, dias),
                  travado=confirmacao.travado_ate(e), publicada=confirmacao.semana_publicada(db, e, seg),
                  cargos=sorted({f.cargo.strip() for f in equipe if f.cargo and f.cargo.strip()}
                                | {f["nome"] for f in triagem.funcoes_da(e)}))


@app.post("/painel/escala/turno", dependencies=CSRF)
def escala_turno(request: Request, usuario_id: int = Form(...), data: str = Form(...),
                 modo: str = Form("trabalho"), inicio: str = Form(""), fim: str = Form(""),
                 repetir: str = Form("dia"), obs: str = Form(""), intervalo: str = Form(""),
                 local_id: str = Form(""), fixar: str = Form(""), local_sempre: str = Form(""),
                 intervalo_inicio: str = Form(""), a_pedido: str = Form(""),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    fixo = bool(fixar)  # vale para toda semana daqui em diante (escala fixa)
    d = data_iso(data, None)
    if not d:
        return ir("/painel/escala")
    pausa = int(intervalo) if intervalo.strip().isdigit() else 0
    local = da_empresa(db, banco.Local, int(local_id), u) if local_id.isdigit() else None
    na_matriz = local_id == "principal"
    # Outro local vale só para os dias marcados agora; na escala fixa ele só entra se o gestor
    # marcar "usar este local também nas próximas semanas".
    local_na_fixa = bool(local_sempre) or not (local or na_matriz)
    fechado = apuracao.fechado_ate(db, u.empresa)
    seg = d - timedelta(days=d.weekday())
    volta = f"/painel/escala?semana={seg.isoformat()}"
    if modo == "trabalho" and not (HORA_RE.match(inicio) and HORA_RE.match(fim)):
        avisar(request, "Informe o horário de início e de fim (ex.: 08:00 e 17:00).", "erro")
        return ir(volta)
    if modo == "trabalho" and inicio == fim:
        avisar(request, "O início e o fim do turno não podem ser iguais.", "erro")
        return ir(volta)
    if modo == "trabalho" and not 0 <= pausa <= 240:
        avisar(request, "O intervalo precisa ficar entre 0 e 240 minutos.", "erro")
        return ir(volta)
    saida_int = intervalo_inicio.strip() if modo == "trabalho" else ""
    if saida_int and not HORA_RE.match(saida_int):
        avisar(request, "Hora de saída para o intervalo inválida (ex.: 12:00).", "erro")
        return ir(volta)
    if saida_int and not pausa:
        avisar(request, "Para marcar a hora do intervalo, informe também a duração em minutos.", "erro")
        return ir(volta)
    if saida_int and not regras.intervalo_cabe(inicio, fim, saida_int, pausa):
        avisar(request, f"O intervalo das {saida_int}, com {pausa} min, não cabe dentro do turno "
                        f"de {inicio} a {fim}.", "erro")
        return ir(volta)
    alvo = {"dia": [d], "uteis": [seg + timedelta(days=i) for i in range(5)],
            "sabado": [seg + timedelta(days=i) for i in range(6)],
            "semana": [seg + timedelta(days=i) for i in range(7)]}.get(repetir, [d])
    pulados = 0
    rp = paises.regras_de(u.empresa)
    antes_fw = rp.escala_justa.antes_de_mudar(db, f.id, alvo)
    trava = confirmacao.travado_ate(u.empresa)
    travados = 0
    for dia in alvo:
        if fechado and dia <= fechado:
            pulados += 1
            continue
        if trava and dia <= trava:
            travados += 1
            continue
        t = db.scalar(select(Turno).where(Turno.usuario_id == f.id, Turno.data == dia))
        if modo == "limpar":
            if t:
                db.delete(t)
            continue
        if not t:
            t = Turno(empresa_id=u.empresa_id, usuario_id=f.id, data=dia)
            db.add(t)
        t.folga = modo == "folga"
        t.inicio, t.fim = ("", "") if t.folga else (inicio, fim)
        t.intervalo_min = 0 if t.folga else pausa
        if t.intervalo_inicio != saida_int:
            t.intervalo_inicio, t.lembrou_int = saida_int, False
        t.local_id = local.id if local and not t.folga else None
        t.so_principal = na_matriz and not t.folga
        t.obs = obs.strip()[:200]
        t.fixa = fixo and local_na_fixa   # local avulso: o dia fica diferente da escala fixa
        if antes_fw.get(dia) != rp.escala_justa._valor(t):
            t.confirmado_em, t.lembrou_conf = None, False   # mudou: precisa confirmar de novo
    if fixo:
        for dia in alvo:
            r = db.scalar(select(banco.EscalaFixa).where(
                banco.EscalaFixa.usuario_id == f.id, banco.EscalaFixa.dia_semana == dia.weekday()))
            if modo == "limpar":
                if r:
                    db.delete(r)
                continue
            if not r:
                r = banco.EscalaFixa(empresa_id=u.empresa_id, usuario_id=f.id,
                                     dia_semana=dia.weekday())
                db.add(r)
            r.folga = modo == "folga"
            r.inicio, r.fim = ("", "") if r.folga else (inicio, fim)
            r.intervalo_min = 0 if r.folga else pausa
            r.intervalo_inicio = saida_int
            if local_na_fixa or r.folga:
                r.local_id = local.id if local and not r.folga else None
                r.so_principal = na_matriz and not r.folga
            r.obs = obs.strip()[:200]
        regras.limpar_futuros(db, [f.id], max(alvo), {dia.weekday() for dia in alvo})
        regras.registrar(db, u.empresa_id, u.nome, "escala fixa alterada",
                         f"{f.nome}: " + ", ".join(regras.DIAS_SEMANA[dia.weekday()] for dia in alvo)
                         + (" sem escala" if modo == "limpar" else " folga" if modo == "folga"
                            else f" {inicio}–{fim}") + ", toda semana.")
    db.flush()
    premios = []
    if u.empresa.pais == "US":   # Fair Workweek: mudança em escala publicada dentro do prazo
        premios = rp.escala_justa.avaliar_mudancas(db, u.empresa, f.id, alvo, antes_fw, u.nome, bool(a_pedido))
        for p in premios:
            regras.registrar(db, u.empresa_id, u.nome, "predictability pay",
                             f"{f.nome}, {p.data:%m/%d}: {p.antes or '—'} → {p.depois or '—'} ({regras.fmt_min(p.minutos)}).")
    db.commit()
    regras.gerar_da_fixa(db, u.empresa, seg, max(seg + timedelta(days=6), regras.hoje(u.empresa)
                                                 + timedelta(days=regras.DIAS_A_FRENTE)))
    problemas_menor = rp.menores.problemas_ao_salvar(db, u.empresa, f, [x for x in alvo if modo != "limpar"])
    if problemas_menor:
        avisar(request, f"{f.nome} é menor de idade e este turno passa do limite da lei: "
                        f"{'; '.join(problemas_menor[:4])}.", "erro")
    if premios:
        avisar(request, f"Mudança em escala já publicada dentro do prazo de aviso: {f.nome} ganha "
                        f"{regras.fmt_min(sum(p.minutos for p in premios))} de predictability pay.", "erro")
    curtos = (rp.escala_justa.curtos_ao_salvar(db, f.id, [x for x in alvo if modo != "limpar"])
              if u.empresa.pais == "BR" else [])
    if curtos:
        avisar(request, f"{f.nome} fica com menos de 11 horas de descanso entre um turno e outro em "
                        f"{', '.join(f'{d:%d/%m}' for d in curtos[:4])} (CLT art. 66).", "erro")
    if pulados:
        avisar(request, f"{pulados} dia(s) não foram alterados porque estão num período de ponto fechado.", "erro")
    if travados:
        avisar(request, f"{travados} dia(s) não foram alterados porque a escala trava {u.empresa.travar_escala_dias} dia(s) depois do dia.", "erro")
    return ir(volta)


@app.post("/painel/escala/fixar", dependencies=CSRF)
def escala_fixar(request: Request, semana: str = Form(...),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    """Transforma a semana mostrada na escala fixa de toda a equipe."""
    seg = data_iso(semana, None)
    if not seg:
        return ir("/painel/escala")
    seg -= timedelta(days=seg.weekday())
    e = u.empresa
    equipe = equipe_da_escala(db, e)
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(seg, seg + timedelta(days=6))))}
    if not turnos:
        avisar(request, "Esta semana está vazia: monte a escala antes de torná-la fixa.", "erro")
        return ir(f"/painel/escala?semana={seg.isoformat()}")
    fixas = {(r.usuario_id, r.dia_semana): r for r in db.scalars(select(banco.EscalaFixa).where(
        banco.EscalaFixa.empresa_id == e.id))}
    for f in equipe:
        for i in range(7):
            t, r = turnos.get((f.id, seg + timedelta(days=i))), fixas.get((f.id, i))
            if t:
                if not r:
                    r = banco.EscalaFixa(empresa_id=e.id, usuario_id=f.id, dia_semana=i)
                    db.add(r)
                regras.copiar_para(r, t)
                t.fixa = True
            elif r:
                db.delete(r)
    regras.limpar_futuros(db, [f.id for f in equipe], seg + timedelta(days=6), set(range(7)))
    regras.registrar(db, e.id, u.nome, "escala fixa alterada",
                     f"A semana de {seg:%d/%m/%Y} virou a escala fixa de toda a equipe.")
    db.commit()
    regras.gerar_da_fixa(db, e, seg, regras.hoje(e) + timedelta(days=regras.DIAS_A_FRENTE))
    avisar(request, "Pronto: esta semana agora se repete sozinha, toda semana.")
    return ir(f"/painel/escala?semana={seg.isoformat()}")


@app.post("/painel/escala/copiar", dependencies=CSRF)
def escala_copiar(request: Request, semana: str = Form(...),
                  u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    seg = data_iso(semana, None)
    if not seg:
        return ir("/painel/escala")
    seg -= timedelta(days=seg.weekday())
    ja = {(t.usuario_id, t.data) for t in db.scalars(select(Turno).where(
        Turno.empresa_id == u.empresa_id,
        Turno.data.between(seg, seg + timedelta(days=6))))}
    ativos = {f.id for f in equipe_da_escala(db, u.empresa)}
    n = 0
    for t in db.scalars(select(Turno).where(
            Turno.empresa_id == u.empresa_id,
            Turno.data.between(seg - timedelta(days=7), seg - timedelta(days=1)))):
        novo = t.data + timedelta(days=7)
        if t.usuario_id in ativos and (t.usuario_id, novo) not in ja:
            db.add(Turno(empresa_id=t.empresa_id, usuario_id=t.usuario_id, data=novo,
                         inicio=t.inicio, fim=t.fim, folga=t.folga, obs=t.obs,
                         intervalo_min=t.intervalo_min, local_id=t.local_id, so_principal=t.so_principal,
                         intervalo_inicio=t.intervalo_inicio))   # copiar a semana é decisão do gestor: leva o local
            n += 1
    db.commit()
    avisar(request, f"{n} dia(s) copiado(s) da semana anterior." if n
           else "Nada para copiar: a semana anterior está vazia ou esta já está preenchida.",
           "ok" if n else "erro")
    return ir(f"/painel/escala?semana={seg.isoformat()}")


# ============================================================ painel: ponto
def periodo(e: Empresa, de: str, ate: str) -> tuple[date, date]:
    hoje = regras.hoje(e)
    fim = data_iso(ate, hoje)
    ini = data_iso(de, fim - timedelta(days=6))
    if ini > fim:
        ini = fim
    if (fim - ini).days > 92:
        ini = fim - timedelta(days=92)
    return ini, fim


@app.get("/painel/ponto")
def ponto(request: Request, de: str = "", ate: str = "", fid: int = Query(0, alias="func"),
          mes: str = "", setor: str = "", u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    ini, fim = periodo(e, de, ate)
    sid = setores_mod.ler_filtro(db, e.id, setor)
    linhas, totais = _espelho_setor(db, e, ini, fim, fid, sid)
    # Calendário: o mês escolhido (?mes=AAAA-MM) ou o do fim do período.
    try:
        ano_m, mes_m = (int(x) for x in mes.split("-")) if mes else (fim.year, fim.month)
        date(ano_m, mes_m, 1)
    except ValueError:
        ano_m, mes_m = fim.year, fim.month
    ini_m = date(ano_m, mes_m, 1)
    fim_m = (ini_m + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    linhas_m = linhas if (ini <= ini_m and fim_m <= fim) else _espelho_setor(
        db, e, ini_m - timedelta(days=7), fim_m + timedelta(days=7), fid, sid)[0]
    cal = calendario.mes(db, e, ano_m, mes_m, linhas_m, regras.hoje(e))
    resumo = dict(pessoas=len(totais), minutos=sum(t["minutos"] for t in totais),
                  previsto=sum(t["previsto"] for t in totais),
                  atrasos=sum(t["atrasos"] for t in totais),
                  faltando=sum(1 for l in linhas if l["incompleto"]),
                  fora=sum(1 for l in linhas if l["fora"]),
                  manuais=sum(1 for l in linhas for p in l["pares"] for x in p if x and x.manual))
    equipe = [p for p in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id)
                                    .order_by(Usuario.nome)) if setores_mod.no_setor(p, sid)]
    ajustes = list(db.scalars(select(Ajuste).where(
        Ajuste.empresa_id == e.id, Ajuste.situacao == "pendente").order_by(Ajuste.criado_em)))
    return render(request, "painel_ponto.html", u=u, e=e, aba="ponto", ini=ini, fim=fim,
                  n_problemas=len(ponto_problemas.problemas(db, e)),
                  func=fid, linhas=linhas, totais=totais, equipe=equipe,
                  setores=setores_mod.setores_da(db, e.id), setor=sid,
                  hoje=regras.hoje(e), ajustes=ajustes, cal=cal, resumo=resumo)


def _espelho_setor(db: Session, e: Empresa, ini: date, fim: date, fid: int, sid: int):
    """regras.espelho, só com as pessoas do setor escolhido (sid 0 = todos)."""
    linhas, totais = regras.espelho(db, e, ini, fim, fid or None)
    if sid:
        linhas = [l for l in linhas if setores_mod.no_setor(l["u"], sid)]
        totais = [t for t in totais if setores_mod.no_setor(t["u"], sid)]
    return linhas, totais


@app.get("/painel/ponto.csv")
def ponto_csv(de: str = "", ate: str = "", fid: int = Query(0, alias="func"), setor: str = "",
              u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    ini, fim = periodo(e, de, ate)
    linhas, _resto = _espelho_setor(db, e, ini, fim, fid, setores_mod.ler_filtro(db, e.id, setor))
    saida = io.StringIO()
    # Planilha para o Excel em português: ponto e vírgula separa as colunas.
    w = csv.writer(saida, delimiter=";")
    w.writerow([i18n._(c) for c in ("Funcionário", "Data", "Escala", "Entradas e saídas", "Horas",
                                    "Atraso (min)", "Fora da área", "Incompleto")])
    sim = i18n._("sim")
    for l in sorted(linhas, key=lambda l: (l["u"].nome.lower(), l["dia"])):
        t = l["turno"]
        esc = "" if not t else i18n._("Folga") if t.folga else f"{t.inicio}-{t.fim}"
        pares = " | ".join(
            f"{regras.para_local(e, a.momento):%H:%M}" if a else "?"
            for par in l["pares"] for a in par)
        w.writerow([l["u"].nome, l["dia"].strftime("%d/%m/%Y"), esc, pares,
                    regras.fmt_min(l["minutos"]), l["atraso"],
                    sim if l["fora"] else "", sim if l["incompleto"] else ""])
    return Response("﻿" + saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="ponto-{ini}-{fim}.csv"'})


@app.post("/painel/ponto/manual", dependencies=CSRF)
def ponto_manual(request: Request, usuario_id: int = Form(...), tipo: str = Form(...),
                 data: str = Form(...), hora: str = Form(...), obs: str = Form(""),
                 volta: str = Form("/painel/ponto"),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    e = u.empresa
    d = data_iso(data, None)
    volta = volta if volta.startswith("/painel/ponto") else "/painel/ponto"
    if not d or not HORA_RE.match(hora) or tipo not in ("entrada", "saida"):
        avisar(request, "Confira a data, a hora e o tipo da batida.", "erro")
        return ir(volta)
    local = datetime.combine(d, regras.hm(hora))
    momento = regras.para_utc(e, local)
    if momento > banco.agora() + timedelta(minutes=5):
        avisar(request, "Não dá para lançar uma batida no futuro.", "erro")
        return ir(volta)
    if apuracao.esta_fechado(db, e, regras.dia_de(local)):
        avisar(request, apuracao.MSG_FECHADO, "erro")
        return ir(volta)
    b = regras.lancar_manual(db, f, tipo, local, obs.strip() or f"Lançada por {u.nome}")
    regras.registrar(db, e.id, u.nome, "batida manual",
                     f"Lançou {'entrada' if tipo == 'entrada' else 'saída'} de {f.nome} em "
                     f"{local:%d/%m/%Y %H:%M}. Motivo: {obs.strip() or 'não informado'}",
                     usuario_id=f.id, dia=b.dia)
    db.commit()
    avisar(request, f"Batida de {f.primeiro_nome} lançada.")
    return ir(volta)


@app.post("/painel/ponto/{bid}/apagar", dependencies=CSRF)
def ponto_apagar(request: Request, bid: int, volta: str = Form("/painel/ponto"),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    b = da_empresa(db, Batida, bid, u)
    volta = volta if volta.startswith("/painel/ponto") else "/painel/ponto"
    if apuracao.esta_fechado(db, u.empresa, b.dia):
        avisar(request, apuracao.MSG_FECHADO, "erro")
        return ir(volta)
    if not b.anulada:
        b.anulada, b.anulada_por, b.anulada_em = True, u.nome, banco.agora()
        origem = "lançada à mão" if b.manual else "registrada pelo aplicativo"
        regras.registrar(db, u.empresa_id, u.nome, "batida anulada",
                         f"Anulou a {'entrada' if b.tipo == 'entrada' else 'saída'} de "
                         f"{b.usuario.nome} de {regras.para_local(u.empresa, b.momento):%d/%m/%Y %H:%M} "
                         f"({origem}). O registro original continua guardado.",
                         usuario_id=b.usuario_id, dia=b.dia)
        db.commit()
    avisar(request, "Batida anulada. O registro original fica guardado no histórico.")
    return ir(volta if volta.startswith("/painel/ponto") else "/painel/ponto")


@app.get("/painel/ponto/foto/{bid}")
def ponto_foto(bid: int, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    b = da_empresa(db, Batida, bid, u)
    arquivo = PASTA_FOTOS / str(b.empresa_id) / b.foto
    if not b.foto or not arquivo.is_file():
        return Response("Foto não disponível (o prazo de guarda pode ter vencido).", 404)
    return FileResponse(arquivo, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.post("/painel/ajustes/{aid}", dependencies=CSRF)
def ajuste_decidir(request: Request, tarefas_bg: BackgroundTasks, aid: int,
                   acao: str = Form(...), resposta: str = Form(""),
                   u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    a = da_empresa(db, Ajuste, aid, u)
    if a.situacao != "pendente" or acao not in ("aprovar", "recusar"):
        return ir("/painel/ponto")
    tipo = "entrada" if a.tipo == "entrada" else "saída"
    quando = f"{a.data:%d/%m/%Y} às {a.hora}"
    if acao == "aprovar" and apuracao.esta_fechado(db, u.empresa, a.data):
        avisar(request, apuracao.MSG_FECHADO, "erro")
        return ir("/painel/ponto")
    if acao == "aprovar":
        regras.lancar_manual(db, a.usuario, a.tipo, datetime.combine(a.data, regras.hm(a.hora)),
                             f"Ajuste pedido: {a.motivo}")
        a.situacao = "aprovado"
    else:
        a.situacao = "recusado"
    a.resposta, a.decidido_por, a.decidido_em = resposta.strip()[:300], u.nome, banco.agora()
    regras.registrar(db, u.empresa_id, u.nome, f"ajuste {a.situacao}",
                     f"Pedido de {a.usuario.nome}: {tipo} em {quando}. Motivo: {a.motivo}"
                     + (f" Resposta: {a.resposta}" if a.resposta else ""),
                     usuario_id=a.usuario_id, dia=a.data)
    db.commit()
    tarefas_bg.add_task(push.avisar_usuarios, [(
        a.usuario_id, f"Ajuste de ponto {a.situacao}",
        f"Seu pedido de {tipo} em {quando} foi {a.situacao}.")])
    avisar(request, f"Pedido de {a.usuario.primeiro_nome} {a.situacao}.")
    return ir("/painel/ponto")


POR_PAGINA = 10


def _paginar(db: Session, consulta, pagina: int) -> dict:
    """Corta a consulta em blocos de POR_PAGINA; devolve itens, página atual e total de páginas."""
    total = db.scalar(select(func.count()).select_from(consulta.order_by(None).subquery())) or 0
    paginas = max(1, -(-total // POR_PAGINA))
    pagina = min(max(1, pagina), paginas)
    itens = list(db.scalars(consulta.offset((pagina - 1) * POR_PAGINA).limit(POR_PAGINA)))
    return {"itens": itens, "pagina": pagina, "paginas": paginas, "total": total}


@app.get("/painel/historico")
def historico(request: Request, u: Usuario = Depends(gestor_logado),
              db: Session = Depends(get_db), ph: int = 1, pa: int = 1):
    alt = _paginar(db, select(Historico).where(Historico.empresa_id == u.empresa_id)
                   .order_by(Historico.criado_em.desc(), Historico.id.desc()), ph)
    anu = _paginar(db, select(Batida).where(
        Batida.empresa_id == u.empresa_id, Batida.anulada.is_(True))
        .order_by(Batida.anulada_em.desc(), Batida.id.desc()), pa)
    return render(request, "painel_historico.html", u=u, e=u.empresa, aba="ponto",
                  itens=alt["itens"], anuladas=anu["itens"], pag_alt=alt, pag_anu=anu)


# ============================================================ painel: tarefas
@app.get("/painel/tarefas")
def tarefas(request: Request, u: Usuario = Depends(gestor_logado),
            db: Session = Depends(get_db)):
    e = u.empresa
    todas = list(db.scalars(select(Tarefa).where(Tarefa.empresa_id == e.id, Tarefa.ativo.is_(True))
                            .order_by(Tarefa.ordem, Tarefa.id)))
    dia = regras.hoje(e)
    ontem = dia - timedelta(days=1)
    return render(request, "painel_tarefas.html", u=u, e=e, aba="tarefas", tarefas=todas,
                  equipe=equipe_da_escala(db, e), dia=dia,
                  hoje=regras.tarefas_do_dia(db, e, dia), ontem=ontem,
                  de_ontem=regras.tarefas_do_dia(db, e, ontem))


def _dias_do_form(form) -> str:
    return "".join(d for d in "0123456" if form.get(f"d{d}"))


@app.post("/painel/tarefas", dependencies=CSRF)
async def tarefa_nova(request: Request, u: Usuario = Depends(gestor_logado),
                      db: Session = Depends(get_db)):
    form = await request.form()
    titulo = str(form.get("titulo", "")).strip()[:160]
    dias = _dias_do_form(form)
    if not titulo or not dias:
        avisar(request, "Escreva a tarefa e marque pelo menos um dia da semana.", "erro")
        return ir("/painel/tarefas")
    uid = int(form.get("usuario_id") or 0)
    if uid:
        da_empresa(db, Usuario, uid, u)
    ordem = (db.scalar(select(func.max(Tarefa.ordem)).where(
        Tarefa.empresa_id == u.empresa_id)) or 0) + 1
    db.add(Tarefa(empresa_id=u.empresa_id, titulo=titulo, dias=dias,
                  usuario_id=uid or None, ordem=ordem))
    db.commit()
    return ir("/painel/tarefas")


@app.post("/painel/tarefas/{tid}", dependencies=CSRF)
async def tarefa_editar(request: Request, tid: int, u: Usuario = Depends(gestor_logado),
                        db: Session = Depends(get_db)):
    t = da_empresa(db, Tarefa, tid, u)
    form = await request.form()
    if form.get("acao") == "apagar":
        t.ativo = False  # fica no histórico de quem já fez
    else:
        titulo = str(form.get("titulo", "")).strip()[:160]
        dias = _dias_do_form(form)
        uid = int(form.get("usuario_id") or 0)
        if uid:
            da_empresa(db, Usuario, uid, u)
        if titulo and dias:
            t.titulo, t.dias, t.usuario_id = titulo, dias, uid or None
        else:
            avisar(request, "Escreva a tarefa e marque pelo menos um dia da semana.", "erro")
    db.commit()
    return ir("/painel/tarefas")


# ============================================================ painel: equipe
def link_convite(request: Request, f: Usuario) -> str:
    return f"{base_url(request)}/convite/{f.convite_token}" if f.convite_token else ""


@app.get("/painel/equipe")
def equipe(request: Request, setor: str = "", u: Usuario = Depends(gestor_logado),
           db: Session = Depends(get_db)):
    # O dono vê todos, gerentes incluídos (para editar o cadastro deles); as permissões
    # continuam na tela Gerentes. Um gerente vê os funcionários, o dono e a si mesmo.
    filtro = [Usuario.empresa_id == u.empresa_id, Usuario.cadastro_excluido_em.is_(None)]
    if not u.dono:
        filtro.append((Usuario.papel != "gestor") | Usuario.dono.is_(True) | (Usuario.id == u.id))
    sid = setores_mod.ler_filtro(db, u.empresa_id, setor)
    if setores_mod.filtro_sql(sid) is not None:
        filtro.append(setores_mod.filtro_sql(sid))
    pessoas = list(db.scalars(select(Usuario).where(*filtro)
        .order_by(Usuario.ativo.desc(), Usuario.papel.desc(), Usuario.nome)))
    convites = {}
    for f in pessoas:
        link = link_convite(request, f)
        if link:
            msg = (f"Olá {f.primeiro_nome}! Este é o seu acesso ao {master.plataforma()['nome']} da "
                   f"{u.empresa.nome}, para ver sua escala e bater o ponto: {link}")
            convites[f.id] = {"link": link,
                              "zap": f"https://wa.me/55{f.celular}?text={quote(msg)}"}
    aparelhos: dict[int, list[Aparelho]] = {}
    for a in db.scalars(select(Aparelho).join(Usuario).where(
            Usuario.empresa_id == u.empresa_id, Aparelho.situacao != "recusado")
            .order_by(Aparelho.situacao, Aparelho.criado_em)):
        aparelhos.setdefault(a.usuario_id, []).append(a)
    return render(request, "painel_equipe.html", u=u, e=u.empresa, aba="equipe",
                  pessoas=pessoas, convites=convites, aparelhos=aparelhos,
                  com_registros=desligamento.com_registros(db, [p.id for p in pessoas]),
                  TIPOS=desligamento.TIPOS, AVISOS=desligamento.AVISOS, hoje=regras.hoje(u.empresa),
                  setores=setores_mod.setores_da(db, u.empresa_id), setor=sid,
                  nomes_setor=setores_mod.nomes(db, u.empresa_id),
                  meu_alcance=setores_mod.descrever_alcance(db, u))


@app.post("/painel/equipe", dependencies=CSRF)
def equipe_novo(request: Request, nome: str = Form(""), celular: str = Form(""),
                cargo: str = Form(""), u: Usuario = Depends(gestor_logado),
                db: Session = Depends(get_db)):
    nome, cel = nome.strip()[:120], celular_limpo(celular)
    if not nome or not celular_valido(cel):
        avisar(request, "Informe o nome e o celular com DDD (ex.: 98 99999-1234).", "erro")
    elif db.scalar(select(Usuario.id).where(Usuario.celular == cel).execution_options(alcance_livre=True)):
        avisar(request, "Este celular já está cadastrado.", "erro")
    elif master.sem_vaga(db, u.empresa):
        avisar(request, master.sem_vaga(db, u.empresa), "erro")
    else:
        novo = Usuario(empresa_id=u.empresa_id, nome=nome, celular=cel,
                       cargo=cargo.strip()[:80], papel="funcionario",
                       convite_token=secrets.token_urlsafe(18))
        core.encaixar_no_alcance(u, novo)
        db.add(novo)
        db.commit()
        avisar(request, f"{nome} cadastrado. Envie o link de acesso para o celular dele(a).")
    return ir("/painel/equipe")


@app.post("/painel/equipe/{fid}", dependencies=CSRF)
def equipe_editar(request: Request, fid: int, acao: str = Form("salvar"),
                  nome: str = Form(""), celular: str = Form(""), cargo: str = Form(""),
                  na_escala: str = Form(""), aparelho_id: str = Form(""),
                  u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, fid, u)
    if f.gestor and f.id != u.id and not u.dono:
        avisar(request, "Só o dono da conta muda os dados de outro gestor.", "erro")
        return ir("/painel/equipe")
    if acao in ("autorizar_aparelho", "recusar_aparelho"):
        ap = db.get(Aparelho, int(aparelho_id or 0))
        if ap and ap.usuario_id == f.id and ap.situacao == "pendente":
            if acao == "autorizar_aparelho":
                for outro in db.scalars(select(Aparelho).where(
                        Aparelho.usuario_id == f.id, Aparelho.id != ap.id)):
                    db.delete(outro)  # o aparelho antigo deixa de valer
                ap.situacao = "autorizado"
                avisar(request, f"Novo aparelho de {f.primeiro_nome} liberado. O anterior deixou de valer.")
            else:
                ap.situacao = "recusado"
                avisar(request, f"Aparelho de {f.primeiro_nome} recusado.")
            regras.registrar(db, u.empresa_id, u.nome, f"aparelho {ap.situacao}",
                             f"{ap.nome} de {f.nome}.")
    elif acao == "liberar_troca":
        for ap in db.scalars(select(Aparelho).where(Aparelho.usuario_id == f.id)):
            db.delete(ap)
        regras.registrar(db, u.empresa_id, u.nome, "troca de aparelho liberada",
                         f"O próximo aparelho em que {f.nome} bater o ponto fica autorizado.")
        avisar(request, f"Pronto: o próximo aparelho em que {f.primeiro_nome} bater o ponto fica autorizado.")
    elif acao == "novo_link":
        f.convite_token, f.senha_hash = secrets.token_urlsafe(18), None
        for ap in db.scalars(select(Aparelho).where(Aparelho.usuario_id == f.id)):
            db.delete(ap)
        avisar(request, f"Novo link de acesso gerado para {f.primeiro_nome}. A senha antiga deixou de valer.")
    elif acao == "desativar" and f.id != u.id:
        f.ativo = False
        avisar(request, f"{f.primeiro_nome} foi desativado(a) e não entra mais no sistema.")
    elif acao == "reativar":
        if f.papel == "funcionario" and master.sem_vaga(db, u.empresa):
            avisar(request, master.sem_vaga(db, u.empresa), "erro")
            return ir("/painel/equipe")
        f.ativo = True
        f.desligado_em, f.desligamento_tipo, f.aviso_previo, f.desligamento_obs = None, "", "", ""
    elif acao == "salvar":
        nome, cel = nome.strip()[:120], celular_limpo(celular)
        if not nome:
            avisar(request, "Informe o nome.", "erro")
            return ir("/painel/equipe")
        if cel or not f.gestor:
            if not celular_valido(cel):
                avisar(request, "Celular inválido. Use DDD + número.", "erro")
                return ir("/painel/equipe")
            if db.scalar(select(Usuario.id).where(Usuario.celular == cel, Usuario.id != f.id).execution_options(alcance_livre=True)):
                avisar(request, "Este celular já está cadastrado.", "erro")
                return ir("/painel/equipe")
        f.nome, f.celular, f.cargo = nome, cel or None, cargo.strip()[:80]
        f.na_escala = bool(na_escala)
        avisar(request, "Dados salvos.")
    db.commit()
    return ir("/painel/equipe")


# ============================================================ painel: configurações
def _numero(s: str) -> float | None:
    try:
        return float(s.strip().replace(",", "."))
    except ValueError:
        return None


@app.get("/painel/config")
def config(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    if e.pais == "US":   # tela americana (templates/us/painel_config.html)
        from .paises.us import telas as telas_us
        return render(request, "painel_config.html", u=u, e=e, aba="config",
                      **telas_us.contexto_config(db, e))
    return render(request, "painel_config.html", u=u, e=e, aba="config", fusos=regras.FUSOS,
                  sm=salario_minimo.situacao(db, e, regras.hoje(e)),
                  sm_atualizado=salario_minimo.ATUALIZADO_EM, REGIMES=faturamento.REGIMES,
                  facial_resumo=facial.resumo_equipe(db, e),
                  contador=parceiros.contador_da_empresa(db, e))


@app.post("/painel/config", dependencies=CSRF)
def config_salvar(request: Request, nome: str = Form(""), endereco: str = Form(""),
                  lat: str = Form(""), lng: str = Form(""), raio_m: str = Form("150"),
                  tolerancia_min: str = Form("10"), fuso: str = Form("America/Sao_Paulo"),
                  bloquear_fora: str = Form(""), ponto_na_lotacao: str = Form(""), exigir_foto: str = Form(""),
                  aparelho_unico: str = Form(""), ajuste_dias: str = Form("7"),
                  fotos_dias: str = Form("45"), controle: str = Form("banco"),
                  intervalo_padrao_min: str = Form("60"), intervalo_auto: str = Form(""),
                  tolerancia_dia_min: str = Form("10"), pct_extra_normal: str = Form("50"),
                  pct_extra_especial: str = Form("100"), pct_noturno: str = Form("20"),
                  noturno_reduzida: str = Form(""), intervalo_obrigatorio: str = Form(""),
                  dsr_faltas: str = Form(""), dsr_extras: str = Form(""),
                  intervalo_minimo_min: str = Form("60"),
                  estado: str = Form(""), semana_inicio: str = Form("6"),
                  frequencia: str = Form("biweekly"), periodo_ancora: str = Form(""),
                  pct_feriado: str = Form("0"),
                  u: Usuario = Depends(gestor_logado),
                  db: Session = Depends(get_db)):
    e = u.empresa
    antes = (e.lat, e.lng, e.raio_m, e.bloquear_fora, e.tolerancia_min, e.exigir_foto,
             e.aparelho_unico, e.ajuste_dias, e.fotos_dias, e.ponto_na_lotacao)
    eua = e.pais == "US"
    if eua:
        from .paises.us import telas as telas_us
        if not telas_us.salvar_jornada(request, db, e, u, dict(
                estado=estado, semana_inicio=semana_inicio, frequencia=frequencia,
                periodo_ancora=periodo_ancora, intervalo_padrao_min=intervalo_padrao_min,
                intervalo_auto=intervalo_auto, tolerancia_dia_min=tolerancia_dia_min,
                pct_noturno=pct_noturno, pct_feriado=pct_feriado,
                intervalo_obrigatorio=intervalo_obrigatorio)):
            db.rollback()
            return ir("/painel/config")
        # as regras brasileiras ficam como estão
        intervalo_minimo_min, pct_extra_normal, pct_extra_especial = (
            str(e.intervalo_minimo_min), str(e.pct_extra_normal), str(e.pct_extra_especial))
        pct_noturno, controle = str(e.pct_noturno), e.controle
        noturno_reduzida, dsr_faltas, dsr_extras = (
            "1" if e.noturno_reduzida else "", "1" if e.dsr_faltas else "", "1" if e.dsr_extras else "")
    jornada_antes = (e.controle, e.intervalo_padrao_min, e.intervalo_auto, e.tolerancia_dia_min,
                     e.pct_extra_normal, e.pct_extra_especial, e.pct_noturno,
                     e.noturno_reduzida, e.intervalo_obrigatorio, e.dsr_faltas, e.dsr_extras,
                     e.intervalo_minimo_min)
    ip_, td = _numero(intervalo_padrao_min), _numero(tolerancia_dia_min)
    if ip_ is None or not 0 <= ip_ <= 240 or td is None or not 0 <= td <= 60:
        avisar(request, "Confira a jornada: intervalo de 0 a 240 min e tolerância diária de 0 a 60 min.", "erro")
        return ir("/painel/config")
    im = _numero(intervalo_minimo_min)
    if im is None or not 30 <= im <= 60:
        avisar(request, "O intervalo mínimo para jornadas de mais de 6 horas fica entre 30 e 60 minutos.", "erro")
        return ir("/painel/config")
    pn, pe = _numero(pct_extra_normal), _numero(pct_extra_especial)
    pnot = _numero(pct_noturno)
    if (pn is None or not 0 <= pn <= 300 or pe is None or not 0 <= pe <= 300
            or pnot is None or not 0 <= pnot <= 300):
        avisar(request, "Confira os percentuais de hora extra e de adicional noturno: de 0 a 300.", "erro")
        return ir("/painel/config")
    aj, fd = _numero(ajuste_dias), _numero(fotos_dias)
    if aj is None or not 0 <= aj <= 60 or fd is None or not 1 <= fd <= 45:
        avisar(request, "Confira os prazos: ajuste de 0 a 60 dias e fotos de 1 a 45 dias.", "erro")
        return ir("/painel/config")
    la, lo = _numero(lat), _numero(lng)
    raio, tol = _numero(raio_m), _numero(tolerancia_min)
    if (lat.strip() or lng.strip()) and (la is None or lo is None
                                         or not -90 <= la <= 90 or not -180 <= lo <= 180):
        avisar(request, "Latitude ou longitude inválida. Use o botão “Usar minha localização”.", "erro")
        return ir("/painel/config")
    if raio is None or not 30 <= raio <= 5000:
        avisar(request, "O raio precisa ficar entre 30 e 5000 metros.", "erro")
        return ir("/painel/config")
    if tol is None or not 0 <= tol <= 120:
        avisar(request, "A tolerância precisa ficar entre 0 e 120 minutos.", "erro")
        return ir("/painel/config")
    e.nome = nome.strip()[:120] or e.nome
    e.endereco = endereco.strip()[:300]
    e.lat, e.lng = (la, lo) if la is not None and lo is not None else (None, None)
    e.raio_m, e.tolerancia_min = int(raio), int(tol)
    e.bloquear_fora = bool(bloquear_fora)
    e.ponto_na_lotacao = bool(ponto_na_lotacao)
    e.exigir_foto, e.aparelho_unico = bool(exigir_foto), bool(aparelho_unico)
    e.ajuste_dias, e.fotos_dias = int(aj), int(fd)
    if not eua:
        e.controle = controle if controle in ("banco", "extra") else "banco"
        e.intervalo_padrao_min, e.tolerancia_dia_min = int(ip_), int(td)
        e.intervalo_auto = bool(intervalo_auto)
        e.pct_extra_normal, e.pct_extra_especial, e.pct_noturno = int(pn), int(pe), int(pnot)
        e.noturno_reduzida, e.intervalo_obrigatorio = bool(noturno_reduzida), bool(intervalo_obrigatorio)
        e.dsr_faltas, e.dsr_extras = bool(dsr_faltas), bool(dsr_extras)
        e.intervalo_minimo_min = int(im)
    if not eua and jornada_antes != (e.controle, e.intervalo_padrao_min, e.intervalo_auto, e.tolerancia_dia_min,
                         e.pct_extra_normal, e.pct_extra_especial, e.pct_noturno,
                         e.noturno_reduzida, e.intervalo_obrigatorio, e.dsr_faltas, e.dsr_extras,
                         e.intervalo_minimo_min):
        regras.registrar(db, e.id, u.nome, "regras da jornada alteradas",
                         f"Controle por {'banco de horas' if e.controle == 'banco' else 'hora extra'}; "
                         f"intervalo padrão {e.intervalo_padrao_min} min "
                         f"({'desconta sozinho' if e.intervalo_auto else 'não desconta sozinho'}); "
                         f"tolerância diária {e.tolerancia_dia_min} min; hora extra a "
                         f"{e.pct_extra_normal}% em dia normal e {e.pct_extra_especial}% em "
                         f"folga ou feriado; adicional noturno de {e.pct_noturno}% "
                         f"({'com' if e.noturno_reduzida else 'sem'} hora reduzida); intervalo "
                         f"{'precisa ser batido' if e.intervalo_obrigatorio else 'não precisa ser batido'}; "
                         f"intervalo mínimo acima de 6 h: {e.intervalo_minimo_min} min; "
                         f"DSR: {'desconta' if e.dsr_faltas else 'não desconta'} na semana com falta, "
                         f"{'calcula' if e.dsr_extras else 'não calcula'} o reflexo das extras.")
    if fuso in dict(telas_us.FUSOS if eua else regras.FUSOS):
        e.fuso = fuso
    depois = (e.lat, e.lng, e.raio_m, e.bloquear_fora, e.tolerancia_min, e.exigir_foto,
              e.aparelho_unico, e.ajuste_dias, e.fotos_dias, e.ponto_na_lotacao)
    if antes != depois:
        regras.registrar(db, e.id, u.nome, "regras do ponto alteradas",
                         f"Local {e.lat}, {e.lng}; raio {e.raio_m} m; "
                         f"{'bloqueia' if e.bloquear_fora else 'aceita'} fora do raio; "
                         f"ponto {'só no local de cada pessoa' if e.ponto_na_lotacao else 'em qualquer endereço'}; "
                         f"tolerância {e.tolerancia_min} min; foto {'obrigatória' if e.exigir_foto else 'não exigida'}; "
                         f"aparelho único {'ligado' if e.aparelho_unico else 'desligado'}; "
                         f"ajuste até {e.ajuste_dias} dias; fotos guardadas por {e.fotos_dias} dias.")
    db.commit()
    avisar(request, "Configurações salvas.")
    return ir("/painel/config")


# ============================================================ app do funcionário
@app.get("/app")
def app_hoje(request: Request, codigo: str = "", u: Usuario = Depends(usuario_logado),
             db: Session = Depends(get_db)):
    e = u.empresa
    al = regras.agora_local(e)
    aberta = regras.ponto_aberto(db, u)
    dia = aberta.dia if aberta else regras.dia_de(al)
    turno = None
    if aberta and aberta.turno_id:
        turno = db.get(Turno, aberta.turno_id)
    if not turno:
        turno = regras.turno_para_entrada(db, u, al) or db.scalar(select(Turno).where(
            Turno.usuario_id == u.id, Turno.data == dia))
    batidas = list(db.scalars(select(Batida).where(
        Batida.usuario_id == u.id, Batida.dia == dia, VALIDA).order_by(Batida.momento)))
    proximos = list(db.scalars(select(Turno).where(
        Turno.usuario_id == u.id, Turno.data > dia,
        Turno.data <= dia + timedelta(days=7)).order_by(Turno.data)))
    resp = render(request, "app_hoje.html", u=u, e=e, aba="hoje", dia=dia, agora=al,
                  aberta=aberta, turno=turno, batidas=batidas, proximos=proximos,
                  tarefas=regras.tarefas_do_dia(db, e, dia, u),
                  push_chave=push.chave_publica(db),
                  avaliar_dia=None if aberta else avaliacao.pendente(db, u),
                  perguntas_fim=None if aberta else perguntas.pendente(db, u),
                  codigo="".join(c for c in codigo if c.isdigit())[:6],
                  tem_totem=bool(db.scalar(select(Totem.id).where(Totem.empresa_id == e.id).limit(1))),
                  facial_pendente=facial.pendente(u), facial_usa=facial.usa(u),
                  pausas=None if aberta or e.pais != "US" else _us_descanso.pendente(db, u))
    if not request.cookies.get(COOKIE_APARELHO):  # identifica este aparelho daqui em diante
        resp.set_cookie(COOKIE_APARELHO, secrets.token_urlsafe(32), max_age=60 * 60 * 24 * 730,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.post("/app/ponto", dependencies=CSRF)
async def app_ponto(request: Request, tarefas_bg: BackgroundTasks,
                    u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    totem = lat = lng = prec = None
    try:
        d = await request.json()
        if not d.get("codigo") and not core.gps_liberado(u.empresa):
            return i18n.resposta_erro({"erro": "Na sua empresa o ponto é batido no totem (o tablet da empresa): use o seu PIN nele ou digite aqui o código que aparece na tela."}, 400)
        if not d.get("codigo"):
            lat, lng = float(d["lat"]), float(d["lng"])
            prec = float(d["precisao"]) if d.get("precisao") is not None else None
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
    except Exception:
        return i18n.resposta_erro({"erro": "Não recebi a sua localização. Tente de novo."}, 400)
    foto = None
    if d.get("foto"):
        try:
            foto = base64.b64decode(str(d["foto"]).split(",", 1)[-1], validate=True)
        except Exception:
            foto = b""
        if not foto.startswith(b"\xff\xd8") or len(foto) > 600_000:
            return i18n.resposta_erro({"erro": "Não consegui ler a foto. Tire outra e tente de novo."}, 400)
    if not dentro_do_limite(f"ponto:{u.id}", 12, 600):
        return i18n.resposta_erro({"erro": "Muitas tentativas. Aguarde alguns minutos."}, 429)
    if d.get("codigo"):
        totem = regras.totem_do_codigo(db, u.empresa, str(d["codigo"]))
        if not totem:
            return i18n.resposta_erro({"erro": "Código do totem inválido ou vencido. Ele muda a cada "
                                         "minuto: confira no tablet e digite de novo."}, 400)
    try:
        b, alertas = regras.bater(db, u, lat, lng, prec, totem=totem,
                                  aparelho=request.cookies.get(COOKIE_APARELHO, ""),
                                  ua=request.headers.get("user-agent", ""), foto=foto,
                                  motivo=str(d.get("motivo") or ""))
    except regras.ErroPonto as ex:
        tarefas_bg.add_task(push.avisar_alertas, [a.id for a in ex.alertas])
        return i18n.resposta_erro({"erro": str(ex), "codigo": ex.codigo}, 400)
    if facial.usa(u):
        facial.conferir_batida(db, u, b, d.get("facial"))
        db.commit()
        if b.facial == "diverge":
            novos = list(db.scalars(select(Alerta.id).where(Alerta.usuario_id == u.id, Alerta.tipo == "facial")
                                    .order_by(Alerta.id.desc()).limit(1)))
            tarefas_bg.add_task(push.avisar_alertas, novos)
    tarefas_bg.add_task(push.avisar_alertas, [a.id for a in alertas])
    hora = regras.para_local(u.empresa, b.momento).strftime("%H:%M")
    msg = f"{'Entrada' if b.tipo == 'entrada' else 'Saída'} registrada às {hora}."
    if b.tipo == "entrada" and b.atraso_min > u.empresa.tolerancia_min:
        msg += f" Atraso de {b.atraso_min} min."
    if b.tipo == "entrada" and not b.conferida:
        msg += " Você bateu antes do horário do turno: o gestor vai conferir."
    nsr = db.scalar(select(rep.RegistroRep.nsr).where(rep.RegistroRep.batida_id == b.id))
    if nsr:
        msg += f" Comprovante nº {nsr} em Meus comprovantes."
    return {"ok": True, "tipo": b.tipo, "hora": hora, "mensagem": msg, "nsr": nsr,
            "comprovante": f"/app/comprovante/{nsr}.pdf" if nsr else ""}


@app.post("/app/tarefas/{tid}", dependencies=CSRF)
def app_tarefa(request: Request, tid: int, dia: str = Form(""), volta: str = Form("/app"),
               u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    t = db.get(Tarefa, tid)
    hoje = regras.hoje(e)
    d = data_iso(dia, hoje)
    volta = volta if volta in ("/app", "/painel/tarefas", "/painel") else "/app"
    if (not t or t.empresa_id != e.id or not t.ativo or abs((d - hoje).days) > 1
            or (t.usuario_id and t.usuario_id != u.id and not u.gestor)):
        return ir(volta)
    feita = db.scalar(select(TarefaFeita).where(TarefaFeita.tarefa_id == t.id,
                                                TarefaFeita.data == d))
    if feita:
        if feita.usuario_id == u.id or u.gestor:
            db.delete(feita)
        else:
            avisar(request, f"Esta tarefa foi marcada por {feita.usuario.primeiro_nome}.", "erro")
    else:
        db.add(TarefaFeita(empresa_id=e.id, tarefa_id=t.id, data=d, usuario_id=u.id,
                           feito_em=banco.agora()))
    db.commit()
    return ir(volta)


@app.get("/app/escala")
def app_escala(request: Request, u: Usuario = Depends(usuario_logado),
               db: Session = Depends(get_db)):
    e = u.empresa
    hoje = regras.hoje(e)
    seg = hoje - timedelta(days=hoje.weekday())
    dias = [seg + timedelta(days=i) for i in range(14)]
    regras.gerar_da_fixa(db, e, dias[0], dias[-1])
    turnos = {t.data: t for t in db.scalars(select(Turno).where(
        Turno.usuario_id == u.id, Turno.data.between(dias[0], dias[-1])))}
    return render(request, "app_escala.html", u=u, e=e, aba="escala", dias=dias,
                  turnos=turnos, hoje=hoje, of=trocas.do_app(db, u),
                  disp=disponibilidade.do_usuario(db, u.id),
                  precisa_confirmar=lambda t: confirmacao.precisa_confirmar(db, e, t),
                  pendentes=[t for t in turnos.values() if confirmacao.precisa_confirmar(db, e, t)],
                  comecou=lambda t: trocas.ja_comecou(e, t.data, t.inicio))


@app.get("/app/ajustes")
def app_ajustes(request: Request, u: Usuario = Depends(usuario_logado),
                db: Session = Depends(get_db)):
    e = u.empresa
    pedidos = list(db.scalars(select(Ajuste).where(Ajuste.usuario_id == u.id)
                              .order_by(Ajuste.criado_em.desc()).limit(30)))
    hoje = regras.agora_local(e).date()
    enviados = list(db.scalars(select(Ausencia).where(Ausencia.usuario_id == u.id)
                               .order_by(Ausencia.criado_em.desc()).limit(20)))
    return render(request, "app_ajustes.html", u=u, e=e, aba="ajustes", pedidos=pedidos,
                  hoje=hoje, minimo=hoje - timedelta(days=e.ajuste_dias), enviados=enviados,
                  ferias=ferias.situacao(db, u, hoje), ferias_participa=ferias.participa(u),
                  **(dict(saldos=_us_pto.saldos(db, e.id, u.id), extrato=_us_pto.extrato(db, u.id, 10),
                          MOTIVOS_PTO=_us_pto.MOTIVOS) if e.pais == "US" else {}))


@app.post("/app/ajustes", dependencies=CSRF)
def app_ajuste_novo(request: Request, tarefas_bg: BackgroundTasks, tipo: str = Form(""),
                    data: str = Form(""), hora: str = Form(""), motivo: str = Form(""),
                    u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    al = regras.agora_local(e)
    d = data_iso(data, None)
    motivo = motivo.strip()[:300]
    erro = None
    if e.ajuste_dias <= 0:
        erro = "Sua empresa não aceita pedidos de ajuste pelo aplicativo. Fale com o gestor."
    elif tipo not in ("entrada", "saida") or not d or not HORA_RE.match(hora):
        erro = "Informe o tipo, a data e a hora da batida."
    elif len(motivo) < 5:
        erro = "Explique o motivo do ajuste."
    elif datetime.combine(d, regras.hm(hora)) > al:
        erro = "Não dá para pedir ajuste de um horário que ainda não chegou."
    elif apuracao.esta_fechado(db, e, d):
        erro = "Esse dia já está num período de ponto fechado. Fale com o gestor."
    elif d < al.date() - timedelta(days=e.ajuste_dias):
        erro = f"Só é possível pedir ajuste de até {e.ajuste_dias} dias atrás. Fale com o gestor."
    elif (db.scalar(select(func.count()).select_from(Ajuste).where(
            Ajuste.usuario_id == u.id, Ajuste.situacao == "pendente")) or 0) >= 5:
        erro = "Você já tem 5 pedidos aguardando resposta."
    if erro:
        avisar(request, erro, "erro")
        return ir("/app/ajustes")
    db.add(Ajuste(empresa_id=e.id, usuario_id=u.id, tipo=tipo, data=d, hora=hora, motivo=motivo,
                  criado_em=banco.agora()))
    a = Alerta(empresa_id=e.id, usuario_id=u.id, tipo="ajuste", criado_em=banco.agora(),
               texto=f"{u.nome} pediu ajuste de ponto: {'entrada' if tipo == 'entrada' else 'saída'} "
                     f"em {d:%d/%m} às {hora}.")
    db.add(a)
    db.commit()
    tarefas_bg.add_task(push.avisar_alertas, [a.id])
    avisar(request, "Pedido enviado ao gestor.")
    return ir("/app/ajustes")
