"""Crescimento: faixas automáticas, plano anual, cupons, indique e ganhe e o pacote de planos.

- Faixas: planos com o mesmo `grupo` são faixas de um produto. A empresa paga a faixa que fica
  mais barata para o número de pessoas dela (assim nunca paga mais do que pagaria na seguinte),
  e o ajuste diário a muda de faixa sozinha.
- Anual: paga `Plataforma.meses_anual` mensalidades (10 = 2 meses grátis) e usa 12 meses.
- Cupom: % de desconto por alguns meses ou para sempre (ex.: FUNDADORES).
- Indique e ganhe: quem indica e quem foi indicado ganham 1 mês quando o indicado paga a 1ª vez.
"""
import re
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import asaas, regras
from . import db as banco
from .core import (admin_logado, avisar, exigir_csrf, funcionarios_ativos, gestor_logado, ir,
                   log, plataforma, plataforma_mudou, render, situacao)
from .db import Cupom, Empresa, Pagamento, Plano, Plataforma, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
DIAS_MES = 30.44


# ------------------------------------------------------------------ faixas e valor
def faixas(db: Session, p: Plano) -> list[Plano]:
    """As faixas do produto do plano (o próprio plano se ele não é de um grupo)."""
    if not p.grupo:
        return [p]
    outras = db.scalars(select(Plano).where(Plano.grupo == p.grupo, Plano.ativo.is_(True)))
    return [p] + [f for f in outras if f.id != p.id]


def _cabe(f: Plano, n: int) -> bool:
    return not f.limite_efetivo or n <= f.limite_efetivo


def melhor_faixa(db: Session, p: Plano, n: int) -> Plano:
    """Faixa mais barata em que n pessoas cabem (empate: a de ordem menor)."""
    opcoes = [f for f in faixas(db, p) if _cabe(f, n)]
    if not opcoes:   # ninguém cabe: fica na de maior limite
        return max(faixas(db, p), key=lambda f: f.limite_efetivo)
    return min(opcoes, key=lambda f: (f.valor_mensal(n), f.ordem, f.id))


def pessoas(db: Session, e: Empresa, p: Plano) -> int:
    from .cobranca import pessoas_cobradas
    if any(f.por_pessoa for f in faixas(db, p)):
        return pessoas_cobradas(db, e)
    return funcionarios_ativos(db, e.id) if p.grupo else 0


def cupom_valido(db: Session, e: Empresa) -> Cupom | None:
    if not e.cupom_id:
        return None
    c = db.get(Cupom, e.cupom_id)
    if not c:
        return None
    if c.meses and e.cupom_desde and banco.agora() > e.cupom_desde + timedelta(days=c.meses * DIAS_MES):
        return None
    return c


def desconto_pct(db: Session, e: Empresa) -> int:
    c = cupom_valido(db, e)
    return max(0, min(100, c.pct)) if c else 0


def calcular(db: Session, e: Empresa, p: Plano | None = None) -> tuple[int, int, Plano | None]:
    """(valor do mês em centavos, já com cupom; pessoas contadas; faixa que vale)."""
    p = p or e.plano
    if not p:
        return 0, 0, None
    n = pessoas(db, e, p)
    faixa = melhor_faixa(db, p, n)
    base = faixa.valor_mensal(n)
    pct = desconto_pct(db, e)
    return round(base * (100 - pct) / 100), n, faixa


def meses_anual() -> int:
    return max(1, min(12, int(plataforma().get("meses_anual") or 10)))


def valor_do_ciclo(mensal: int, ciclo: str) -> int:
    return mensal * meses_anual() if ciclo == "anual" else mensal


# ------------------------------------------------------------------ cupons
def normalizar(codigo: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "", (codigo or "").strip().upper())[:30]


def aplicar_cupom(db: Session, e: Empresa, codigo: str) -> str | None:
    """Liga o cupom à empresa. Devolve a mensagem de erro, ou None."""
    codigo = normalizar(codigo)
    c = db.scalar(select(Cupom).where(Cupom.codigo == codigo)) if codigo else None
    hoje = banco.agora().date()
    if not c or not c.ativo:
        return "Cupom não encontrado."
    if c.valido_ate and c.valido_ate < hoje:
        return "Este cupom já expirou."
    if c.max_usos and c.usos >= c.max_usos:
        return "Este cupom já foi usado o número máximo de vezes."
    if e.cupom_id:
        return "Sua empresa já tem um cupom."
    if e.isenta:
        return "Sua conta é cortesia: não há o que descontar."
    agora = banco.agora()
    e.cupom_id, c.usos = c.id, c.usos + 1
    # O prazo do cupom começa a contar quando o teste termina (é quando a empresa paga).
    e.cupom_desde = max(agora, e.teste_ate) if e.teste_ate and situacao(e) == "teste" else agora
    regras.registrar(db, e.id, "cupom", "cupom", f"Cupom {c.codigo} aplicado: {c.pct}% de desconto"
                     + (f" por {c.meses} meses." if c.meses else " enquanto a assinatura durar."))
    return None


def texto_cupom(c: Cupom) -> str:
    return f"{c.pct}% de desconto" + (f" por {c.meses} {'mês' if c.meses == 1 else 'meses'}" if c.meses else " para sempre")


# ------------------------------------------------------------------ indique e ganhe
def codigo_da(db: Session, e: Empresa) -> str:
    """Código do link de indicação da empresa (criado na primeira vez)."""
    if not e.codigo_indicacao:
        while True:
            c = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:7].lower()
            if len(c) == 7 and not db.scalar(select(Empresa.id).where(Empresa.codigo_indicacao == c)):
                break
        e.codigo_indicacao = c
        db.commit()
    return e.codigo_indicacao


def dar_mes_gratis(db: Session, e: Empresa, motivo: str):
    """Soma 30 dias à empresa e, se ela tem cobrança automática, adia o próximo vencimento."""
    agora = banco.agora()
    base = max(agora, e.pago_ate or agora, e.teste_ate or agora)
    e.pago_ate = base + timedelta(days=30)
    if e.asaas_assinatura_id and asaas.configurado():
        try:
            asaas.adiar_assinatura(e.asaas_assinatura_id, 1)
        except asaas.AsaasErro as ex:
            log.warning("Não deu para adiar a assinatura da empresa %s: %s", e.id, ex)
    db.add(Pagamento(empresa_id=e.id, tipo="credito", status="pago", valor_centavos=0, dias=30,
                     forma="outro", obs=motivo[:300], registrado_por="indicação",
                     plano_id=e.plano_id, plano_nome=e.plano.nome if e.plano else "",
                     criado_em=agora, pago_em=agora))
    regras.registrar(db, e.id, "indicação", "mês grátis", motivo)


def premiar_indicacao(db: Session, e: Empresa):
    """Primeiro pagamento de uma empresa indicada: 1 mês para ela e para quem indicou."""
    if not e.indicada_por_id or e.indicacao_premiada or e.indicada_por_id == e.id:
        return
    quem = db.get(Empresa, e.indicada_por_id)
    e.indicacao_premiada = True
    dar_mes_gratis(db, e, "Indique e ganhe: 1 mês grátis por ter vindo por indicação.")
    if quem and quem.ativo:
        dar_mes_gratis(db, quem, f"Indique e ganhe: 1 mês grátis pela indicação de {e.nome}.")
    db.commit()


def apos_pagamento(db: Session, e: Empresa, reg: Pagamento):
    """Chamado quando um pagamento é confirmado (Asaas)."""
    if (reg.valor_centavos or 0) <= 0:
        return
    try:
        premiar_indicacao(db, e)
    except Exception:  # noqa: BLE001 — o pagamento já está confirmado; o prêmio não pode derrubar
        log.exception("Falha ao premiar a indicação da empresa %s", e.id)
        db.rollback()
    from . import parceiros
    try:
        parceiros.registrar_comissao(db, e, reg)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao registrar a comissão da empresa %s", e.id)
        db.rollback()


def ao_criar_empresa(request: Request, db: Session, u: Usuario, cupom: str = "",
                     contador: bool = False):
    """Cadastro público: liga indicação, parceiro e cupom à empresa nova."""
    from . import parceiros
    e = u.empresa
    cod = request.session.pop("indicacao", "")
    if cod:
        quem = db.scalar(select(Empresa).where(Empresa.codigo_indicacao == cod))
        if quem and quem.id != e.id:
            e.indicada_por_id = quem.id
    par = parceiros.parceiro_da_visita(request, db)
    if par:
        e.parceiro_id, e.contador_acesso = par.id, bool(contador)
    cupom = cupom or request.session.pop("cupom", "")
    if cupom:
        erro = aplicar_cupom(db, e, cupom)
        if erro:
            avisar(request, f"Cupom não aplicado: {erro}", "erro")
    db.commit()
    from . import crm
    crm.lead_do_cadastro(db, e, u)   # conta de teste nova entra no funil do CRM


@rota.get("/i/{codigo}")
def link_indicacao(codigo: str, request: Request):
    request.session["indicacao"] = re.sub(r"[^a-z0-9]", "", codigo.lower())[:12]
    return ir("/")


@rota.get("/cupom/{codigo}")
def link_cupom(codigo: str, request: Request):
    request.session["cupom"] = normalizar(codigo)
    return ir("/")


@rota.post("/painel/assinatura/cupom", dependencies=CSRF)
def usar_cupom(request: Request, codigo: str = Form(""), u: Usuario = Depends(gestor_logado),
               db: Session = Depends(get_db)):
    e = u.empresa
    erro = ("Encerre a cobrança automática antes de usar um cupom; depois assine de novo."
            if e.asaas_assinatura_id else aplicar_cupom(db, e, codigo))
    if erro:
        avisar(request, erro, "erro")
    else:
        db.commit()
        avisar(request, f"Cupom aplicado: {texto_cupom(db.get(Cupom, e.cupom_id))}.")
    return ir("/painel/assinatura")


def resumo_indicacoes(db: Session, e: Empresa) -> dict:
    indicadas = db.scalar(select(func.count()).select_from(Empresa).where(Empresa.indicada_por_id == e.id)) or 0
    premiadas = db.scalar(select(func.count()).select_from(Empresa).where(
        Empresa.indicada_por_id == e.id, Empresa.indicacao_premiada.is_(True))) or 0
    return {"codigo": codigo_da(db, e), "indicadas": indicadas, "premiadas": premiadas}


# ------------------------------------------------------------------ master: cupons
@rota.get("/master/cupons")
def cupons(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    lista = list(db.scalars(select(Cupom).order_by(Cupom.criado_em.desc())))
    em_uso = dict(db.execute(select(Empresa.cupom_id, func.count()).where(
        Empresa.cupom_id.is_not(None)).group_by(Empresa.cupom_id)).all())
    return render(request, "master/cupons.html", u=u, e=u.empresa, aba="master", sub="cupons",
                  cupons=lista, em_uso=em_uso, texto_cupom=texto_cupom)


def _ler_cupom(c: Cupom, codigo: str, descricao: str, pct: int, meses: int, max_usos: int,
               valido_ate: str, ativo: str) -> str | None:
    codigo = normalizar(codigo)
    if len(codigo) < 3:
        return "O código precisa ter pelo menos 3 letras ou números."
    if not 1 <= pct <= 100 or not 0 <= meses <= 120 or not 0 <= max_usos <= 1_000_000:
        return "Desconto de 1% a 100%, meses de 0 a 120 e usos a partir de 0."
    try:
        fim = date.fromisoformat(valido_ate) if valido_ate else None
    except ValueError:
        return "Data inválida."
    c.codigo, c.descricao, c.pct, c.meses = codigo, descricao.strip()[:200], pct, meses
    c.max_usos, c.valido_ate, c.ativo = max_usos, fim, bool(ativo)
    return None


@rota.post("/master/cupons", dependencies=CSRF)
def cupom_novo(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db),
               codigo: str = Form(""), descricao: str = Form(""), pct: int = Form(10),
               meses: int = Form(0), max_usos: int = Form(0), valido_ate: str = Form(""),
               ativo: str = Form("")):
    c = Cupom()
    erro = _ler_cupom(c, codigo, descricao, pct, meses, max_usos, valido_ate, ativo)
    if not erro and db.scalar(select(Cupom.id).where(Cupom.codigo == c.codigo)):
        erro = "Já existe um cupom com este código."
    if erro:
        avisar(request, erro, "erro")
    else:
        db.add(c)
        db.commit()
        avisar(request, f"Cupom {c.codigo} criado.")
    return ir("/master/cupons")


@rota.post("/master/cupons/{cupom_id}", dependencies=CSRF)
def cupom_salvar(cupom_id: int, request: Request, u: Usuario = Depends(admin_logado),
                 db: Session = Depends(get_db), codigo: str = Form(""), descricao: str = Form(""),
                 pct: int = Form(10), meses: int = Form(0), max_usos: int = Form(0),
                 valido_ate: str = Form(""), ativo: str = Form("")):
    c = db.get(Cupom, cupom_id)
    if not c:
        return ir("/master/cupons")
    erro = _ler_cupom(c, codigo, descricao, pct, meses, max_usos, valido_ate, ativo)
    if not erro and db.scalar(select(Cupom.id).where(Cupom.codigo == c.codigo, Cupom.id != c.id)):
        erro = "Já existe um cupom com este código."
    if erro:
        db.rollback()
        avisar(request, erro, "erro")
    else:
        db.commit()
        avisar(request, f"Cupom {c.codigo} salvo. Empresas que já usam continuam com o desconto.")
    return ir("/master/cupons")


# ------------------------------------------------------------------ master: pacote de planos
# (nome, preço, pessoas, locais, ordem, destaque, recursos desligados, benefícios)
PACOTE = [
    ("Iniciante", 0, 10, 1, 0, False,
     ("com_fechamento", "com_rh", "com_totem", "com_gerentes", "com_cmo", "com_pagamento",
      "com_foto", "com_facial", "com_cerca"),
     "Ponto pelo celular com GPS\nEscala da semana e turnos abertos\nMural da equipe\nAvisos de atraso e falta"),
    ("Essencial", 5990, 20, 1, 1, False,
     ("com_rh", "com_totem", "com_gerentes", "com_cmo", "com_pagamento", "com_foto", "com_facial", "com_cerca"),
     "Tudo do Iniciante\nEspelho de ponto, ajustes e justificativas\nBanco de horas e fechamento do mês\nRelatórios"),
    ("Profissional", 13990, 50, 3, 2, True,
     ("com_gerentes", "com_cmo", "com_pagamento", "com_facial"),
     "Tudo do Essencial\nFoto na batida e cerca virtual\nTotem no tablet e 3 locais\nFérias e dados de RH\nAlertas inteligentes"),
    ("Empresarial", 29990, 120, 10, 3, False, (),
     "Tudo do Profissional\nReconhecimento facial\nFolha e pagamentos\nGerentes por setor e local\nCusto de mão de obra\nAté 10 locais"),
    ("Ilimitado", 59990, 0, 0, 4, False, (),
     "Tudo liberado, sem limite de pessoas e locais\nRevisão da folha com IA\nImportação assistida\nAtendimento prioritário"),
]
# Faixas por pessoa (fora de venda; o master liga quando quiser): nome, por pessoa, limite, mínimo
PACOTE_POR_PESSOA = [("Por pessoa 1–10", 690, 10, 3990), ("Por pessoa 11–30", 590, 30, 6990),
                     ("Por pessoa 31–80", 490, 80, 15990), ("Por pessoa 81–200", 390, 200, 31990),
                     ("Por pessoa 200+", 290, 0, 58990)]
RECURSOS = ("com_pagamento", "com_alertas", "com_gps", "com_foto", "com_facial", "com_cerca",
            "com_escala", "com_fechamento", "com_rh", "com_totem", "com_gerentes", "com_cmo")


@rota.post("/master/pacote-planos", dependencies=CSRF)
def aplicar_pacote(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    """Cria os planos recomendados (Faixas de equipe) e as faixas por pessoa, tira de venda os
    planos antigos (quem já usa continua neles), cria o cupom FUNDADORES e põe o teste em 14 dias."""
    antigos = list(db.scalars(select(Plano).where(Plano.ativo.is_(True))))
    for p in antigos:
        p.ativo = False
    for nome, preco, lim, loc, ordem, dest, sem, desc in PACOTE:
        p = Plano(nome=nome, preco_centavos=preco, limite_funcionarios=lim, limite_locais=loc,
                  ordem=ordem, destaque=dest, ativo=True, descricao=desc, tolerancia=2 if lim else 0)
        for r in RECURSOS:
            setattr(p, r, r not in sem)
        db.add(p)
    for i, (nome, pp, lim, mn) in enumerate(PACOTE_POR_PESSOA):
        p = Plano(nome=nome, preco_centavos=max(mn, pp), por_pessoa_centavos=pp, minimo_centavos=mn,
                  limite_funcionarios=lim, limite_locais=0, ordem=10 + i, ativo=False, grupo="por-pessoa",
                  descricao="Todos os recursos do Profissional\nPaga só por quem trabalhou nos últimos 30 dias\nQuanto maior a equipe, menor o preço")
        for r in RECURSOS:
            setattr(p, r, r not in ("com_gerentes", "com_cmo", "com_pagamento", "com_facial"))
        db.add(p)
    if not db.scalar(select(Cupom.id).where(Cupom.codigo == "FUNDADORES")):
        db.add(Cupom(codigo="FUNDADORES", descricao="Clientes Fundadores: 30% de desconto para sempre",
                     pct=30, meses=0, max_usos=100, ativo=True))
    plat = db.get(Plataforma, 1)
    plat.dias_teste, plat.meses_anual = 14, 10
    db.commit()
    plataforma_mudou()
    avisar(request, f"Pacote aplicado: 5 planos à venda, faixas por pessoa prontas (fora de venda), cupom "
                    f"FUNDADORES e teste de 14 dias. {len(antigos)} plano(s) antigo(s) saíram de venda; "
                    "quem já usa continua neles.")
    return ir("/master/planos")


@rota.post("/master/plataforma/anual", dependencies=CSRF)
def salvar_anual(request: Request, meses: int = Form(10), u: Usuario = Depends(admin_logado),
                 db: Session = Depends(get_db)):
    plat = db.get(Plataforma, 1)
    plat.meses_anual = max(1, min(12, meses))
    db.commit()
    plataforma_mudou()
    avisar(request, f"Plano anual: paga {plat.meses_anual} mensalidades por 12 meses.")
    return ir("/master/cupons")
