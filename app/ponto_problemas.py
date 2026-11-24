"""Problemas de ponto resolvidos em lote e regras automáticas do ponto.

- Saída esquecida: o dia tem entrada sem saída. O horário sugerido é o fim da escala; o gestor
  confere, muda se precisar e lança vários de uma vez.
- Saída esquecida (regra): X min depois do fim da escala, quem não bateu a saída recebe um lembrete
  e o gestor um alerta. O sistema não marca a saída sozinho (Portaria 671, art. 74, II): o gestor
  lança, com o horário do fim da escala sugerido. Saídas automáticas antigas continuam na lista.
- Entrada antecipada (regra): a entrada feita mais de X min antes do turno é registrada (a Portaria
  proíbe restringir o horário, art. 74, I) e fica "para conferir" (`batidas.conferida = False`): o
  gestor mantém o horário marcado ou considera a partir de outro, com a marcação original
  desconsiderada e o motivo registrado."""
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import apuracao, push, regras
from . import db as banco
from .core import HORA_RE, avisar, exigir_csrf, gestor_logado, ir, pode, render
from .db import Batida, Turno, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
DIAS_PARA_TRAS = 14


def problemas(db: Session, e) -> list[dict]:
    """Saídas esquecidas e saídas automáticas para conferir, dos últimos 14 dias."""
    agora_l = regras.agora_local(e)
    hoje = agora_l.date()
    fechado = apuracao.fechado_ate(db, e)
    ini = hoje - timedelta(days=DIAS_PARA_TRAS)
    if fechado and fechado >= ini:
        ini = fechado + timedelta(days=1)
    por_dia: dict[tuple[int, object], list[Batida]] = {}
    for b in db.scalars(select(Batida).where(Batida.empresa_id == e.id, Batida.dia >= ini,
                                             regras.VALIDA).order_by(Batida.momento)):
        por_dia.setdefault((b.usuario_id, b.dia), []).append(b)
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id))}
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data >= ini, Turno.folga.is_(False)))}
    lista = []
    for (uid, dia), bats in sorted(por_dia.items(), key=lambda x: (x[0][1], pessoas[x[0][0]].nome)):
        u, t = pessoas.get(uid), turnos.get((uid, dia))
        ultima = bats[-1]
        if ultima.tipo == "entrada":
            ent_l = regras.para_local(e, ultima.momento)
            fim_l = regras.intervalo(t)[1] if t else None
            # ainda trabalhando: turno não acabou, ou sem escala e entrada há menos de 12 h
            trabalhando = (fim_l and fim_l > agora_l) or (not fim_l and agora_l - ent_l < timedelta(hours=12))
            if not trabalhando:
                sugestao = fim_l if fim_l and fim_l > ent_l else None
                lista.append({"chave": f"s-{uid}-{dia.isoformat()}", "tipo": "sem_saida", "u": u,
                              "dia": dia, "entrada": ent_l, "turno": t, "sugestao": sugestao})
        for b in bats:
            if b.tipo == "entrada" and not b.conferida and not b.manual and b.origem != "auto":
                lista.append({"chave": f"e-{b.id}", "tipo": "antecipada", "u": u, "dia": dia,
                              "entrada": regras.para_local(e, b.momento), "turno": t,
                              "sugestao": regras.para_local(e, b.momento), "batida": b,
                              "inicio": regras.intervalo(t)[0] if t else None})
            if b.origem == "auto" and not b.conferida:
                lista.append({"chave": f"a-{b.id}", "tipo": "auto", "u": u, "dia": dia,
                              "entrada": None, "turno": t, "sugestao": regras.para_local(e, b.momento),
                              "batida": b})
    return lista


def _hora_no_dia(base: datetime, hora: str, depois_de: datetime | None) -> datetime:
    h, m = map(int, hora.split(":"))
    alvo = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if depois_de and alvo <= depois_de:
        alvo += timedelta(days=1)   # saída depois da meia-noite
    return alvo


# ------------------------------------------------------------------ telas
@rota.get("/painel/ponto/problemas")
def tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    return render(request, "painel_problemas.html", u=u, e=e, aba="ponto",
                  lista=problemas(db, e), regras_dono=pode(u, "dono"))


@rota.post("/painel/ponto/problemas", dependencies=CSRF)
async def resolver(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    form = await request.form()
    feitos = erros = 0
    agora_l = regras.agora_local(e)
    for p in problemas(db, e):
        if not form.get(f"sel_{p['chave']}"):
            continue
        hora = str(form.get(f"hora_{p['chave']}", "")).strip()
        if not HORA_RE.match(hora):
            erros += 1
            continue
        if p["tipo"] == "antecipada":
            b = p["batida"]
            if hora == p["sugestao"].strftime("%H:%M"):
                b.conferida = True
                regras.registrar(db, e.id, u.nome, "entrada antecipada mantida",
                                 f"{p['u'].nome}, {p['dia']:%d/%m}, entrada às {hora}.")
            else:
                quando = _hora_no_dia(p["sugestao"], hora, None)
                if quando < p["sugestao"] or quando > agora_l or quando - p["sugestao"] > timedelta(hours=12):
                    erros += 1
                    continue
                b.anulada, b.anulada_por, b.anulada_em = True, u.nome, banco.agora()
                b.conferida = True
                regras.lancar_manual(db, p["u"], "entrada", quando,
                                     f"Entrada antecipada considerada a partir de {hora} por {u.nome} "
                                     f"(marcada às {p['sugestao']:%H:%M})")
                regras.registrar(db, e.id, u.nome, "entrada antecipada ajustada",
                                 f"{p['u'].nome}, {p['dia']:%d/%m}: marcada às {p['sugestao']:%H:%M}, "
                                 f"considerada a partir de {hora}.")
        elif p["tipo"] == "sem_saida":
            quando = _hora_no_dia(p["entrada"], hora, p["entrada"])
            if quando > agora_l or quando - p["entrada"] > timedelta(hours=18):
                erros += 1
                continue
            regras.lancar_manual(db, p["u"], "saida", quando,
                                 f"Correção em lote por {u.nome}: saída esquecida")
            regras.registrar(db, e.id, u.nome, "saída esquecida lançada",
                             f"{p['u'].nome}, {p['dia']:%d/%m}, saída às {quando:%H:%M}.")
        else:
            b = p["batida"]
            if hora == p["sugestao"].strftime("%H:%M"):
                b.conferida = True
                regras.registrar(db, e.id, u.nome, "saída automática conferida",
                                 f"{p['u'].nome}, {p['dia']:%d/%m}, {hora}.")
            else:
                ent = db.scalar(select(Batida).where(
                    Batida.usuario_id == b.usuario_id, Batida.tipo == "entrada", regras.VALIDA,
                    Batida.momento < b.momento).order_by(Batida.momento.desc()).limit(1))
                ent_l = regras.para_local(e, ent.momento) if ent else None
                quando = _hora_no_dia(ent_l or p["sugestao"], hora, ent_l)
                if quando > agora_l:
                    erros += 1
                    continue
                b.anulada, b.anulada_por, b.anulada_em = True, u.nome, banco.agora()
                b.conferida = True
                regras.lancar_manual(db, p["u"], "saida", quando,
                                     f"Correção por {u.nome}: saída automática trocada")
                regras.registrar(db, e.id, u.nome, "saída automática corrigida",
                                 f"{p['u'].nome}, {p['dia']:%d/%m}: {p['sugestao']:%H:%M} → {quando:%H:%M}.")
        feitos += 1
    db.commit()
    if feitos:
        avisar(request, f"{feitos} problema(s) resolvido(s). {erros} com horário inválido ficaram na lista."
               if erros else f"{feitos} problema(s) resolvido(s).")
    else:
        avisar(request, "Marque os problemas que quer resolver e confira o horário." if not erros
               else "Confira os horários: a saída precisa ser depois da entrada e não pode estar no futuro.", "erro")
    return ir("/painel/ponto/problemas")


@rota.post("/painel/config/regras-ponto", dependencies=CSRF)
def regras_ponto(request: Request, entrada_antecipada_min: str = Form("0"), saida_auto_min: str = Form("0"),
                 aviso_extra_min: str = Form("0"),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    vals = [entrada_antecipada_min.strip() or "0", saida_auto_min.strip() or "0", aviso_extra_min.strip() or "0"]
    if not all(v.isdigit() and int(v) <= 720 for v in vals):
        avisar(request, "Use minutos entre 0 e 720 (0 = desligado).", "erro")
        return ir("/painel/ponto/problemas#regras")
    e.entrada_antecipada_min, e.saida_auto_min, e.aviso_extra_min = map(int, vals)
    regras.registrar(db, e.id, u.nome, "regras automáticas do ponto",
                     f"Sinalizar entrada mais de {e.entrada_antecipada_min} min antes (0 = desligado); "
                     f"avisar saída esquecida {e.saida_auto_min} min depois do fim (0 = 60 min).")
    db.commit()
    avisar(request, "Regras do ponto salvas.")
    return ir("/painel/ponto/problemas#regras")
