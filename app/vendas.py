"""Página de vendas longa, editável no Painel Master.

O master edita um rascunho, vê a prévia e publica. Enquanto nada foi publicado,
a raiz do site mostra a página simples (inicio.html). Imagens e vídeos enviados
ficam em MIDIA_DIR (no volume de dados) e são servidos em /midia.
"""
import copy
import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from . import db as banco
from . import i18n
from .core import admin_logado, avisar, exigir_csrf, plataforma, render
from .db import PaginaVendas, Plano, Usuario, get_db

router = APIRouter(include_in_schema=False)

MIDIA_DIR = Path(os.environ.get("MIDIA_DIR") or banco.PASTA_DADOS / "midia")
MIDIA_DIR.mkdir(parents=True, exist_ok=True)

LIMITE_IMAGEM = 5 * 1024 * 1024
LIMITE_VIDEO = 100 * 1024 * 1024
RE_MIDIA = re.compile(r"^/midia/[a-f0-9]{24}\.(png|jpg|webp|gif|mp4|webm)$")
RE_YOUTUBE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
RE_VIMEO = re.compile(r"vimeo\.com/(?:video/)?(\d{6,12})")

# ---------------------------------------------------------------- conteúdo
# Cada seção: campos de texto (nome -> tamanho máximo), mídias, listas simples
# (um item por linha) e uma lista de itens com campos próprios.
ESQUEMA = {
    "topo": {"nome": "Topo", "campos": {"selo": 80, "titulo": 160, "subtitulo": 400,
                                        "botao": 40, "nota": 120},
             "midia": ("imagem", "video")},
    "dor": {"nome": "A dor do cliente", "campos": {"titulo": 160, "texto": 400, "fecho": 240},
            "lista": {"titulo": 80, "texto": 240}, "max": 8},
    "solucao": {"nome": "A solução", "campos": {"titulo": 160, "texto": 800},
                "linhas": ("pontos",), "midia": ("imagem", "video")},
    "passos": {"nome": "Como funciona", "campos": {"titulo": 160},
               "lista": {"titulo": 80, "texto": 240}, "max": 6},
    "beneficios": {"nome": "Benefícios", "campos": {"titulo": 160, "texto": 400},
                   "lista": {"titulo": 80, "texto": 240}, "max": 12},
    "demo": {"nome": "Vídeo de demonstração", "campos": {"titulo": 160, "texto": 400},
             "midia": ("video",)},
    "depoimentos": {"nome": "Depoimentos", "campos": {"titulo": 160, "texto": 400},
                    "lista": {"nome": 80, "empresa": 80, "texto": 600, "nota": 1},
                    "foto": True, "max": 12},
    "planos": {"nome": "Planos e preço", "campos": {"titulo": 160, "texto": 400}},
    "garantia": {"nome": "Garantia", "campos": {"titulo": 160, "texto": 600}},
    "faq": {"nome": "Perguntas frequentes", "campos": {"titulo": 160},
            "lista": {"pergunta": 200, "resposta": 800}, "max": 15},
    "final": {"nome": "Chamada final", "campos": {"titulo": 160, "texto": 400, "botao": 40}},
    "rodape": {"nome": "Rodapé", "campos": {"texto": 240}},
}

ROTULOS = {
    "selo": "Frase curta acima do título", "titulo": "Título", "subtitulo": "Subtítulo",
    "texto": "Texto", "botao": "Texto do botão", "nota": "Frase pequena abaixo do botão",
    "fecho": "Frase de fechamento", "pontos": "Pontos de destaque (um por linha)",
    "nome": "Nome", "empresa": "Empresa ou cargo", "pergunta": "Pergunta",
    "resposta": "Resposta", "imagem": "Imagem", "video": "Vídeo",
}
CAMPOS_LONGOS = {"subtitulo", "texto", "resposta"}


def conteudo_padrao() -> dict:
    """Ponto de partida do rascunho. Depoimentos começam vazios e escondidos:
    só entram os que o master cadastrar."""
    dias = plataforma()["dias_teste"]
    teste = f"Testar {dias} dias grátis" if dias else "Começar agora"
    return {
        "topo": {"ativo": True, "selo": "Para restaurantes, bares, lojas, clínicas e prestadores de serviço",
                 "titulo": "Escala, ponto e fechamento do mês da sua equipe, no celular de cada um",
                 "subtitulo": "Monte a escala em minutos. O funcionário bate o ponto pelo celular e o sistema "
                              "confere se ele está mesmo na empresa. Você é avisado na hora de cada atraso e "
                              "fecha o mês com horas extras, adicional noturno e folha de ponto prontos.",
                 "botao": teste, "nota": "Sem cartão de crédito. Sem relógio de ponto para comprar." if dias else "",
                 "imagem": "", "video": ""},
        "dor": {"ativo": True, "titulo": "Controlar a equipe na planilha e no grupo de WhatsApp custa caro",
                "texto": "Quem tem funcionário em turno conhece estes problemas.",
                "itens": [
                    {"titulo": "Escala que ninguém acha", "texto": "A escala fica numa planilha ou numa foto no grupo, e sempre alguém diz que não viu."},
                    {"titulo": "Você não sabe quem chegou", "texto": "Só descobre o atraso ou a falta quando o cliente já está esperando."},
                    {"titulo": "Relógio de ponto caro", "texto": "Aparelho, instalação, bobina e manutenção, para cada endereço da empresa."},
                    {"titulo": "Fechamento do mês na mão", "texto": "Somar horas, extras e faltas de cada um leva dias e sempre sobra dúvida."},
                    {"titulo": "Hora extra e adicional noturno no chute", "texto": "Sem a conta certa, ou você paga a mais ou fica devendo ao funcionário."},
                    {"titulo": "Ponto esquecido vira discussão", "texto": "O acerto é combinado de boca e ninguém lembra depois o que foi ajustado."},
                ],
                "fecho": "Cada um desses problemas custa dinheiro todo mês, mesmo quando ninguém percebe."},
        "solucao": {"ativo": True, "titulo": "Tudo num lugar só: o gestor no painel, a equipe no celular",
                    "texto": "Você monta a escala da semana e cada funcionário vê a dele no próprio celular. Na hora de "
                             "trabalhar, ele toca em Bater ponto: a hora é registrada e a localização é conferida com o "
                             "endereço da empresa. No fim do mês, as horas de cada um já estão somadas.",
                    "pontos": ["Funciona no navegador do computador e do celular, sem comprar aparelho",
                               "Ponto com conferência de localização, foto e um aparelho por funcionário",
                               "Avisos na hora: atraso, ponto não batido, saída esquecida, intervalo não feito",
                               "Fechamento com banco de horas, horas extras, adicional noturno e folha para assinar"],
                    "imagem": "", "video": ""},
        "passos": {"ativo": True, "titulo": "Como funciona",
                   "itens": [
                       {"titulo": "Cadastre a equipe", "texto": "Cada funcionário recebe um link no WhatsApp e cria a própria senha."},
                       {"titulo": "Monte a escala", "texto": "Horários, folgas e intervalos da semana numa grade só. Copie a semana anterior com um toque."},
                       {"titulo": "A equipe bate o ponto", "texto": "Pelo celular, com a localização conferida, ou num tablet na empresa para quem não tem celular."},
                       {"titulo": "Feche o mês", "texto": "Confira as horas, feche o período e cada funcionário assina a folha pelo aplicativo."},
                   ]},
        "beneficios": {"ativo": True, "titulo": "O que você ganha", "texto": "",
                       "itens": [
                           {"titulo": "Avisos na hora", "texto": "Atraso, ponto não batido e tentativa fora da empresa chegam no seu painel e no seu celular."},
                           {"titulo": "Ponto mais difícil de burlar", "texto": "Localização conferida, foto na batida e um aparelho autorizado por funcionário."},
                           {"titulo": "Fechamento pronto", "texto": "Horas previstas e trabalhadas, extras, atrasos, faltas e banco de horas de cada um."},
                           {"titulo": "Adicional noturno calculado", "texto": "As horas entre 22h e 5h são separadas, com a hora noturna reduzida."},
                           {"titulo": "Folha de ponto assinada", "texto": "O funcionário confere e assina pelo aplicativo. Você imprime ou salva em PDF."},
                           {"titulo": "Planilha para o contador", "texto": "Os totais do período fechado saem prontos para enviar."},
                           {"titulo": "Atestados e férias", "texto": "O funcionário envia a foto do atestado pelo aplicativo e os dias deixam de contar como falta."},
                           {"titulo": "Filiais e totem", "texto": "Vários endereços na mesma conta e um tablet na entrada para quem não tem celular."},
                           {"titulo": "Checklist do dia", "texto": "Tarefas diárias da equipe, com quem fez e a que horas."},
                           {"titulo": "Histórico de tudo", "texto": "Nenhuma batida é apagada: toda correção fica registrada com quem fez e quando."},
                       ]},
        "demo": {"ativo": False, "titulo": "Veja funcionando", "texto": "", "video": ""},
        "depoimentos": {"ativo": False, "titulo": "Quem usa recomenda", "texto": "", "itens": []},
        "planos": {"ativo": True, "titulo": "Planos",
                   "texto": (f"Teste por {dias} dias sem cartão. " if dias else "") + "Mude de plano quando quiser."},
        "garantia": {"ativo": bool(dias), "titulo": f"Teste por {dias} dias sem pagar nada",
                     "texto": "Você usa o sistema completo durante o teste, com a sua equipe de verdade, sem informar "
                              "cartão. Só paga se quiser continuar."},
        "faq": {"ativo": True, "titulo": "Perguntas frequentes",
                "itens": [
                    {"pergunta": "Preciso instalar alguma coisa ou comprar aparelho?", "resposta": "Não. Funciona no navegador do computador e do celular, e pode ser adicionado à tela inicial como um aplicativo."},
                    {"pergunta": "E o funcionário que não tem celular?", "resposta": "Você deixa um tablet na empresa, no modo totem. Ele toca no próprio nome e digita um PIN."},
                    {"pergunta": "Dá para bater o ponto de casa?", "resposta": "O sistema confere a localização do celular com o endereço da empresa e recusa o ponto fora do raio que você definir. Com a foto e o aparelho autorizado fica bem mais difícil burlar, mas nenhum controle por celular é à prova de tudo."},
                    {"pergunta": "Isso substitui o relógio de ponto exigido por lei?", "resposta": "É um controle de jornada para a gestão da empresa. Não é um registrador de ponto homologado (REP-P). Converse com o seu contador sobre o que a sua empresa é obrigada a ter."},
                    {"pergunta": "Calcula hora extra e adicional noturno?", "resposta": "Sim. O fechamento separa as extras em dia normal e em folga ou feriado, com os percentuais que você configurar, e as horas noturnas. Os valores em dinheiro ficam com o contador."},
                    {"pergunta": "Preciso de cartão de crédito para testar?", "resposta": "Não. O teste é liberado na hora do cadastro."},
                    {"pergunta": "Como pago depois do teste?", "resposta": "Por Pix, boleto ou cartão, direto na tela Assinatura."},
                    {"pergunta": "Posso cancelar quando quiser?", "resposta": "Sim. Você encerra a cobrança automática na própria tela Assinatura e usa até o fim do período pago."},
                ]},
        "final": {"ativo": True, "titulo": "Comece hoje a organizar a sua equipe",
                  "texto": "Crie a conta, cadastre os funcionários e monte a primeira escala em minutos.",
                  "botao": teste},
        "rodape": {"ativo": True, "texto": ""},
    }


# Uma página por idioma: o português é a linha 1 (a que já existia).
PAGINA_ID = {"pt": 1, "en": 2, "es": 3}


def padrao_no_idioma(lang: str) -> dict:
    """Conteúdo inicial já traduzido para o idioma da página."""
    with i18n.no_idioma(lang):
        return i18n.traduzir_tudo(conteudo_padrao())


def completar(c: dict | None, lang: str = "pt") -> dict:
    """Garante todas as seções e campos, para conteúdo salvo em versões anteriores."""
    base = padrao_no_idioma(lang)
    if not isinstance(c, dict):
        return base
    for sec, padrao in base.items():
        atual = c.get(sec)
        if not isinstance(atual, dict):
            c[sec] = padrao
            continue
        for k, v in padrao.items():
            atual.setdefault(k, copy.deepcopy(v) if k != "itens" else [])
    return {sec: c[sec] for sec in base}


# ------------------------------------------------------------------- mídia
def tipo_do_arquivo(inicio: bytes) -> str | None:
    """Reconhece o arquivo pelos primeiros bytes, não pelo nome."""
    if inicio.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if inicio.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if inicio[:4] == b"RIFF" and inicio[8:12] == b"WEBP":
        return "webp"
    if inicio[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if inicio[4:8] == b"ftyp":
        return "mp4"
    if inicio.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return None


IMAGENS, VIDEOS = ("png", "jpg", "webp", "gif"), ("mp4", "webm")


async def salvar_midia(up: UploadFile, tipos: tuple, limite: int) -> str:
    inicio = await up.read(16)
    ext = tipo_do_arquivo(inicio)
    if ext not in tipos:
        raise ValueError(f"“{up.filename}” não é um arquivo aceito "
                         f"({', '.join(tipos).upper()}).")
    nome = f"{secrets.token_hex(12)}.{ext}"
    destino = MIDIA_DIR / nome
    total = len(inicio)
    try:
        with open(destino, "wb") as f:
            f.write(inicio)
            while True:
                bloco = await up.read(1024 * 1024)
                if not bloco:
                    break
                total += len(bloco)
                if total > limite:
                    raise ValueError(f"“{up.filename}” passa do limite de "
                                     f"{limite // (1024 * 1024)} MB.")
                f.write(bloco)
    except ValueError:
        destino.unlink(missing_ok=True)
        raise
    return f"/midia/{nome}"


def video_info(v: str) -> dict | None:
    """Como exibir um vídeo: arquivo enviado, YouTube ou Vimeo. None se não for válido."""
    v = (v or "").strip()
    if RE_MIDIA.match(v) and v.endswith((".mp4", ".webm")):
        return {"tipo": "arquivo", "src": v}
    m = RE_YOUTUBE.search(v)
    if m and v.startswith("https://"):
        return {"tipo": "embutido", "src": f"https://www.youtube-nocookie.com/embed/{m.group(1)}"}
    m = RE_VIMEO.search(v)
    if m and v.startswith("https://"):
        return {"tipo": "embutido", "src": f"https://player.vimeo.com/video/{m.group(1)}"}
    return None


async def _ler_midia(form, base: str, tipos: tuple, limite: int, erros: list) -> str:
    if form.get(f"{base}_remover"):
        return ""
    up = form.get(base)
    if isinstance(up, UploadFile) and up.filename:
        try:
            return await salvar_midia(up, tipos, limite)
        except ValueError as e:
            erros.append(str(e))
    if tipos is VIDEOS:
        link = str(form.get(f"{base}_link") or "").strip()
        if link:
            if video_info(link) and not link.startswith("/midia/"):
                return link[:300]
            erros.append("Link de vídeo não reconhecido: use um endereço https do YouTube ou do Vimeo.")
    atual = str(form.get(f"{base}_atual") or "")
    return atual if RE_MIDIA.match(atual) else ""


async def ler_formulario(form) -> tuple[dict, list[str]]:
    erros: list[str] = []
    novo = {}
    for sec, esq in ESQUEMA.items():
        d = {"ativo": bool(form.get(f"{sec}__ativo"))}
        for campo, limite in esq.get("campos", {}).items():
            d[campo] = str(form.get(f"{sec}__{campo}") or "").strip()[:limite]
        for campo in esq.get("linhas", ()):
            d[campo] = [x.strip()[:200] for x in
                        str(form.get(f"{sec}__{campo}") or "").splitlines() if x.strip()][:10]
        if "imagem" in esq.get("midia", ()):
            d["imagem"] = await _ler_midia(form, f"{sec}__imagem", IMAGENS, LIMITE_IMAGEM, erros)
        if "video" in esq.get("midia", ()):
            d["video"] = await _ler_midia(form, f"{sec}__video", VIDEOS, LIMITE_VIDEO, erros)
        if "lista" in esq:
            itens = []
            for chave in form.getlist(f"{sec}__chave")[:esq.get("max", 12)]:
                chave = re.sub(r"[^a-z0-9]", "", str(chave))[:16]
                item = {c: str(form.get(f"{sec}__{c}__{chave}") or "").strip()[:lim]
                        for c, lim in esq["lista"].items()}
                if "nota" in item:
                    item["nota"] = item["nota"] if item["nota"] in "12345" and item["nota"] else "5"
                if esq.get("foto"):
                    item["foto"] = await _ler_midia(form, f"{sec}__foto__{chave}",
                                                    IMAGENS, LIMITE_IMAGEM, erros)
                if any(v for k, v in item.items() if k not in ("nota", "foto")):
                    itens.append(item)
            d["itens"] = itens
        novo[sec] = d
    return novo, erros


def _arquivos_usados(*conteudos: dict | None) -> set[str]:
    usados = set()
    for c in conteudos:
        if c:
            usados.update(re.findall(r"/midia/([a-f0-9]{24}\.[a-z0-9]+)", json.dumps(c)))
    return usados


def limpar_midia(db: Session):
    """Apaga arquivos que não estão em nenhuma página (rascunho ou publicada, de nenhum
    idioma). Poupa os enviados na última hora (podem ser de um envio em andamento)."""
    paginas = list(db.scalars(select(PaginaVendas)))
    usados = _arquivos_usados(*[_carregar(p.rascunho) for p in paginas],
                              *[_carregar(p.publicado) for p in paginas])
    limite = time.time() - 3600
    for f in MIDIA_DIR.iterdir():
        if f.is_file() and f.name not in usados and f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)


# ------------------------------------------------------------ persistência
def _carregar(txt: str | None) -> dict | None:
    if not txt:
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def _lang(valor) -> str:
    return valor if valor in PAGINA_ID else "pt"


def _pagina(db: Session, lang: str = "pt") -> PaginaVendas:
    pid = PAGINA_ID[_lang(lang)]
    pag = db.get(PaginaVendas, pid)
    if not pag:
        pag = PaginaVendas(id=pid)
        db.add(pag)
        db.commit()
    return pag


def conteudo_publicado(db: Session) -> dict | None:
    """Página publicada no idioma do visitante; sem ela, a página em português."""
    for lang in dict.fromkeys((i18n.idioma(), "pt")):
        pag = db.get(PaginaVendas, PAGINA_ID[_lang(lang)])
        c = _carregar(pag.publicado) if pag else None
        if c:
            return completar(c, lang)
    return None


def contexto_da_pagina(db: Session, c: dict) -> dict:
    plat = plataforma()
    planos = list(db.scalars(select(Plano).where(Plano.ativo.is_(True))
                             .order_by(Plano.ordem, Plano.preco_centavos)))
    if plat["cadastro_aberto"]:
        cta = "/cadastro"
    elif plat["whatsapp"]:
        cta = f"https://wa.me/55{plat['whatsapp']}"
    else:
        cta = "/entrar"
    return {"c": c, "planos": planos, "cta": cta, "video_info": video_info}


# ------------------------------------------------------------------- rotas
@router.get("/master/pagina")
def editor(request: Request, lang: str = "pt", u: Usuario = Depends(admin_logado),
           db: Session = Depends(get_db)):
    lang = _lang(lang)
    pag = _pagina(db, lang)
    rascunho, publicado = _carregar(pag.rascunho), _carregar(pag.publicado)
    c = completar(copy.deepcopy(rascunho or publicado), lang)
    estados = {}
    for cod, pid in PAGINA_ID.items():
        p = db.get(PaginaVendas, pid)
        estados[cod] = "publicada" if p and p.publicado else "rascunho" if p and p.rascunho else "vazia"
    return render(request, "master/pagina.html", u=u, e=u.empresa, aba="master", sub="pagina",
                  c=c, esquema=ESQUEMA, rotulos=ROTULOS, longos=CAMPOS_LONGOS,
                  publicada=bool(publicado), publicado_em=pag.publicado_em,
                  pendente=bool(rascunho) and rascunho != publicado,
                  video_info=video_info, nova_chave=lambda: secrets.token_hex(4),
                  lang=lang, estados=estados)


@router.post("/master/pagina", dependencies=[Depends(exigir_csrf)])
async def editor_salvar(request: Request, u: Usuario = Depends(admin_logado),
                        db: Session = Depends(get_db)):
    form = await request.form()
    acao = str(form.get("acao") or "salvar")
    lang = _lang(str(form.get("lang") or "pt"))
    pag = _pagina(db, lang)
    volta = f"/master/pagina?lang={lang}"
    if acao == "tirar":
        pag.publicado, pag.publicado_em = None, None
        db.commit()
        avisar(request, "Página de vendas fora do ar. O site voltou à página simples.")
    elif acao == "descartar":
        pag.rascunho = pag.publicado
        db.commit()
        avisar(request, "Alterações não publicadas descartadas.")
    else:
        novo, erros = await ler_formulario(form)
        pag.rascunho = json.dumps(novo, ensure_ascii=False)
        pag.atualizado_em = banco.agora()
        if erros:
            db.commit()
            avisar(request, "Rascunho salvo, mas com problemas: " + " ".join(erros), "erro")
        elif acao == "previa":
            db.commit()
            limpar_midia(db)
            return RedirectResponse(f"/master/pagina/previa?lang={lang}", status_code=303)
        elif acao == "publicar":
            pag.publicado, pag.publicado_em = pag.rascunho, banco.agora()
            db.commit()
            avisar(request, "Página de vendas publicada. Já está no ar.")
        else:
            db.commit()
            avisar(request, "Rascunho salvo. Só vai ao ar quando você publicar.")
    limpar_midia(db)
    return RedirectResponse(volta, status_code=303)


@router.get("/master/pagina/previa")
def previa(request: Request, lang: str = "pt", u: Usuario = Depends(admin_logado),
           db: Session = Depends(get_db)):
    lang = _lang(lang)
    pag = _pagina(db, lang)
    c = completar(_carregar(pag.rascunho) or _carregar(pag.publicado), lang)
    with i18n.no_idioma(lang):  # a prévia mostra os menus no idioma da página
        return render(request, "vendas.html", previa=True, previa_lang=lang,
                      **contexto_da_pagina(db, c))
