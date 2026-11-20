"""Revenda: Painel Master (o dono da plataforma gerencia empresas, planos, pagamentos e a
plataforma) e a tela Assinatura de cada empresa. O master é o usuário com is_admin."""
import secrets
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import asaas
from . import db as banco
from .core import (SITUACOES, Redirecionar, admin_logado, avisar, base_url, celular_limpo,
                   dinheiro, email_valido, exigir_csrf, funcionarios_ativos, gerar_hash,
                   ir, ler_centavos, limite_funcionarios, plataforma,
                   plataforma_mudou, render, situacao)
from .db import Banner, Batida, Empresa, Pagamento, Plano, Plataforma, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
FORMAS = ("pix", "boleto", "cartao", "dinheiro", "outro")


# ------------------------------------------------------------------ apoio
def planos_a_venda(db: Session) -> list[Plano]:
    return list(db.scalars(select(Plano).where(Plano.ativo.is_(True))
                           .order_by(Plano.ordem, Plano.preco_centavos)))


def _todos_os_planos(db: Session) -> list[Plano]:
    return list(db.scalars(select(Plano).order_by(Plano.ordem, Plano.preco_centavos)))


def criar_empresa(db: Session, empresa: str, nome: str, email: str, senha: str,
                  plano_id: int = 0) -> Usuario:
    """Cria a empresa em período de teste e o gestor dela. Devolve o gestor."""
    plano = db.get(Plano, plano_id) if plano_id else None
    e = Empresa(nome=empresa, plano_id=plano.id if plano else None,
                teste_ate=banco.agora() + timedelta(days=plataforma()["dias_teste"]))
    db.add(e)
    db.flush()
    u = Usuario(empresa_id=e.id, nome=nome, email=email, senha_hash=gerar_hash(senha),
                papel="gestor", na_escala=False, dono=True)
    db.add(u)
    db.flush()
    banco.definir_admin(db)  # a primeira conta da instalação é a do dono da plataforma
    db.commit()
    return u


def sem_vaga(db: Session, e: Empresa) -> str | None:
    """Mensagem de erro se a empresa já usa todos os funcionários do plano."""
    lim = limite_funcionarios(e)
    if lim and funcionarios_ativos(db, e.id) >= lim:
        return (f"Seu plano {e.plano.nome} permite até {lim} funcionários ativos. "
                "Desative alguém ou mude de plano em Configurações → Assinatura e plano.")
    return None


def _empresa(db: Session, empresa_id: int) -> Empresa:
    e = db.get(Empresa, empresa_id)
    if not e:
        raise Redirecionar("/master")
    return e


def _dono(db: Session, empresa_id: int) -> Usuario | None:
    return db.scalar(select(Usuario).where(
        Usuario.empresa_id == empresa_id, Usuario.papel == "gestor").order_by(Usuario.id).limit(1))


# ------------------------------------------------------------------ master: visão geral
@rota.get("/master")
def visao_geral(request: Request, s: str = "", q: str = "",
                u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    empresas = list(db.scalars(select(Empresa).order_by(Empresa.criado_em.desc())))
    equipe = dict(db.execute(select(Usuario.empresa_id, func.count()).where(
        Usuario.papel == "funcionario", Usuario.ativo.is_(True))
        .group_by(Usuario.empresa_id)).all())
    batidas = dict(db.execute(select(Batida.empresa_id, func.count()).where(
        Batida.momento >= banco.agora() - timedelta(days=30)).group_by(Batida.empresa_id)).all())
    donos = {}
    for g in db.scalars(select(Usuario).where(Usuario.papel == "gestor").order_by(Usuario.id)):
        donos.setdefault(g.empresa_id, g)
    contagem = {k: 0 for k in SITUACOES}
    mrr = 0
    for e in empresas:
        sit = situacao(e)
        contagem[sit] += 1
        if sit == "ativa" and e.plano:
            mrr += e.plano.preco_centavos
    recebido = db.scalar(select(func.coalesce(func.sum(Pagamento.valor_centavos), 0)).where(
        Pagamento.status == "pago",
        Pagamento.pago_em >= banco.agora() - timedelta(days=30))) or 0
    termo = q.strip().lower()
    lista = [e for e in empresas if (not s or situacao(e) == s) and (
        not termo or termo in e.nome.lower()
        or (donos.get(e.id) and termo in (donos[e.id].email or "").lower()))]
    return render(request, "master/geral.html", u=u, e=u.empresa, aba="master", sub="geral",
                  empresas=lista, total=len(empresas), contagem=contagem, mrr=mrr,
                  recebido=recebido, equipe=equipe, batidas=batidas, donos=donos, s=s, q=q,
                  novas=sum(1 for e in empresas
                            if e.criado_em >= banco.agora() - timedelta(days=30)))


# ------------------------------------------------------------------ master: empresas
@rota.get("/master/empresas/nova")
def empresa_nova_form(request: Request, u: Usuario = Depends(admin_logado),
                      db: Session = Depends(get_db)):
    return render(request, "master/empresa_nova.html", u=u, e=u.empresa, aba="master",
                  sub="geral", planos=_todos_os_planos(db), dados={}, erro=None)


def _detalhe(request: Request, u: Usuario, db: Session, empresa_id: int,
             senha_temp: str | None = None):
    alvo = _empresa(db, empresa_id)
    return render(
        request, "master/empresa.html", u=u, e=u.empresa, aba="master", sub="geral", alvo=alvo,
        dono=_dono(db, alvo.id), planos=_todos_os_planos(db), senha_temp=senha_temp,
        entrar_url=f"{base_url(request)}/entrar",
        pagamentos=list(db.scalars(select(Pagamento).where(Pagamento.empresa_id == alvo.id)
                                   .order_by(Pagamento.criado_em.desc()).limit(50))),
        numeros={"equipe": funcionarios_ativos(db, alvo.id),
                 "limite": limite_funcionarios(alvo),
                 "batidas": db.scalar(select(func.count()).select_from(Batida).where(
                     Batida.empresa_id == alvo.id,
                     Batida.momento >= banco.agora() - timedelta(days=30))) or 0})


@rota.post("/master/empresas", dependencies=CSRF)
def empresa_nova(request: Request, u: Usuario = Depends(admin_logado),
                 db: Session = Depends(get_db), empresa: str = Form(""),
                 nome: str = Form(""), email: str = Form(""), plano: int = Form(0)):
    empresa, nome, email = empresa.strip()[:120], nome.strip()[:120], email.strip().lower()
    erro = None
    if not empresa or not nome:
        erro = "Preencha o nome da empresa e o do responsável."
    elif not email_valido(email):
        erro = "E-mail inválido."
    elif db.scalar(select(Usuario.id).where(Usuario.email == email)):
        erro = "Já existe uma conta com este e-mail."
    if erro:
        return render(request, "master/empresa_nova.html", 400, u=u, e=u.empresa, aba="master",
                      sub="geral", planos=_todos_os_planos(db), erro=erro,
                      dados={"empresa": empresa, "nome": nome, "email": email, "plano": plano})
    senha = secrets.token_urlsafe(9)
    novo = criar_empresa(db, empresa, nome, email, senha, plano)
    return _detalhe(request, u, db, novo.empresa_id, senha_temp=senha)


@rota.get("/master/empresas/{empresa_id}")
def empresa_detalhe(empresa_id: int, request: Request,
                    u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return _detalhe(request, u, db, empresa_id)


@rota.post("/master/empresas/{empresa_id}/dados", dependencies=CSRF)
def empresa_dados(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                  db: Session = Depends(get_db), plano: int = Form(0),
                  isenta: str = Form(""), obs: str = Form("")):
    alvo = _empresa(db, empresa_id)
    alvo.plano_id = plano if db.get(Plano, plano) else None
    alvo.isenta = bool(isenta) or alvo.id == u.empresa_id  # a do dono é sempre cortesia
    alvo.obs_master = obs.strip()[:2000]
    db.commit()
    avisar(request, "Dados da empresa salvos.")
    return ir(f"/master/empresas/{alvo.id}")


@rota.post("/master/empresas/{empresa_id}/pagamento", dependencies=CSRF)
def empresa_pagamento(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                      db: Session = Depends(get_db), valor: str = Form(""),
                      dias: int = Form(30), forma: str = Form("pix"), obs: str = Form("")):
    alvo = _empresa(db, empresa_id)
    centavos = ler_centavos(valor)
    if centavos is None:
        avisar(request, "Valor inválido. Use o formato 49,90.", "erro")
    elif not 1 <= dias <= 3660:
        avisar(request, "Quantidade de dias inválida.", "erro")
    else:
        alvo.pago_ate = max(banco.agora(), alvo.pago_ate or banco.agora()) + timedelta(days=dias)
        db.add(Pagamento(empresa_id=alvo.id, plano_nome=alvo.plano.nome if alvo.plano else "",
                         valor_centavos=centavos, dias=dias, obs=obs.strip()[:300],
                         forma=forma if forma in FORMAS else "outro",
                         registrado_por=u.email or u.nome, criado_em=banco.agora(),
                         pago_em=banco.agora(), plano_id=alvo.plano_id))
        db.commit()
        avisar(request, f"Pagamento de {dinheiro(centavos)} registrado. {alvo.nome} está "
                        f"liberada por mais {dias} dias.")
    return ir(f"/master/empresas/{alvo.id}")


@rota.post("/master/empresas/{empresa_id}/teste", dependencies=CSRF)
def empresa_teste(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                  db: Session = Depends(get_db), dias: int = Form(7)):
    alvo = _empresa(db, empresa_id)
    if not 1 <= dias <= 365:
        avisar(request, "Quantidade de dias inválida.", "erro")
    else:
        alvo.teste_ate = max(banco.agora(), alvo.teste_ate or banco.agora()) + timedelta(days=dias)
        db.commit()
        avisar(request, f"Teste estendido em {dias} dias.")
    return ir(f"/master/empresas/{alvo.id}")


@rota.post("/master/empresas/{empresa_id}/bloqueio", dependencies=CSRF)
def empresa_bloqueio(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                     db: Session = Depends(get_db)):
    alvo = _empresa(db, empresa_id)
    if alvo.id == u.empresa_id:
        avisar(request, "Você não pode bloquear a sua própria empresa.", "erro")
    else:
        alvo.ativo = not alvo.ativo
        db.commit()
        avisar(request, "Empresa desbloqueada." if alvo.ativo else
               "Empresa bloqueada: ninguém dela entra e o ponto não é mais registrado.")
    return ir(f"/master/empresas/{alvo.id}")


@rota.post("/master/empresas/{empresa_id}/senha", dependencies=CSRF)
def empresa_senha(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                  db: Session = Depends(get_db)):
    dono = _dono(db, empresa_id)
    if not dono or dono.id == u.id:
        avisar(request, "Não dá para redefinir a senha desta conta por aqui.", "erro")
        return ir(f"/master/empresas/{empresa_id}")
    senha = secrets.token_urlsafe(9)
    dono.senha_hash = gerar_hash(senha)
    db.commit()
    return _detalhe(request, u, db, empresa_id, senha_temp=senha)


@rota.post("/master/empresas/{empresa_id}/entrar", dependencies=CSRF)
def empresa_entrar(empresa_id: int, request: Request, u: Usuario = Depends(admin_logado),
                   db: Session = Depends(get_db)):
    """Abre o painel da empresa como o gestor dela, para dar suporte."""
    alvo, dono = _empresa(db, empresa_id), _dono(db, empresa_id)
    if not dono or not dono.ativo or not alvo.ativo:
        avisar(request, "Desbloqueie a empresa antes de entrar no painel dela.", "erro")
        return ir(f"/master/empresas/{empresa_id}")
    if alvo.id != u.empresa_id:
        request.session["master_uid"] = u.id
        request.session["uid"] = dono.id
    return ir("/painel")


@rota.post("/master/voltar", dependencies=CSRF)
def voltar_ao_master(request: Request, db: Session = Depends(get_db)):
    mid = request.session.pop("master_uid", None)
    m = db.get(Usuario, mid) if mid else None
    if not m or not m.is_admin:
        request.session.clear()
        return ir("/entrar")
    request.session["uid"] = m.id
    return ir("/master")


# ------------------------------------------------------------------ master: planos
@rota.get("/master/planos")
def planos(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    em_uso = dict(db.execute(select(Empresa.plano_id, func.count()).where(
        Empresa.plano_id.is_not(None)).group_by(Empresa.plano_id)).all())
    return render(request, "master/planos.html", u=u, e=u.empresa, aba="master", sub="planos",
                  planos=_todos_os_planos(db), em_uso=em_uso)


def _ler_plano(p: Plano, nome: str, preco: str, limite: int, descricao: str,
               destaque: str, ativo: str, ordem: int, limite_locais: int = 0,
               com_pagamento: str = "", com_alertas: str = "", com_gps: str = "",
               com_foto: str = "", com_cerca: str = "", base: str = "", por_pessoa: str = "",
               minimo: str = "") -> str | None:
    centavos = ler_centavos(preco)
    b, pp, mn = (ler_centavos(x.strip() or "0") for x in (base, por_pessoa, minimo))
    if None in (b, pp, mn) or max(b or 0, pp or 0, mn or 0) > 10_000_000:
        return "Valores da cobrança por pessoa inválidos. Use o formato 6,00."
    if pp and centavos is None:
        centavos = 0
    if not nome.strip():
        return "Dê um nome ao plano."
    if centavos is None:
        return "Preço inválido. Use o formato 49,90."
    if not 0 <= limite <= 100000 or not 0 <= limite_locais <= 1000:
        return "Os limites não podem ser negativos (0 = ilimitado)."
    p.nome, p.preco_centavos, p.limite_funcionarios = nome.strip()[:60], centavos, limite
    p.descricao, p.destaque, p.ativo, p.ordem = (
        descricao.strip()[:1000], bool(destaque), bool(ativo), ordem)
    p.limite_locais, p.com_pagamento = limite_locais, bool(com_pagamento)
    p.com_alertas, p.com_gps, p.com_foto = bool(com_alertas), bool(com_gps), bool(com_foto)
    p.com_cerca = bool(com_cerca)
    p.base_centavos, p.por_pessoa_centavos, p.minimo_centavos = b, pp, mn
    if pp:   # plano por pessoa: o "preço" passa a ser o menor valor possível (1 pessoa ou o mínimo)
        p.preco_centavos = max(mn, b + pp)
    return None


@rota.post("/master/planos", dependencies=CSRF)
def plano_novo(request: Request, u: Usuario = Depends(admin_logado),
               db: Session = Depends(get_db), nome: str = Form(""), preco: str = Form(""),
               limite: int = Form(0), descricao: str = Form(""), destaque: str = Form(""),
               ativo: str = Form(""), ordem: int = Form(0), limite_locais: int = Form(0),
               com_pagamento: str = Form(""), com_alertas: str = Form(""),
               com_gps: str = Form(""), com_foto: str = Form(""), com_cerca: str = Form(""),
               base: str = Form(""), por_pessoa: str = Form(""), minimo: str = Form("")):
    p = Plano()
    erro = _ler_plano(p, nome, preco, limite, descricao, destaque, ativo, ordem,
                      limite_locais, com_pagamento, com_alertas, com_gps, com_foto, com_cerca,
                      base, por_pessoa, minimo)
    if erro:
        avisar(request, erro, "erro")
    else:
        db.add(p)
        db.commit()
        avisar(request, f"Plano {p.nome} criado.")
    return ir("/master/planos")


@rota.post("/master/planos/{plano_id}", dependencies=CSRF)
def plano_salvar(plano_id: int, request: Request, u: Usuario = Depends(admin_logado),
                 db: Session = Depends(get_db), nome: str = Form(""), preco: str = Form(""),
                 limite: int = Form(0), descricao: str = Form(""), destaque: str = Form(""),
                 ativo: str = Form(""), ordem: int = Form(0), limite_locais: int = Form(0),
                 com_pagamento: str = Form(""), com_alertas: str = Form(""),
                 com_gps: str = Form(""), com_foto: str = Form(""), com_cerca: str = Form(""),
                 base: str = Form(""), por_pessoa: str = Form(""), minimo: str = Form("")):
    p = db.get(Plano, plano_id)
    if not p:
        return ir("/master/planos")
    erro = _ler_plano(p, nome, preco, limite, descricao, destaque, ativo, ordem,
                      limite_locais, com_pagamento, com_alertas, com_gps, com_foto, com_cerca,
                      base, por_pessoa, minimo)
    if erro:
        db.rollback()
        avisar(request, erro, "erro")
    else:
        db.commit()
        avisar(request, f"Plano {p.nome} salvo.")
    return ir("/master/planos")


@rota.post("/master/planos/{plano_id}/excluir", dependencies=CSRF)
def plano_excluir(plano_id: int, request: Request, u: Usuario = Depends(admin_logado),
                  db: Session = Depends(get_db)):
    p = db.get(Plano, plano_id)
    if p and db.scalar(select(Empresa.id).where(Empresa.plano_id == p.id).limit(1)):
        avisar(request, "Há empresas neste plano. Desmarque “À venda” para tirá-lo da "
                        "vitrine sem afetar quem já usa.", "erro")
    elif p:
        db.delete(p)
        db.commit()
        avisar(request, "Plano excluído.")
    return ir("/master/planos")


# ------------------------------------------------------------------ master: plataforma
@rota.get("/master/plataforma")
def plataforma_form(request: Request, u: Usuario = Depends(admin_logado),
                    db: Session = Depends(get_db)):
    return render(request, "master/plataforma.html", u=u, e=u.empresa, aba="master",
                  sub="plataforma", p=db.get(Plataforma, 1), asaas_ok=asaas.configurado(),
                  webhook_ok=len(asaas.token_webhook()) >= 32)


@rota.post("/master/plataforma", dependencies=CSRF)
def plataforma_salvar(request: Request, u: Usuario = Depends(admin_logado),
                      db: Session = Depends(get_db), nome: str = Form(""),
                      nome_curto: str = Form(""),
                      whatsapp: str = Form(""), dias_teste: int = Form(7),
                      carencia_dias: int = Form(3), carencia_teste_dias: int = Form(0),
                      cadastro_aberto: str = Form(""), instrucoes_pagamento: str = Form(""),
                      titulo: str = Form(""), subtitulo: str = Form(""),
                      razao_social: str = Form(""), endereco: str = Form(""), email_legal: str = Form(""),
                      estado_lei: str = Form(""), pais_servidores: str = Form("")):
    p = db.get(Plataforma, 1)
    zap = celular_limpo(whatsapp)
    if not nome.strip():
        avisar(request, "O nome da plataforma não pode ficar vazio.", "erro")
    elif zap and len(zap) < 10:
        avisar(request, "WhatsApp do suporte incompleto: use DDD + número.", "erro")
    elif not 0 <= dias_teste <= 90:
        avisar(request, "Dias de teste: use um número entre 0 e 90.", "erro")
    elif not (0 <= carencia_dias <= 30 and 0 <= carencia_teste_dias <= 30):
        avisar(request, "Carência: use um número de dias entre 0 e 30.", "erro")
    elif email_legal.strip() and not email_valido(email_legal.strip()):
        avisar(request, "E-mail de contato legal inválido.", "erro")
    else:
        p.nome, p.whatsapp, p.dias_teste = nome.strip()[:60], zap[:20], dias_teste
        p.nome_curto = nome_curto.strip()[:20]
        p.carencia_dias, p.carencia_teste_dias = carencia_dias, carencia_teste_dias
        p.cadastro_aberto = bool(cadastro_aberto)
        p.instrucoes_pagamento = instrucoes_pagamento.strip()[:2000]
        p.titulo, p.subtitulo = titulo.strip()[:200], subtitulo.strip()[:600]
        p.razao_social, p.endereco = razao_social.strip()[:160], endereco.strip()[:300]
        p.email_legal = email_legal.strip().lower()[:200]
        p.estado_lei = estado_lei.strip()[:40]
        p.pais_servidores = pais_servidores.strip()[:60] or "Brasil"
        db.commit()
        plataforma_mudou()
        avisar(request, "Plataforma atualizada.")
    return ir("/master/plataforma")


# ------------------------------------------------------------------ banners da tela Hoje
CORES_BANNER = {"azul": "Azul", "verde": "Verde", "roxo": "Roxo", "laranja": "Laranja"}


def banners_no_ar(db: Session, hoje: date) -> list[Banner]:
    """Banners ativos e dentro do período, na ordem escolhida pelo Master."""
    return [b for b in db.scalars(select(Banner).where(Banner.ativo.is_(True))
                                  .order_by(Banner.ordem, Banner.id.desc()))
            if (not b.inicio or b.inicio <= hoje) and (not b.fim or hoje <= b.fim)]


PASTA_BANNERS = banco.PASTA_DADOS / "banners"
TIPOS_IMAGEM = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}
NOME_IMAGEM = re.compile(r"^[0-9]+-[0-9a-f]{12}\.(jpg|png|webp)$")


def _salvar_imagem(arquivo: UploadFile, bid: int) -> str | None:
    """Guarda a imagem enviada (JPG, PNG ou WebP até 3 MB) e devolve o endereço dela."""
    dados = arquivo.file.read(3 * 1024 * 1024 + 1)
    if not dados or len(dados) > 3 * 1024 * 1024:
        return None
    ext = next((x for sig, x in TIPOS_IMAGEM.items() if dados.startswith(sig)), None)
    if ext == "webp" and dados[8:12] != b"WEBP":
        ext = None
    if not ext:
        return None
    PASTA_BANNERS.mkdir(parents=True, exist_ok=True)
    nome = f"{bid}-{secrets.token_hex(6)}.{ext}"
    (PASTA_BANNERS / nome).write_bytes(dados)
    return f"/banners/{nome}"


def _apagar_imagem(imagem: str):
    if imagem.startswith("/banners/") and NOME_IMAGEM.match(imagem[9:]):
        (PASTA_BANNERS / imagem[9:]).unlink(missing_ok=True)


@rota.get("/banners/{nome}")
def banner_imagem(nome: str):
    """Imagem de fundo de banner (pública: é material de divulgação)."""
    if not NOME_IMAGEM.match(nome) or not (PASTA_BANNERS / nome).is_file():
        return Response(status_code=404)
    return FileResponse(PASTA_BANNERS / nome, headers={"Cache-Control": "public,max-age=86400"})


def _link_ok(link: str) -> bool:
    link = link.strip()
    return not link or link.startswith("https://") or (link.startswith("/") and not link.startswith("//"))


def _data(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


@rota.get("/master/banners")
def banners(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/banners.html", u=u, e=u.empresa, aba="master", sub="banners",
                  banners=list(db.scalars(select(Banner).order_by(Banner.ordem, Banner.id.desc()))),
                  CORES=CORES_BANNER, hoje=date.today())


@rota.post("/master/banners", dependencies=CSRF)
def banner_salvar(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db),
                  bid: int = Form(0), titulo: str = Form(""), texto: str = Form(""),
                  link: str = Form(""), botao: str = Form(""), cor: str = Form("azul"),
                  imagem: str = Form(""), arquivo: UploadFile | None = File(None),
                  sem_imagem: str = Form(""),
                  inicio: str = Form(""), fim: str = Form(""), ordem: int = Form(0),
                  ativo: str = Form("")):
    titulo = titulo.strip()
    ini, fi = _data(inicio), _data(fim)
    if not titulo:
        avisar(request, "Dê um título ao banner.", "erro")
    elif not _link_ok(link):
        avisar(request, "O link precisa começar com https:// ou ser uma página do sistema (/painel/...).", "erro")
    elif imagem.strip() and not (imagem.strip().startswith("/banners/") and NOME_IMAGEM.match(imagem.strip()[9:])) and (
            not imagem.strip().startswith("https://") or any(c in imagem for c in "'\"()<> ")):
        avisar(request, "A imagem precisa ser um endereço https:// de uma foto (sem espaços nem aspas).", "erro")
    elif ini and fi and fi < ini:
        avisar(request, "O fim do banner é antes do início.", "erro")
    else:
        b = db.get(Banner, bid) if bid else None
        if b is None:
            b = Banner()
            db.add(b)
        b.titulo, b.texto = titulo[:90], texto.strip()[:240]
        b.link, b.botao = link.strip()[:300], botao.strip()[:40]
        db.flush()
        nova = imagem.strip()[:400]
        if arquivo is not None and arquivo.filename:
            enviada = _salvar_imagem(arquivo, b.id)
            if not enviada:
                db.rollback()
                avisar(request, "A imagem precisa ser JPG, PNG ou WebP de até 3 MB.", "erro")
                return ir("/master/banners")
            nova = enviada
        if sem_imagem:
            nova = ""
        if b.imagem and b.imagem != nova:
            _apagar_imagem(b.imagem)
        b.imagem = nova
        b.cor = cor if cor in CORES_BANNER else "azul"
        b.inicio, b.fim, b.ordem, b.ativo = ini, fi, ordem, bool(ativo)
        db.commit()
        avisar(request, "Banner salvo." + ("" if b.ativo else " Ele está desligado: ligue para aparecer."))
    return ir("/master/banners")


@rota.post("/master/banners/{bid}/remover", dependencies=CSRF)
def banner_remover(request: Request, bid: int, u: Usuario = Depends(admin_logado),
                   db: Session = Depends(get_db)):
    b = db.get(Banner, bid)
    if b:
        _apagar_imagem(b.imagem or "")
        db.delete(b)
        db.commit()
        avisar(request, "Banner removido.")
    return ir("/master/banners")
