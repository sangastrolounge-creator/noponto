"""Escala justa (Fair Workweek / predictive scheduling).

Algumas cidades e o estado do Oregon exigem, de redes maiores:
1. Publicar a escala com antecedência (em geral 14 dias).
2. Pagar um adicional ("predictability pay") quando o empregador muda um turno já publicado
   dentro desse prazo: 1 hora de salário quando acrescenta ou muda o horário; metade das horas
   perdidas quando corta ou cancela.
3. Dar um descanso mínimo entre um turno e o seguinte (fechar à noite e abrir de manhã, o
   "clopening"). Horas trabalhadas dentro desse descanso pagam mais (1,5x, 1,25x ou um valor fixo).

As leis só valem para empresas de certo tamanho e setor (veja `cobertura`). A empresa liga a regra
em Configurações. Mudanças pedidas pelo próprio funcionário (trocas aprovadas, pedido por escrito)
não geram adicional. Resumo das regras de 2026; confira sempre o texto oficial da cidade."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import regras
from ... import db as banco
from ...db import Batida, EscalaPublicada, PremioEscala, Turno

# chave -> regra. descanso: (horas mínimas, ("mult", fator) | ("fixo", centavos)) ou None.
# muda=False: o adicional por mudança tem valores fixos por tipo e não é calculado aqui.
REGRAS = {
    "oregon": dict(nome="Oregon (statewide)", estado="OR", aviso=14, descanso=(10, ("mult", 1.5)), muda=True,
                   cobertura="Retail, hotel, motel and food service employers with 500+ employees worldwide."),
    "seattle": dict(nome="Seattle", estado="WA", aviso=14, descanso=(10, ("mult", 1.5)), muda=True,
                    cobertura="Retail and food service with 500+ employees worldwide (full-service restaurants: also 40+ locations)."),
    "losangeles": dict(nome="Los Angeles (city)", estado="CA", aviso=14, descanso=(10, ("mult", 1.5)), muda=True,
                       cobertura="Retail employers with 300+ employees worldwide."),
    "lacounty": dict(nome="Los Angeles County (unincorporated)", estado="CA", aviso=14, descanso=(10, ("mult", 1.5)),
                     muda=True, cobertura="Retail employers with 300+ employees worldwide."),
    "sanfrancisco": dict(nome="San Francisco", estado="CA", aviso=14, descanso=None, muda=True,
                         cobertura="Formula retail chains (40+ locations worldwide) with 20+ employees in San Francisco."),
    "berkeley": dict(nome="Berkeley", estado="CA", aviso=14, descanso=(11, ("mult", 1.5)), muda=True,
                     cobertura="Retail, food service and hotels above the city's size threshold."),
    "emeryville": dict(nome="Emeryville", estado="CA", aviso=14, descanso=(11, ("mult", 1.5)), muda=True,
                       cobertura="Retail and fast food employers with 56+ employees worldwide."),
    "chicago": dict(nome="Chicago", estado="IL", aviso=14, descanso=(10, ("mult", 1.25)), muda=True,
                    cobertura="Covered industries with 100+ employees worldwide (restaurants: 30+ locations and 250+ employees), for workers under the wage cap."),
    "philadelphia": dict(nome="Philadelphia", estado="PA", aviso=14, descanso=(9, ("fixo", 4000)), muda=True,
                         cobertura="Retail, hospitality and food service with 250+ employees and 30+ locations worldwide."),
    "nyc_fastfood": dict(nome="New York City: fast food", estado="NY", aviso=14, descanso=(11, ("fixo", 10000)),
                         muda=False, cobertura="Fast food chains with 30+ locations nationally."),
    "nyc_retail": dict(nome="New York City: retail", estado="NY", aviso=3, descanso=None, muda=False,
                       cobertura="Retail employers with 20+ employees in New York City (72-hour notice)."),
}


def regra(e) -> dict | None:
    r = REGRAS.get(e.escala_justa or "")
    return r if r and r["estado"] == e.estado else None


def da_empresa_opcoes(estado: str) -> list[tuple[str, str]]:
    return [(k, v["nome"]) for k, v in REGRAS.items() if v["estado"] == estado]


def prazo(e, seg: date) -> date | None:
    """Último dia para publicar a escala da semana que começa em `seg`."""
    r = regra(e)
    return seg - timedelta(days=r["aviso"]) if r else None


def situacao_semana(db: Session, e, seg: date) -> dict | None:
    r = regra(e)
    if not r:
        return None
    pub = db.scalar(select(EscalaPublicada).where(EscalaPublicada.empresa_id == e.id, EscalaPublicada.semana == seg))
    limite = prazo(e, seg)
    hoje = regras.hoje(e)
    publicada = bool(pub and pub.publicado_em)
    no_prazo = publicada and regras.para_local(e, pub.publicado_em).date() <= limite
    return {"regra": r, "prazo": limite, "publicada": publicada, "no_prazo": no_prazo,
            "atrasada": (not publicada and hoje > limite) or (publicada and not no_prazo),
            "janela_ate": hoje + timedelta(days=r["aviso"] - 1)}


# ------------------------------------------------------------------ mudanças em turnos publicados
def _publicado(db: Session, e, uid: int, d: date) -> str | None:
    """Como o dia estava na última publicação: "F" (folga), "HH:MM-HH:MM", "" (sem escala) ou None."""
    import json
    seg = d - timedelta(days=d.weekday())
    pub = db.scalar(select(EscalaPublicada).where(EscalaPublicada.empresa_id == e.id, EscalaPublicada.semana == seg))
    if not pub or not pub.publicado_em:
        return None
    sig = json.loads(pub.assinaturas or "{}").get(str(uid), "")
    for parte in sig.split("|"):
        if parte.startswith(f"{d:%m%d}:"):
            return parte.split(":", 1)[1].rsplit(":", 1)[0]
    return ""


def _minutos(v: str) -> tuple[int, int, int]:
    """(início, fim, duração) em minutos do dia; 0 para folga ou sem escala."""
    if not v or v == "F":
        return 0, 0, 0
    a, b = v.split("-")
    ini = int(a[:2]) * 60 + int(a[3:])
    fim = int(b[:2]) * 60 + int(b[3:])
    if fim <= ini:
        fim += 1440
    return ini, fim, fim - ini


def _valor(t: Turno | None) -> str:
    if not t:
        return ""
    return "F" if t.folga else f"{t.inicio}-{t.fim}"


def premio_da_mudanca(antes: str, depois: str) -> tuple[int, str]:
    """Minutos de adicional (ao valor da hora) e o motivo."""
    if antes == depois:
        return 0, ""
    a_dur, d_dur = _minutos(antes)[2], _minutos(depois)[2]
    perdidos = max(0, a_dur - d_dur)
    if perdidos:
        return perdidos // 2, "horas cortadas: metade das horas perdidas"
    return 60, "turno acrescentado ou mudado: 1 hora"


def avaliar_mudancas(db: Session, e, uid: int, dias: list[date], antes: dict, autor: str,
                     a_pedido: bool) -> list[PremioEscala]:
    """Chamado depois de o gestor salvar a escala. `antes`: {dia: valor antes da mudança}."""
    r = regra(e)
    if not r or not r["muda"]:
        return []
    hoje = regras.hoje(e)
    novos = []
    for d in dias:
        if not (hoje <= d < hoje + timedelta(days=r["aviso"])):
            continue
        publicado = _publicado(db, e, uid, d)
        if publicado is None or antes.get(d) == _valor_atual(db, uid, d):
            continue
        atual = _valor_atual(db, uid, d)
        if a_pedido:
            continue   # pedido do funcionário: não gera adicional e não apaga o que já era devido
        p = db.scalar(select(PremioEscala).where(PremioEscala.usuario_id == uid, PremioEscala.data == d))
        minutos, motivo = premio_da_mudanca(publicado, atual)
        if not minutos:
            if p:
                db.delete(p)
            continue
        if not p:
            p = PremioEscala(empresa_id=e.id, usuario_id=uid, data=d)
            db.add(p)
        p.minutos, p.antes, p.depois, p.detalhe = minutos, publicado or "", atual, motivo
        p.criado_por, p.criado_em = autor, banco.agora()
        novos.append(p)
    return novos


def _valor_atual(db: Session, uid: int, d: date) -> str:
    return _valor(db.scalar(select(Turno).where(Turno.usuario_id == uid, Turno.data == d)))


def antes_de_mudar(db: Session, uid: int, dias: list[date]) -> dict:
    return {d: _valor_atual(db, uid, d) for d in dias}


# ------------------------------------------------------------------ descanso curto entre turnos
def descansos_curtos_na_grade(e, turnos: dict, equipe, dias: list[date]) -> set[tuple[int, date]]:
    """Dias da grade em que o turno começa antes do descanso mínimo depois do turno anterior."""
    r = regra(e)
    if not r or not r["descanso"]:
        return set()
    minimo = timedelta(hours=r["descanso"][0])
    marcados = set()
    for f in equipe:
        for d in dias:
            t, ontem = turnos.get((f.id, d)), turnos.get((f.id, d - timedelta(days=1)))
            if t and ontem and not t.folga and not ontem.folga and t.inicio and ontem.fim:
                if regras.intervalo(t)[0] - regras.intervalo(ontem)[1] < minimo:
                    marcados.add((f.id, d))
    return marcados


def descanso_curto(e, entrada_hoje: datetime, saida_ontem: datetime | None, trabalhado_hoje: int) -> tuple[int, int]:
    """Adicional pelo descanso curto (minutos ao valor da hora, centavos fixos)."""
    r = regra(e)
    if not r or not r["descanso"] or not saida_ontem:
        return 0, 0
    horas, (tipo, valor) = r["descanso"]
    folga = entrada_hoje - saida_ontem
    if folga >= timedelta(hours=horas) or folga < timedelta(0):
        return 0, 0
    dentro = min(trabalhado_hoje, int((timedelta(hours=horas) - folga).total_seconds() // 60))
    if dentro <= 0:
        return 0, 0
    if tipo == "fixo":
        return 0, valor
    return int(dentro * (valor - 1) + 0.5), 0


def premios_por_dia(db: Session, e, ini: date, fim: date) -> dict:
    return {(p.usuario_id, p.data): p for p in db.scalars(select(PremioEscala).where(
        PremioEscala.empresa_id == e.id, PremioEscala.data.between(ini, fim)))}


def ultima_saida_antes(db: Session, uid: int, momento: datetime) -> datetime | None:
    b = db.scalar(select(Batida).where(Batida.usuario_id == uid, Batida.tipo == "saida", regras.VALIDA,
                                       Batida.momento < momento, Batida.momento >= momento - timedelta(hours=24))
                  .order_by(Batida.momento.desc()).limit(1))
    return b.momento if b else None


# ------------------------------------------------------------------ configuração
from fastapi import APIRouter, Depends, Form, Request  # noqa: E402

rota = APIRouter()


def _rotas():
    from ...core import avisar, exigir_csrf, gestor_logado, ir
    from ...db import Usuario, get_db

    @rota.post("/painel/config/escala-justa", dependencies=[Depends(exigir_csrf)])
    def salvar(request: Request, regra_fw: str = Form(""), u: Usuario = Depends(gestor_logado),
               db: Session = Depends(get_db)):
        e = u.empresa
        if regra_fw and (regra_fw not in REGRAS or REGRAS[regra_fw]["estado"] != e.estado):
            avisar(request, "Escolha uma regra do estado da empresa.", "erro")
            return ir("/painel/config#escala-justa")
        e.escala_justa = regra_fw
        regras.registrar(db, e.id, u.nome, "escala justa (Fair Workweek)",
                         REGRAS[regra_fw]["nome"] if regra_fw else "Desligada.")
        db.commit()
        avisar(request, "Regra de escala justa salva.")
        return ir("/painel/config#escala-justa")


_rotas()
