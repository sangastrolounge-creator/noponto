"""Pausas de descanso da Califórnia (rest breaks).

Regra (Labor Code 226.7 e Wage Orders do IWC): 10 minutos pagos a cada 4 horas trabalhadas ou
"fração maior" disso. Ninguém tem direito a pausa com menos de 3h30 no dia; 1 pausa de 3h30 a 6 h;
2 pausas acima de 6 h até 10 h; 3 pausas acima de 10 h até 14 h; e assim por diante.
O empregador precisa liberar e permitir as pausas (não precisa obrigar). Se não liberou, deve
1 hora de salário (rest premium) naquele dia, no máximo uma por dia, além do meal premium.

Como as pausas são pagas, ninguém bate o ponto nelas. Por isso, na saída, o funcionário responde
se fez as pausas. "Não me liberaram" gera o rest premium e um aviso para o gestor, que pode
corrigir a resposta na tela de problemas de ponto."""
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import push, regras
from . import apuracao
from ... import db as banco
from ...core import avisar, exigir_csrf, gestor_logado, ir, usuario_logado
from ...db import Alerta, Batida, DescansoDia, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MINIMO_MIN = 210          # 3h30
RESPOSTAS = {"sim": "Fez todas as pausas", "escolhi": "Preferiu não fazer",
             "nao_liberado": "Não foi liberado (rest premium)"}
DIAS_REVISAO = 14


def ativo(e) -> bool:
    return e.estado == "CA" and bool(e.descanso_premium)


def pausas_devidas(minutos: int) -> int:
    """Quantas pausas de 10 min a pessoa tinha direito no dia."""
    if minutos < MINIMO_MIN:
        return 0
    return 1 + max(0, (minutos - 360 + 239) // 240) if minutos > 360 else 1


def _minutos_do_dia(db: Session, e, uid: int, dia) -> int:
    from ...custos import minutos_trabalhados
    return minutos_trabalhados(db, e, dia).get(uid, 0)


def pendente(db: Session, u: Usuario) -> dict | None:
    """Dia que a pessoa acabou de encerrar e ainda precisa responder sobre as pausas."""
    e = u.empresa
    if not ativo(e) or u.isento or (u.classificacao or "w2").startswith("1099"):
        return None
    b = regras.ultima_batida(db, u)
    if not b or b.tipo != "saida" or banco.agora() - b.momento > timedelta(hours=20):
        return None
    if db.scalar(select(DescansoDia.id).where(DescansoDia.usuario_id == u.id, DescansoDia.dia == b.dia)):
        return None
    m = _minutos_do_dia(db, e, u.id, b.dia)
    n = pausas_devidas(m)
    return {"dia": b.dia, "pausas": n} if n else None


def gravar(db: Session, u: Usuario, dia, resposta: str, origem: str, por: str) -> DescansoDia:
    d = db.scalar(select(DescansoDia).where(DescansoDia.usuario_id == u.id, DescansoDia.dia == dia))
    if not d:
        d = DescansoDia(empresa_id=u.empresa_id, usuario_id=u.id, dia=dia)
        db.add(d)
    d.resposta, d.origem, d.por, d.em = resposta, origem, por, banco.agora()
    return d


def por_dia(db: Session, empresa_id: int, ini, fim) -> dict:
    return {(d.usuario_id, d.dia): d for d in db.scalars(select(DescansoDia).where(
        DescansoDia.empresa_id == empresa_id, DescansoDia.dia.between(ini, fim)))}


@rota.post("/app/descanso", dependencies=CSRF)
def responder(request: Request, tarefas_bg: BackgroundTasks, resposta: str = Form(""),
              u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    p = pendente(db, u)
    if not p:
        return ir("/app")
    if resposta not in RESPOSTAS:
        avisar(request, "Escolha uma das respostas sobre as pausas.", "erro")
        return ir("/app")
    gravar(db, u, p["dia"], resposta, "funcionario", u.nome)
    if resposta == "nao_liberado":
        texto = f"{u.nome} disse que não foi liberado para as pausas de 10 min em {p['dia']:%m/%d}: 1 h de rest premium."
        db.add(Alerta(empresa_id=u.empresa_id, usuario_id=u.id, tipo="descanso", texto=texto,
                      criado_em=banco.agora()))
        tarefas_bg.add_task(push.avisar_gestores, u.empresa_id, f"Pausas: {u.primeiro_nome}", texto,
                            "/painel/ponto/problemas#descansos", "ponto")
    db.commit()
    avisar(request, "Resposta registrada. Obrigado!")
    return ir("/app")


# ------------------------------------------------------------------ gestor: conferir respostas
def para_revisar(db: Session, e) -> list[dict]:
    """Dias dos últimos 14 dias (já encerrados e não fechados) com direito a pausa em que a resposta
    foi "não liberado" ou ninguém respondeu."""
    if not ativo(e):
        return []
    hoje = regras.hoje(e)
    ini = hoje - timedelta(days=DIAS_REVISAO)
    fechado = apuracao.fechado_ate(db, e)
    if fechado and fechado >= ini:
        ini = fechado + timedelta(days=1)
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id))}
    respostas = por_dia(db, e.id, ini, hoje)
    dias = sorted({(b.usuario_id, b.dia) for b in db.scalars(select(Batida).where(
        Batida.empresa_id == e.id, Batida.dia.between(ini, hoje), regras.VALIDA))}, key=lambda x: (x[1], x[0]))
    lista = []
    for uid, dia in dias:
        u = pessoas.get(uid)
        if not u or u.isento or (u.classificacao or "w2").startswith("1099"):
            continue
        r = respostas.get((uid, dia))
        if r and r.resposta != "nao_liberado":
            continue
        ultima = regras.ultima_batida(db, u)
        if not r and ultima and ultima.dia == dia and ultima.tipo == "entrada":
            continue   # ainda trabalhando
        m = _minutos_do_dia(db, e, uid, dia)
        if pausas_devidas(m) == 0:
            continue
        lista.append({"u": u, "dia": dia, "minutos": m, "pausas": pausas_devidas(m), "r": r,
                      "chave": f"{uid}-{dia.isoformat()}"})
    return lista


@rota.post("/painel/ponto/descansos", dependencies=CSRF)
async def revisar(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    form = await request.form()
    n = 0
    for item in para_revisar(db, e):
        resp = str(form.get(f"desc_{item['chave']}", ""))
        if resp not in RESPOSTAS or (item["r"] and item["r"].resposta == resp):
            continue
        antes = RESPOSTAS[item["r"].resposta] if item["r"] else "sem resposta"
        gravar(db, item["u"], item["dia"], resp, "gestor", u.nome)
        regras.registrar(db, e.id, u.nome, "pausas de descanso",
                         f"{item['u'].nome}, {item['dia']:%m/%d}: {antes} → {RESPOSTAS[resp]}.")
        n += 1
    db.commit()
    avisar(request, f"{n} resposta(s) sobre pausas salva(s)." if n else "Nada mudou nas pausas.")
    return ir("/painel/ponto/problemas#descansos")


@rota.post("/painel/config/descanso", dependencies=CSRF)
def regra(request: Request, ligado: str = Form(""), u: Usuario = Depends(gestor_logado),
          db: Session = Depends(get_db)):
    u.empresa.descanso_premium = bool(ligado)
    regras.registrar(db, u.empresa_id, u.nome, "pausas de descanso (Califórnia)",
                     "Pergunta na saída e rest premium ligados." if ligado else "Desligados.")
    db.commit()
    avisar(request, "Regra salva.")
    return ir("/painel/ponto/problemas#descansos")
