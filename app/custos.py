"""Custo da equipe x vendas (sem integração com o caixa: o gestor digita a venda do dia) e a
publicação da escala da semana (avisa no celular quem teve a escala mudada).

O custo é ESTIMADO pelo valor da hora de cada um (salário ÷ jornada mensal, cadastrados na ficha
ou em Pagamento): horas trabalhadas x valor da hora. Encargos (INSS da empresa, FGTS, férias,
13º), hora extra e gorjeta não entram: o valor exato sai no Pagamento."""
import json
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import push, regras
from . import db as banco
from .core import avisar, data_iso, exigir_csrf, gestor_logado, ir, ler_centavos, pode
from .db import Batida, EscalaPublicada, Turno, Usuario, VendaDia, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]


# ------------------------------------------------------------------ cálculo
def valor_hora(u: Usuario) -> float:
    """Valor da hora normal em centavos: salário mensal ÷ jornada mensal (220 h se vazia)."""
    return u.salario_centavos / (u.jornada_mensal or 220) if u.salario_centavos else 0


def minutos_trabalhados(db: Session, e, d: date) -> dict[int, int]:
    """Minutos trabalhados no dia por pessoa (quem ainda está trabalhando conta até agora)."""
    agora = banco.agora()
    por = {}
    abertas = {}
    for b in db.scalars(select(Batida).where(Batida.empresa_id == e.id, Batida.dia == d,
                                             regras.VALIDA).order_by(Batida.momento)):
        if b.tipo == "entrada":
            abertas[b.usuario_id] = b.momento
        elif b.usuario_id in abertas:
            ini = abertas.pop(b.usuario_id)
            por[b.usuario_id] = por.get(b.usuario_id, 0) + int((b.momento - ini).total_seconds() // 60)
    for uid, ini in abertas.items():
        if agora - ini < timedelta(hours=18):
            por[uid] = por.get(uid, 0) + max(0, int((agora - ini).total_seconds() // 60))
    return por


def custo_do_dia(db: Session, e, d: date) -> dict:
    """{'minutos', 'centavos', 'sem_valor': nomes sem valor da hora cadastrado}"""
    mins = minutos_trabalhados(db, e, d)
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id))}
    total, sem_valor = 0, []
    for uid, m in mins.items():
        u = pessoas.get(uid)
        if not u:
            continue
        if valor_hora(u):
            total += round(m * valor_hora(u) / 60)
        elif m:
            sem_valor.append(u.primeiro_nome)
    return {"minutos": sum(mins.values()), "centavos": total, "sem_valor": sem_valor}


def custo_previsto(t: Turno, u: Usuario, e) -> int:
    if not t or t.folga:
        return 0
    return round(regras.minutos_turno(t) * valor_hora(u) / 60)


def vendas(db: Session, empresa_id: int, ini: date, fim: date) -> dict[date, int]:
    return {v.data: v.valor_centavos for v in db.scalars(select(VendaDia).where(
        VendaDia.empresa_id == empresa_id, VendaDia.data.between(ini, fim)))}


def resumo_dias(db: Session, e, fim: date, n: int = 7) -> list[dict]:
    """Últimos n dias até `fim`: vendas, custo e % do custo sobre as vendas."""
    ini = fim - timedelta(days=n - 1)
    vs = vendas(db, e.id, ini, fim)
    linhas = []
    for i in range(n):
        d = fim - timedelta(days=i)
        c = custo_do_dia(db, e, d)
        v = vs.get(d)
        linhas.append({"dia": d, "vendas": v, "custo": c["centavos"], "minutos": c["minutos"],
                       "pct": round(c["centavos"] * 100 / v, 1) if v else None,
                       "sem_valor": c["sem_valor"]})
    return linhas


# ------------------------------------------------------------------ escala publicada
def assinaturas(turnos: dict, equipe: list[Usuario], dias: list[date]) -> dict[str, str]:
    """Um texto por pessoa que resume a semana dela (muda quando a escala dela muda)."""
    sai = {}
    for f in equipe:
        partes = []
        for d in dias:
            t = turnos.get((f.id, d))
            if t:
                partes.append(f"{d:%d%m}:{'F' if t.folga else t.inicio + '-' + t.fim}:{t.local_id or ''}")
        sai[str(f.id)] = "|".join(partes)
    return sai


def mudados(db: Session, e, seg: date, atuais: dict[str, str]) -> tuple[list[int], EscalaPublicada | None]:
    pub = db.scalar(select(EscalaPublicada).where(EscalaPublicada.empresa_id == e.id,
                                                  EscalaPublicada.semana == seg))
    antes = json.loads(pub.assinaturas) if pub else {}
    return [int(uid) for uid, sig in atuais.items() if sig and sig != antes.get(uid, "")
            or (not sig and antes.get(uid))], pub


# ------------------------------------------------------------------ rotas
@rota.post("/painel/vendas", dependencies=CSRF)
def venda_salvar(request: Request, data: str = Form(""), valor: str = Form(""),
                 volta: str = Form("/painel"), u: Usuario = Depends(gestor_logado),
                 db: Session = Depends(get_db)):
    volta = volta if volta.startswith("/painel") else "/painel"
    if not pode(u, "pagamento"):
        avisar(request, "Você não tem acesso a esta parte do painel. Fale com o dono da conta.", "erro")
        return ir("/painel")
    d = data_iso(data, None)
    centavos = ler_centavos(valor) if valor.strip() else 0
    if not d or d > regras.hoje(u.empresa) or centavos is None or centavos > 10**10:
        avisar(request, "Informe o dia e o valor das vendas (ex.: 2.450,00).", "erro")
        return ir(volta)
    v = db.scalar(select(VendaDia).where(VendaDia.empresa_id == u.empresa_id, VendaDia.data == d))
    if not centavos:
        if v:
            db.delete(v)
    else:
        if not v:
            v = VendaDia(empresa_id=u.empresa_id, data=d)
            db.add(v)
        v.valor_centavos, v.registrado_por = centavos, u.nome
    db.commit()
    avisar(request, "Vendas salvas.")
    return ir(volta)


@rota.post("/painel/escala/publicar", dependencies=CSRF)
def escala_publicar(request: Request, tarefas_bg: BackgroundTasks, semana: str = Form(...),
                    u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    seg = data_iso(semana, None)
    if not seg:
        return ir("/painel/escala")
    seg = seg - timedelta(days=seg.weekday())
    dias = [seg + timedelta(days=i) for i in range(7)]
    equipe = list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True), Usuario.na_escala.is_(True))))
    regras.gerar_da_fixa(db, e, dias[0], dias[-1])
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(dias[0], dias[-1])))}
    atuais = assinaturas(turnos, equipe, dias)
    ids, pub = mudados(db, e, seg, atuais)
    if not pub:
        pub = EscalaPublicada(empresa_id=e.id, semana=seg)
        db.add(pub)
    primeira = pub.publicado_em is None
    # Mescla com a publicação anterior: um gerente que só vê parte da equipe não apaga a dos outros.
    todas = {**(json.loads(pub.assinaturas) if pub.assinaturas else {}), **atuais}
    pub.assinaturas, pub.publicado_em, pub.publicado_por = json.dumps(todas), banco.agora(), u.nome
    regras.registrar(db, e.id, u.nome, "escala publicada",
                     f"Semana de {seg:%d/%m}; {len(ids)} pessoa(s) avisada(s).")
    db.commit()
    titulo = "Escala publicada" if primeira else "Escala atualizada"
    tarefas_bg.add_task(push.avisar_usuarios, [
        (uid, titulo, f"Sua escala de {dias[0]:%d/%m} a {dias[-1]:%d/%m} está no app.", "/app/escala")
        for uid in ids])
    avisar(request, f"Escala publicada. {len(ids)} pessoa(s) avisada(s) no celular."
           if ids else "Escala publicada. Ninguém teve mudança desde a última publicação.")
    return ir(f"/painel/escala?semana={seg.isoformat()}")
