"""Confirmação de turno, sugestão de quem escalar e trava da escala.

- Confirmação: com a regra ligada, o funcionário confirma no app cada turno de uma semana já
  publicada ("Vou estar lá"). X horas antes do início, quem não confirmou recebe um lembrete e o
  gestor um aviso; se a empresa quiser, na metade desse prazo o turno é oferecido à equipe (continua
  da pessoa até alguém pegar e o gestor aprovar). Mudar o turno pede uma nova confirmação.
- Sugestão: para um dia e horário, ordena quem pode trabalhar: sem turno no dia, sem ausência,
  dentro da disponibilidade, sem passar dos limites de menor, sem hora extra e com menos horas.
- Trava: dias passados há mais de N dias não podem mais ser mudados na escala."""
import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import apuracao, custos, push, regras
from . import db as banco
from .core import HORA_RE, avisar, data_iso, exigir_csrf, gestor_logado, ir, pode, usuario_logado
from .db import Alerta, EscalaPublicada, Oferta, Turno, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]


def semana_publicada(db: Session, e, d: date) -> bool:
    seg = d - timedelta(days=d.weekday())
    pub = db.scalar(select(EscalaPublicada).where(EscalaPublicada.empresa_id == e.id, EscalaPublicada.semana == seg))
    return bool(pub and pub.publicado_em)


def precisa_confirmar(db: Session, e, t: Turno) -> bool:
    if not e.confirmar_turno or not t or t.folga or t.confirmado_em or not t.inicio:
        return False
    if regras.intervalo(t)[0] <= regras.agora_local(e):
        return False
    return semana_publicada(db, e, t.data)


@rota.post("/app/turnos/{tid}/confirmar", dependencies=CSRF)
def confirmar(request: Request, tid: int, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    t = db.get(Turno, tid)
    if not t or t.usuario_id != u.id or not precisa_confirmar(db, u.empresa, t):
        return ir("/app/escala")
    t.confirmado_em = banco.agora()
    db.commit()
    avisar(request, "Turno confirmado. Obrigado!")
    return ir("/app/escala")


@rota.post("/app/escala/confirmar-todos", dependencies=CSRF)
def confirmar_todos(request: Request, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    hoje = regras.hoje(e)
    n = 0
    for t in db.scalars(select(Turno).where(Turno.usuario_id == u.id, Turno.data.between(hoje, hoje + timedelta(days=13)))):
        if precisa_confirmar(db, e, t):
            t.confirmado_em = banco.agora()
            n += 1
    db.commit()
    avisar(request, f"{n} turno(s) confirmado(s). Obrigado!" if n else "Nenhum turno para confirmar.")
    return ir("/app/escala")


def verificar(db: Session) -> tuple[list, list]:
    """A cada minuto: lembrete ao funcionário e aviso ao gestor quando faltam X horas e o turno não
    foi confirmado; oferta automática à equipe na metade do prazo (se ligada)."""
    from .core import alertas_liberados
    from .db import Empresa
    from .trocas import ATIVAS
    novos, lembretes = [], []
    agora = banco.agora()
    for e in db.scalars(select(Empresa).where(Empresa.ativo.is_(True), Empresa.confirmar_turno.is_(True))):
        al = regras.agora_local(e)
        hoje = regras.hoje(e)
        antes = timedelta(hours=max(1, e.confirmar_antes_h or 24))
        for t in db.scalars(select(Turno).join(Usuario).where(
                Turno.empresa_id == e.id, Turno.folga.is_(False), Turno.confirmado_em.is_(None),
                Usuario.ativo.is_(True), Turno.data.between(hoje, hoje + timedelta(days=3)))):
            if not t.inicio or not semana_publicada(db, e, t.data):
                continue
            ini = regras.intervalo(t)[0]
            if not (al < ini <= al + antes):
                continue
            if not t.lembrou_conf:
                t.lembrou_conf = True
                lembretes.append((t.usuario_id, "Confirme o seu turno",
                                  f"{t.data:%d/%m}, {t.inicio}–{t.fim}. Toque para confirmar que vai.", "/app/escala"))
                if alertas_liberados(e):
                    a = Alerta(empresa_id=e.id, usuario_id=t.usuario_id, tipo="sem_confirmacao", turno_id=t.id,
                               criado_em=agora,
                               texto=f"{t.usuario.nome} ainda não confirmou o turno de {t.data:%d/%m}, {t.inicio}–{t.fim}.")
                    db.add(a)
                    novos.append(a)
            if e.oferecer_sem_confirmacao and ini - al <= antes / 2 and not db.scalar(
                    select(Oferta.id).where(Oferta.turno_id == t.id, Oferta.situacao.in_(ATIVAS)).limit(1)):
                u = t.usuario
                db.add(Oferta(empresa_id=e.id, tipo="repasse", turno_id=t.id, de_id=u.id, data=t.data,
                              inicio=t.inicio, fim=t.fim, intervalo_min=t.intervalo_min,
                              intervalo_inicio=t.intervalo_inicio, local_id=t.local_id,
                              cargo=(u.cargo or "").strip()[:80], obs="Turno não confirmado", criado_por="Sistema"))
                regras.registrar(db, e.id, "Sistema", "turno não confirmado oferecido",
                                 f"{u.nome}, {t.data:%d/%m} {t.inicio}–{t.fim}.")
                for f in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                          Usuario.na_escala.is_(True), Usuario.papel == "funcionario",
                                                          Usuario.id != u.id)):
                    lembretes.append((f.id, "Turno disponível",
                                      f"{t.data:%d/%m}, {t.inicio}–{t.fim}. Quer pegar?", "/app/escala#disponiveis"))
    db.commit()
    return novos, lembretes


# ------------------------------------------------------------------ quem pode cobrir
def sugestoes(db: Session, e, d: date, inicio: str, fim: str, cargo: str = "") -> list[dict]:
    from . import disponibilidade, menores
    from .db import Ausencia, Turno as T
    a = int(inicio[:2]) * 60 + int(inicio[3:])
    b = int(fim[:2]) * 60 + int(fim[3:])
    dur = (b - a) % 1440 or 1440
    ini_sem = apuracao.inicio_semana(e, d)
    pessoas = list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                    Usuario.na_escala.is_(True))))
    turnos_sem = list(db.scalars(select(T).where(T.empresa_id == e.id, T.data.between(ini_sem, ini_sem + timedelta(days=6)))))
    horas = {}
    no_dia = set()
    for t in turnos_sem:
        horas[t.usuario_id] = horas.get(t.usuario_id, 0) + regras.minutos_turno(t)
        if t.data == d and not t.folga:
            no_dia.add(t.usuario_id)
    ausentes = {x.usuario_id for x in db.scalars(select(Ausencia).where(
        Ausencia.empresa_id == e.id, Ausencia.situacao == "aprovado", Ausencia.inicio <= d, Ausencia.fim >= d))}
    disp = disponibilidade.da_empresa(db, e.id)
    falso = type("T", (), {})()
    falso.folga, falso.inicio, falso.fim, falso.intervalo_min, falso.data = False, inicio, fim, 0, d
    feriados = menores.feriados_de(db, e, d - timedelta(days=1), d + timedelta(days=1))
    lista = []
    for u in pessoas:
        if u.id in no_dia or u.id in ausentes:
            continue
        motivos, pontos = [], 0
        dp = disp.get((u.id, d.weekday()))
        if disponibilidade.fora(falso, dp):
            motivos.append("fora da disponibilidade")
            pontos += 100
        r, faixa = menores.regra(e, u, d)
        if faixa == "<14" or (r and menores.problemas_do_turno(r, falso, feriados)):
            motivos.append("passa do limite de menor")
            pontos += 200
        total = horas.get(u.id, 0) + dur
        extra = 0 if (u.classificacao or "clt") == "pj" else max(0, total - apuracao.SEMANA_CLT_MIN)
        if extra:
            motivos.append(f"{regras.fmt_min(extra)} de hora extra")
            pontos += 50
        if cargo and (u.cargo or "").strip().lower() != cargo.strip().lower():
            pontos += 20
        custo = round(dur * custos.valor_hora(u) / 60 * (1 if not extra else 1 + (e.pct_extra_normal or 50) / 100 * extra / dur))
        lista.append({"id": u.id, "nome": u.nome, "cargo": u.cargo or "", "horas": regras.fmt_min(horas.get(u.id, 0)),
                      "custo": custo, "motivos": motivos, "ok": not motivos, "_p": (pontos, horas.get(u.id, 0), custo)})
    lista.sort(key=lambda x: x["_p"])
    for x in lista:
        x.pop("_p")
    return lista


@rota.get("/painel/escala/sugestoes")
def sugestoes_rota(data: str = "", inicio: str = "", fim: str = "", cargo: str = "",
                   u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    from .i18n import _
    d = data_iso(data, None)
    if not d or not (HORA_RE.match(inicio) and HORA_RE.match(fim)) or inicio == fim:
        return JSONResponse({"erro": _("Informe o dia e o horário.")}, 400)
    lista = sugestoes(db, u.empresa, d, inicio, fim, cargo)
    ver_custo = pode(u, "pagamento")
    for x in lista:
        x["motivos"] = [_(m) for m in x["motivos"]]
        if not ver_custo:
            x["custo"] = None
    return {"pessoas": lista[:12]}


# ------------------------------------------------------------------ configurações
@rota.post("/painel/config/escala-regras", dependencies=CSRF)
def regras_escala(request: Request, confirmar_turno: str = Form(""), confirmar_antes_h: str = Form("24"),
                  oferecer: str = Form(""), travar: str = Form("0"),
                  u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    h, tr = confirmar_antes_h.strip(), travar.strip() or "0"
    if not (h.isdigit() and 1 <= int(h) <= 168 and tr.isdigit() and int(tr) <= 60):
        avisar(request, "Use de 1 a 168 horas para a confirmação e de 0 a 60 dias para a trava.", "erro")
        return ir("/painel/config#escala-regras")
    e.confirmar_turno, e.confirmar_antes_h = bool(confirmar_turno), int(h)
    e.oferecer_sem_confirmacao = bool(oferecer) and bool(confirmar_turno)
    e.travar_escala_dias = int(tr)
    regras.registrar(db, e.id, u.nome, "regras da escala",
                     f"Confirmação {'ligada' if e.confirmar_turno else 'desligada'} ({e.confirmar_antes_h} h); "
                     f"oferecer sem confirmação: {'sim' if e.oferecer_sem_confirmacao else 'não'}; "
                     f"trava: {e.travar_escala_dias} dia(s).")
    db.commit()
    avisar(request, "Regras da escala salvas.")
    return ir("/painel/config#escala-regras")


def travado_ate(e) -> date | None:
    """Último dia travado (dias até essa data não podem mais ser mudados)."""
    if not e.travar_escala_dias:
        return None
    return regras.hoje(e) - timedelta(days=e.travar_escala_dias + 1)
