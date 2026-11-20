"""Ajuste de ponto (apropriação de horas): o dia a dia de cada colaborador no período, com um
menu de ações por dia — pontos originais, histórico, ponto em atraso, preenchimento automático,
motivo de ajuste, atestados e afastamentos, sobreaviso e a escala do dia.

Nada é apagado: batidas lançadas pelo gestor ficam marcadas como manuais (Portaria MTP 671/2021,
as marcações originais do colaborador não podem ser alteradas) e tudo vai para o histórico."""
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import apuracao, regras
from . import db as banco
from .apuracao import MSG_FECHADO, TIPOS_AUSENCIA
from .core import (HORA_RE, avisar, da_empresa, data_iso, exigir_csrf, gestor_logado, ir,
                   render)
from .db import (Ajuste, Ausencia, Batida, Historico, MotivoAjuste, Sobreaviso, Turno, Usuario,
                 get_db)
from .regras import VALIDA

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

ACOES = {
    "originais": "Coleta de pontos originais",
    "historico": "Histórico de alterações",
    "atraso": "Lançar ponto em atraso",
    "preencher": "Preenchimento automático",
    "motivo": "Lançar motivo de ajuste",
    "ausencia": "Lançar atestados e afastamentos",
    "sobreaviso": "Lançar sobreaviso",
    "escala": "Consultar escala",
}
MOTIVOS = ["Esqueceu de bater o ponto", "Problema no celular ou no totem", "Trabalho externo",
           "Batida fora da área autorizada", "Compensação de horas", "Atraso justificado",
           "Falta justificada", "Erro na marcação", "Outro"]
# Ausências que fazem sentido lançar a partir de um dia de ponto (férias têm tela própria).
TIPOS_DIA = {k: v for k, v in TIPOS_AUSENCIA.items() if k != "ferias"}


def periodo(db: Session, e, de: str, ate: str) -> tuple[date, date]:
    """Padrão: o período em aberto (desde o último fechamento). No máximo 62 dias."""
    ini_p, fim_p = apuracao.periodo_aberto(db, e)
    fim = data_iso(ate, fim_p)
    ini = data_iso(de, ini_p)
    if ini > fim:
        ini = fim
    if (fim - ini).days > 62:
        ini = fim - timedelta(days=62)
    return ini, fim


def endereco(ini: date, fim: date, uid: int | None = None) -> str:
    q = {"de": ini.isoformat(), "ate": fim.isoformat()}
    if uid:
        q["func"] = uid
    return "/painel/ponto/ajuste?" + urlencode(q)


def _volta(volta: str) -> str:
    return volta if volta.startswith("/painel/ponto/ajuste") and "//" not in volta else "/painel/ponto/ajuste"


def _dia(dia: str) -> date | None:
    return data_iso(dia, None)


# ------------------------------------------------------------------ página
@rota.get("/painel/ponto/ajuste")
def pagina(request: Request, de: str = "", ate: str = "", fid: int = Query(0, alias="func"),
           u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    ini, fim = periodo(db, e, de, ate)
    resultado = apuracao.apurar(db, e, ini, fim)
    atual = next((r for r in resultado if r["u"].id == fid), None)
    if atual is None and resultado and not fid:
        atual = resultado[0]
    return render(request, "painel_apropriacao.html", u=u, e=e, aba="ponto", ini=ini, fim=fim,
                  resultado=resultado, atual=atual, ACOES=ACOES,
                  volta=endereco(ini, fim, atual["u"].id if atual else None),
                  fechado_ate=apuracao.fechado_ate(db, e), hoje=regras.hoje(e))


# ------------------------------------------------------------------ ações (janela)
@rota.get("/painel/ponto/ajuste/{uid}/{dia}/{acao}")
def acao_tela(request: Request, uid: int, dia: str, acao: str, volta: str = "",
              u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, uid, u)
    d = _dia(dia)
    if not d or acao not in ACOES:
        return ir("/painel/ponto/ajuste")
    if acao == "escala":
        return ir(f"/painel/escala?semana={d.isoformat()}&dia={d.isoformat()}")
    e = u.empresa
    ctx = dict(u=u, e=e, aba="ponto", f=f, d=d, acao=acao, titulo=ACOES[acao],
               volta=_volta(volta or endereco(d, d, f.id)),
               fechado=apuracao.esta_fechado(db, e, d), turno=db.scalar(select(Turno).where(
                   Turno.usuario_id == f.id, Turno.data == d)))
    if acao == "originais":
        ctx["batidas"] = list(db.scalars(select(Batida).where(
            Batida.usuario_id == f.id, Batida.dia == d).order_by(Batida.momento)))
        ctx["pedidos"] = list(db.scalars(select(Ajuste).where(
            Ajuste.usuario_id == f.id, Ajuste.data == d).order_by(Ajuste.criado_em)))
    elif acao == "historico":
        ctx["itens"] = list(db.scalars(select(Historico).where(
            Historico.empresa_id == e.id,
            or_(Historico.dia == d, Historico.dia.is_(None)),
            or_(Historico.usuario_id == f.id, Historico.usuario_id.is_(None)))
            .order_by(Historico.criado_em.desc()).limit(400)))
        # Registros antigos não guardavam pessoa e dia: procura pelo nome e pela data no texto.
        ctx["itens"] = [h for h in ctx["itens"] if h.dia == d and h.usuario_id == f.id
                        or (h.usuario_id is None and f.nome in h.detalhe
                            and f"{d:%d/%m/%Y}" in h.detalhe)][:60]
    elif acao == "preencher":
        ctx["plano"] = plano_preenchimento(db, f, d)
    elif acao == "motivo":
        ctx["MOTIVOS"] = MOTIVOS
        ctx["motivos"] = list(db.scalars(select(MotivoAjuste).where(
            MotivoAjuste.usuario_id == f.id, MotivoAjuste.dia == d).order_by(MotivoAjuste.criado_em)))
    elif acao == "ausencia":
        ctx["TIPOS"] = TIPOS_DIA
        ctx["ausencias"] = list(db.scalars(select(Ausencia).where(
            Ausencia.usuario_id == f.id, Ausencia.inicio <= d, Ausencia.fim >= d)))
    elif acao == "sobreaviso":
        ctx["sobreavisos"] = list(db.scalars(select(Sobreaviso).where(
            Sobreaviso.usuario_id == f.id, Sobreaviso.dia == d).order_by(Sobreaviso.inicio)))
    return render(request, "painel_apropriacao_acao.html", **ctx)


# ------------------------------------------------------------------ preenchimento automático
def plano_preenchimento(db: Session, f: Usuario, d: date) -> dict:
    """Batidas que faltam no dia, tiradas da escala. Só completa: nunca mexe nas que existem."""
    t = db.scalar(select(Turno).where(Turno.usuario_id == f.id, Turno.data == d))
    if not t or t.folga or not t.inicio or not t.fim:
        return dict(erro="Este dia não tem horário na escala (ou é folga). Defina o horário na Escala antes.")
    ini, fim = regras.intervalo(t)
    bats = list(db.scalars(select(Batida).where(Batida.usuario_id == f.id, Batida.dia == d, VALIDA)
                           .order_by(Batida.momento)))
    novas: list[tuple[str, datetime]] = []
    if not bats:
        novas.append(("entrada", ini))
        sai_int = regras.inicio_do_intervalo(t)
        if t.intervalo_min and sai_int and ini < sai_int < fim:
            volta_int = sai_int + timedelta(minutes=t.intervalo_min)
            if volta_int < fim:
                novas += [("saida", sai_int), ("entrada", volta_int)]
        novas.append(("saida", fim))
    else:
        if bats[0].tipo == "saida":
            novas.append(("entrada", ini))
        if bats[-1].tipo == "entrada":
            novas.append(("saida", fim))
    agora_l = regras.agora_local(f.empresa)
    futuras = [n for n in novas if n[1] > agora_l]
    novas = [n for n in novas if n[1] <= agora_l]
    if not novas:
        return dict(erro="Nada a preencher: o dia já tem entrada e saída."
                    if not futuras else "O horário da escala ainda não passou.")
    return dict(novas=novas, turno=t)


@rota.post("/painel/ponto/ajuste/{uid}/{dia}/preencher", dependencies=CSRF)
def preencher(request: Request, uid: int, dia: str, motivo: str = Form(""), volta: str = Form(""),
              u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, uid, u)
    d, destino = _dia(dia), _volta(volta)
    motivo = motivo.strip()[:150]
    if not d:
        return ir(destino)
    if len(motivo) < 3:
        avisar(request, "Informe o motivo do preenchimento: ele fica gravado em cada batida.", "erro")
        return ir(destino)
    if apuracao.esta_fechado(db, u.empresa, d):
        avisar(request, MSG_FECHADO, "erro")
        return ir(destino)
    plano = plano_preenchimento(db, f, d)
    if plano.get("erro"):
        avisar(request, plano["erro"], "erro")
        return ir(destino)
    for tipo, quando in plano["novas"]:
        regras.lancar_manual(db, f, tipo, quando, f"Preenchimento automático: {motivo}")
    horarios = ", ".join(f"{'entrada' if t == 'entrada' else 'saída'} {q:%H:%M}" for t, q in plano["novas"])
    regras.registrar(db, u.empresa_id, u.nome, "preenchimento automático",
                     f"Preencheu o ponto de {f.nome} em {d:%d/%m/%Y} pela escala ({horarios}). "
                     f"Motivo: {motivo}", usuario_id=f.id, dia=d)
    db.commit()
    avisar(request, f"Ponto de {f.primeiro_nome} preenchido pela escala: {horarios}.")
    return ir(destino)


# ------------------------------------------------------------------ motivo de ajuste
@rota.post("/painel/ponto/ajuste/{uid}/{dia}/motivo", dependencies=CSRF)
def motivo_novo(request: Request, uid: int, dia: str, motivo: str = Form(""), obs: str = Form(""),
                abonar: str = Form(""), volta: str = Form(""),
                u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, uid, u)
    d, destino = _dia(dia), _volta(volta)
    if not d or motivo not in MOTIVOS:
        avisar(request, "Escolha o motivo do ajuste.", "erro")
        return ir(destino)
    if motivo == "Outro" and len(obs.strip()) < 3:
        avisar(request, "Em \"Outro\", escreva o motivo na observação.", "erro")
        return ir(destino)
    if apuracao.esta_fechado(db, u.empresa, d):
        avisar(request, MSG_FECHADO, "erro")
        return ir(destino)
    db.add(MotivoAjuste(empresa_id=u.empresa_id, usuario_id=f.id, dia=d, motivo=motivo,
                        obs=obs.strip()[:300], criado_por=u.nome, criado_em=banco.agora()))
    texto = f"Motivo de ajuste em {d:%d/%m/%Y} para {f.nome}: {motivo}" + (f" ({obs.strip()})" if obs.strip() else "")
    if abonar:
        ja = db.scalar(select(Ausencia.id).where(Ausencia.usuario_id == f.id, Ausencia.inicio <= d,
                                                 Ausencia.fim >= d, Ausencia.situacao == "aprovado"))
        if not ja:
            db.add(Ausencia(empresa_id=u.empresa_id, usuario_id=f.id, tipo="abono", inicio=d, fim=d,
                            obs=f"{motivo}. {obs.strip()}".strip()[:300], situacao="aprovado",
                            criado_por=u.nome, decidido_por=u.nome, criado_em=banco.agora()))
            texto += ". Dia abonado"
    regras.registrar(db, u.empresa_id, u.nome, "motivo de ajuste", texto + ".", usuario_id=f.id, dia=d)
    db.commit()
    avisar(request, "Motivo de ajuste lançado." + (" O dia foi abonado." if abonar else ""))
    return ir(destino)


@rota.post("/painel/ponto/ajuste/motivo/{mid}/remover", dependencies=CSRF)
def motivo_remover(request: Request, mid: int, volta: str = Form(""),
                   u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    m = da_empresa(db, MotivoAjuste, mid, u)
    destino = _volta(volta)
    if apuracao.esta_fechado(db, u.empresa, m.dia):
        avisar(request, MSG_FECHADO, "erro")
        return ir(destino)
    f = db.get(Usuario, m.usuario_id)
    regras.registrar(db, u.empresa_id, u.nome, "motivo removido",
                     f"Removeu o motivo \"{m.motivo}\" de {f.nome} em {m.dia:%d/%m/%Y}.",
                     usuario_id=m.usuario_id, dia=m.dia)
    db.delete(m)
    db.commit()
    avisar(request, "Motivo removido.")
    return ir(destino)


# ------------------------------------------------------------------ sobreaviso
def minutos_entre(inicio: str, fim: str) -> int:
    a, b = regras.hm(inicio), regras.hm(fim)
    m = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
    return m if m > 0 else m + 24 * 60


@rota.post("/painel/ponto/ajuste/{uid}/{dia}/sobreaviso", dependencies=CSRF)
def sobreaviso_novo(request: Request, uid: int, dia: str, inicio: str = Form(""), fim: str = Form(""),
                    obs: str = Form(""), volta: str = Form(""),
                    u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, uid, u)
    d, destino = _dia(dia), _volta(volta)
    if not d or not HORA_RE.match(inicio) or not HORA_RE.match(fim) or inicio == fim:
        avisar(request, "Informe o início e o fim do sobreaviso (HH:MM).", "erro")
        return ir(destino)
    if apuracao.esta_fechado(db, u.empresa, d):
        avisar(request, MSG_FECHADO, "erro")
        return ir(destino)
    if d > regras.hoje(u.empresa) + timedelta(days=62):
        avisar(request, "Lance o sobreaviso com no máximo 2 meses de antecedência.", "erro")
        return ir(destino)
    minutos = minutos_entre(inicio, fim)
    ja = sum(s.minutos for s in db.scalars(select(Sobreaviso).where(
        Sobreaviso.usuario_id == f.id, Sobreaviso.dia == d)))
    if ja + minutos > 24 * 60:
        avisar(request, "O sobreaviso passa de 24 horas neste dia (limite do art. 244, §2º da CLT).", "erro")
        return ir(destino)
    db.add(Sobreaviso(empresa_id=u.empresa_id, usuario_id=f.id, dia=d, inicio=inicio, fim=fim,
                      minutos=minutos, obs=obs.strip()[:200], criado_por=u.nome, criado_em=banco.agora()))
    regras.registrar(db, u.empresa_id, u.nome, "sobreaviso lançado",
                     f"Sobreaviso de {f.nome} em {d:%d/%m/%Y}, das {inicio} às {fim} "
                     f"({regras.fmt_min(minutos)}).", usuario_id=f.id, dia=d)
    db.commit()
    avisar(request, f"Sobreaviso de {regras.fmt_min(minutos)} lançado para {f.primeiro_nome}.")
    return ir(destino)


@rota.post("/painel/ponto/ajuste/sobreaviso/{sid}/remover", dependencies=CSRF)
def sobreaviso_remover(request: Request, sid: int, volta: str = Form(""),
                       u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    s = da_empresa(db, Sobreaviso, sid, u)
    destino = _volta(volta)
    if apuracao.esta_fechado(db, u.empresa, s.dia):
        avisar(request, MSG_FECHADO, "erro")
        return ir(destino)
    regras.registrar(db, u.empresa_id, u.nome, "sobreaviso removido",
                     f"Removeu o sobreaviso de {s.usuario.nome} em {s.dia:%d/%m/%Y} "
                     f"({s.inicio}–{s.fim}).", usuario_id=s.usuario_id, dia=s.dia)
    db.delete(s)
    db.commit()
    avisar(request, "Sobreaviso removido.")
    return ir(destino)
