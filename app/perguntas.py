"""Perguntas de fim de turno: a empresa cria até 5 perguntas (sim/não, número ou texto) que o
funcionário responde ao bater a saída. Ex.: "Houve algum acidente hoje?" (avisar o gestor se Sim),
"Quantas gorjetas em dinheiro você recebeu?". As respostas aparecem para o gestor."""
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import push, regras
from . import db as banco
from .core import avisar, exigir_csrf, gestor_logado, ir, render, usuario_logado
from .db import PerguntaTurno, RespostaTurno, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MAX_PERGUNTAS = 5
TIPOS = {"sim_nao": "Sim ou não", "numero": "Número", "texto": "Texto livre"}
EXEMPLOS = ["Houve algum acidente ou alguém se machucou hoje?", "Quanto você recebeu de gorjeta em dinheiro?",
            "Faltou algum produto ou material?", "Alguma observação para o próximo turno?"]


def ativas(db: Session, empresa_id: int) -> list[PerguntaTurno]:
    return list(db.scalars(select(PerguntaTurno).where(PerguntaTurno.empresa_id == empresa_id,
                                                       PerguntaTurno.ativo.is_(True)).order_by(PerguntaTurno.ordem, PerguntaTurno.id)))


def pendente(db: Session, u: Usuario) -> dict | None:
    """Dia que a pessoa acabou de encerrar e ainda não respondeu (com as perguntas)."""
    if u.gestor and not u.na_escala:
        return None
    ps = ativas(db, u.empresa_id)
    if not ps:
        return None
    b = regras.ultima_batida(db, u)
    if not b or b.tipo != "saida" or banco.agora() - b.momento > timedelta(hours=20):
        return None
    if db.scalar(select(RespostaTurno.id).where(RespostaTurno.usuario_id == u.id, RespostaTurno.dia == b.dia).limit(1)):
        return None
    return {"dia": b.dia, "perguntas": ps}


@rota.post("/app/perguntas", dependencies=CSRF)
async def responder(request: Request, tarefas_bg: BackgroundTasks, u: Usuario = Depends(usuario_logado),
                    db: Session = Depends(get_db)):
    p = pendente(db, u)
    if not p:
        return ir("/app")
    form = await request.form()
    pular = form.get("pular") == "1"
    valores = {}
    for q in p["perguntas"]:
        v = "" if pular else str(form.get(f"p_{q.id}", "")).strip()[:300]
        if not pular and q.tipo == "sim_nao" and v not in ("sim", "nao"):
            avisar(request, "Responda Sim ou Não em todas as perguntas.", "erro")
            return ir("/app")
        if not pular and q.tipo == "numero" and v:
            try:
                n = v.replace("R$", "").replace("$", "").replace(" ", "")
                float(n.replace(".", "").replace(",", ".") if "," in n else n)
            except ValueError:
                avisar(request, "Use só números na resposta: {v1}".replace("{v1}", q.texto), "erro")
                return ir("/app")
        valores[q.id] = v
    avisos = []
    for q in p["perguntas"]:
        db.add(RespostaTurno(empresa_id=u.empresa_id, usuario_id=u.id, pergunta_id=q.id, dia=p["dia"],
                             valor=valores[q.id], criado_em=banco.agora()))
        if q.tipo == "sim_nao" and q.avisar_se and valores[q.id] == q.avisar_se:
            avisos.append((q.texto, "Sim" if valores[q.id] == "sim" else "Não"))
    db.commit()
    for texto, resp in avisos:
        tarefas_bg.add_task(push.avisar_gestores, u.empresa_id, f"Fim de turno: {u.primeiro_nome}",
                            f"{u.nome} respondeu {resp} para “{texto}”.", "/painel/perguntas", "ponto")
    if not pular:
        avisar(request, "Respostas enviadas. Obrigado!")
    return ir("/app")


# ------------------------------------------------------------------ gestor
@rota.get("/painel/perguntas")
def tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    hoje = regras.hoje(e)
    respostas = list(db.scalars(select(RespostaTurno).where(
        RespostaTurno.empresa_id == e.id, RespostaTurno.dia >= hoje - timedelta(days=14))
        .order_by(RespostaTurno.dia.desc(), RespostaTurno.usuario_id, RespostaTurno.pergunta_id)))
    por_pessoa_dia: dict = {}
    for r in respostas:
        por_pessoa_dia.setdefault((r.dia, r.usuario_id), {"dia": r.dia, "u": r.usuario, "itens": []})["itens"].append(r)
    todas = list(db.scalars(select(PerguntaTurno).where(PerguntaTurno.empresa_id == e.id).order_by(PerguntaTurno.ordem, PerguntaTurno.id)))
    return render(request, "painel_perguntas.html", u=u, e=e, aba="ponto", perguntas=todas,
                  respostas=list(por_pessoa_dia.values()), TIPOS=TIPOS, EXEMPLOS=EXEMPLOS, MAX=MAX_PERGUNTAS)


@rota.post("/painel/perguntas", dependencies=CSRF)
def nova(request: Request, texto: str = Form(""), tipo: str = Form("sim_nao"), avisar_se: str = Form(""),
         u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    texto = texto.strip()[:200]
    if not texto or tipo not in TIPOS:
        avisar(request, "Escreva a pergunta e escolha o tipo de resposta.", "erro")
    elif len(ativas(db, e.id)) >= MAX_PERGUNTAS:
        avisar(request, f"Use no máximo {MAX_PERGUNTAS} perguntas, para a saída continuar rápida.", "erro")
    else:
        db.add(PerguntaTurno(empresa_id=e.id, texto=texto, tipo=tipo,
                             avisar_se=avisar_se if tipo == "sim_nao" and avisar_se in ("sim", "nao") else "",
                             ordem=len(ativas(db, e.id))))
        regras.registrar(db, e.id, u.nome, "pergunta de fim de turno", f"Nova: {texto}")
        db.commit()
        avisar(request, "Pergunta criada. A equipe responde ao bater a saída.")
    return ir("/painel/perguntas")


@rota.post("/painel/perguntas/{pid}", dependencies=CSRF)
def acao(request: Request, pid: int, acao: str = Form(""), u: Usuario = Depends(gestor_logado),
         db: Session = Depends(get_db)):
    q = db.get(PerguntaTurno, pid)
    if q and q.empresa_id == u.empresa_id:
        if acao == "desligar":
            q.ativo = False
        elif acao == "ligar" and len(ativas(db, u.empresa_id)) < MAX_PERGUNTAS:
            q.ativo = True
        regras.registrar(db, u.empresa_id, u.nome, "pergunta de fim de turno",
                         f"{'Desligada' if not q.ativo else 'Ligada'}: {q.texto}")
        db.commit()
    return ir("/painel/perguntas")
