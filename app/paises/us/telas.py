"""Partes das telas que mudam para empresas dos EUA (as rotas continuam no app comum e
chamam estas funções quando a empresa é americana)."""
import csv
import io

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ... import regras
from ...core import avisar, data_iso
from ...db import Usuario, get_db
from . import apuracao, escala_justa, salario_minimo

FUSOS = [
    ("America/New_York", "Eastern (ET)"),
    ("America/Chicago", "Central (CT)"),
    ("America/Denver", "Mountain (MT)"),
    ("America/Phoenix", "Arizona (MST, no DST)"),
    ("America/Los_Angeles", "Pacific (PT)"),
    ("America/Anchorage", "Alaska (AKT)"),
    ("Pacific/Honolulu", "Hawaii (HST)"),
]
FREQUENCIAS = ("weekly", "biweekly", "semimonthly", "monthly")


def so_eua(request: Request, db: Session = Depends(get_db)) -> None:
    """Rotas só dos EUA: empresa de outro país recebe 404. Sem login, a própria rota decide."""
    uid = request.session.get("uid")
    u = db.get(Usuario, uid) if uid else None
    if u and u.empresa and u.empresa.pais != "US":
        raise HTTPException(404)


def contexto_config(db: Session, e) -> dict:
    return dict(fusos=FUSOS, estados=apuracao.ESTADOS, regras_estado=apuracao.REGRAS_ESTADO,
                cidades=salario_minimo.cidades_do(e.estado),
                sm=salario_minimo.situacao(db, e, regras.hoje(e)),
                sm_atualizado=salario_minimo.ATUALIZADO_EM,
                fw_opcoes=escala_justa.da_empresa_opcoes(e.estado), FW=escala_justa.REGRAS,
                fw_regra=escala_justa.regra(e))


def _numero(s: str):
    try:
        return float((s or "").strip().replace(",", "."))
    except ValueError:
        return None


def salvar_jornada(request: Request, db: Session, e, u, f: dict) -> bool:
    """Regras da jornada americanas (estado, semana, período, refeição, diferenciais).
    Devolve False (com aviso) se algum valor for inválido; nada é gravado nesse caso."""
    ip_, td = _numero(f["intervalo_padrao_min"]), _numero(f["tolerancia_dia_min"])
    if ip_ is None or not 0 <= ip_ <= 240 or td is None or not 0 <= td <= 60:
        avisar(request, "Confira a jornada: refeição de 0 a 240 min e tolerância diária de 0 a 60 min.", "erro")
        return False
    pnot, pfer, si = _numero(f["pct_noturno"]), _numero(f["pct_feriado"]), _numero(f["semana_inicio"])
    if pnot is None or not 0 <= pnot <= 300 or pfer is None or not 0 <= pfer <= 300:
        avisar(request, "Confira os percentuais de diferencial noturno e de feriado: de 0 a 300.", "erro")
        return False
    if f["estado"] not in apuracao.ESTADOS or si is None or not 0 <= si <= 6 or f["frequencia"] not in FREQUENCIAS:
        avisar(request, "Confira o estado, o início da semana e a frequência de pagamento.", "erro")
        return False

    def jornada():
        return (e.estado, e.semana_inicio, e.frequencia, e.periodo_ancora, e.intervalo_padrao_min,
                e.intervalo_auto, e.tolerancia_dia_min, e.pct_noturno, e.pct_feriado,
                e.intervalo_obrigatorio)
    antes = jornada()
    apuracao.aplicar_estado(e, f["estado"])
    e.semana_inicio, e.frequencia = int(si), f["frequencia"]
    e.periodo_ancora = (data_iso(f["periodo_ancora"], None)
                        if f["frequencia"] in ("weekly", "biweekly") else None)
    e.intervalo_padrao_min, e.tolerancia_dia_min = int(ip_), int(td)
    e.intervalo_auto = bool(f["intervalo_auto"])
    e.pct_noturno, e.pct_feriado = int(pnot), int(pfer)
    e.intervalo_obrigatorio = bool(f["intervalo_obrigatorio"])
    if antes != jornada():
        regras.registrar(db, e.id, u.nome, "regras da jornada alteradas",
                         f"Estado {e.estado}; semana de trabalho começa em "
                         f"{regras.DIAS_SEMANA[e.semana_inicio]}; pagamento {e.frequencia}; "
                         f"refeição padrão {e.intervalo_padrao_min} min "
                         f"({'desconta sozinho' if e.intervalo_auto else 'não desconta sozinho'}); "
                         f"tolerância diária {e.tolerancia_dia_min} min; diferencial noturno "
                         f"{e.pct_noturno}%; prêmio de feriado {e.pct_feriado}%; refeição "
                         f"{'precisa ser batida' if e.intervalo_obrigatorio else 'não precisa ser batida'}.")
    return True


def planilha_fechamento(u, f) -> Response:
    """Horas do período para o provedor de folha (Gusto, ADP, QuickBooks...): em horas decimais."""
    saida = io.StringIO()
    w = csv.writer(saida)
    w.writerow(["Employee", "Job title", "Period start", "Period end", "Hourly rate",
                "Regular hours", "Overtime hours (1.5x)", "Double time hours (2x)",
                "Meal premium hours", "Rest premium hours", "Predictability pay hours",
                "Predictability pay fixed ($)", "Paid leave hours (PTO/sick)", "Holiday hours worked",
                "Night hours", "Scheduled hours", "No-shows", "Late/early minutes",
                "Timesheet signed"])
    hd = lambda m: f"{m / 60:.2f}"  # noqa: E731
    for l in sorted(f.linhas, key=lambda l: l.usuario.nome.lower()):
        p = l.usuario
        w.writerow([p.nome, p.cargo, f"{f.inicio:%m/%d/%Y}", f"{f.fim:%m/%d/%Y}",
                    f"{p.taxa_hora_centavos / 100:.2f}", hd(l.regular), hd(l.ot), hd(l.dt),
                    hd(l.meal_premios), hd(l.descanso_premios), hd(l.previsibilidade_min),
                    f"{l.previsibilidade_centavos / 100:.2f}", hd(l.pto_min), hd(l.feriado_min), hd(l.noturno),
                    hd(l.previsto), l.faltas, l.debito,
                    regras.para_local(u.empresa, l.aceite_em).strftime("%m/%d/%Y %I:%M %p")
                    if l.aceite_em else "no"])
    return Response(saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="timesheets-{f.inicio}-to-{f.fim}.csv"'})
