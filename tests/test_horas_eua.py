"""Regras de hora extra dos EUA dentro da camada de países: semana de 40 h (FLSA), Califórnia
(dia, dobro, 7º dia, meal premium), períodos de pagamento, feriados e licença remunerada.
Rodar da raiz: python tests/test_horas_eua.py"""
import os
import re
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as N

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("ANTHROPIC_API_KEY", "ADMIN_EMAIL", "ASAAS_API_KEY", "PUBLIC_URL"):
    os.environ.pop(k, None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import apuracao as apuracao_br  # noqa: E402
from app import db as banco  # noqa: E402
from app import paises, regras  # noqa: E402
from app.db import Ausencia, Batida, Empresa, SessionLocal, Turno, Usuario  # noqa: E402
from app.main import app  # noqa: E402
from app.paises import br, us  # noqa: E402
from app.paises.us import apuracao  # noqa: E402

DOM = date(2026, 11, 15)          # semana de trabalho: domingo 15 a sábado 21
FUSO = 5                          # America/New_York em novembro: UTC-5
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=FUSO)


def unidades():
    fed = N(ot_dia_min=0, dt_dia_min=0, ot_semana_min=2400, setimo_dia=False)
    ca = N(ot_dia_min=480, dt_dia_min=720, ot_semana_min=2400, setimo_dia=True)
    d = apuracao.dividir_dia
    assert d(fed, 600, False, 0) == (600, 0, 0)              # federal: 10 h no dia = normal
    assert d(fed, 600, False, 2100) == (300, 300, 0)         # passou das 40 h na semana
    assert d(ca, 600, False, 0) == (480, 120, 0)             # CA: acima de 8 h
    assert d(ca, 780, False, 0) == (480, 240, 60)            # CA: acima de 12 h em dobro
    assert d(ca, 480, False, 2160) == (240, 240, 0)          # CA: semana passa de 40 h normais
    assert d(ca, 600, True, 2400) == (0, 480, 120)           # CA: 7º dia seguido

    def b(h, m):
        return N(momento=datetime(2026, 1, 1, h, m))
    certo = [(b(8, 0), b(12, 30)), (b(13, 0), b(17, 0))]
    tarde = [(b(8, 0), b(13, 30)), (b(14, 0), b(17, 0))]
    curta = [(b(8, 0), b(12, 0)), (b(12, 20), b(17, 0))]
    assert not apuracao.falta_refeicao(certo, 510)
    assert apuracao.falta_refeicao(tarde, 510) and apuracao.falta_refeicao(curta, 520)
    assert apuracao.falta_refeicao([(b(8, 0), b(14, 0))], 360)
    assert not apuracao.falta_refeicao([(b(8, 0), b(13, 0))], 300)
    e = N(frequencia="biweekly", periodo_ancora=date(2026, 11, 1), semana_inicio=6)
    assert apuracao.periodo_de(e, date(2026, 11, 20)) == (date(2026, 11, 15), date(2026, 11, 28))
    e.frequencia = "semimonthly"
    assert apuracao.periodo_de(e, date(2026, 2, 20)) == (date(2026, 2, 16), date(2026, 2, 28))
    e.frequencia = "weekly"
    assert apuracao.periodo_de(e, date(2026, 11, 21)) == (date(2026, 11, 15), date(2026, 11, 21))
    assert us.inicio_semana(e, date(2026, 11, 18)) == DOM

    # feriados e calendário por país, sem misturar
    nomes_us = [n for _d, n in us.feriados(2026)]
    assert "Thanksgiving Day" in nomes_us and "Tiradentes" not in nomes_us
    assert (date(2026, 11, 26), "Thanksgiving Day") in us.feriados(2026)
    assert (date(2026, 7, 3), "Independence Day (observed)") in us.facultativos(2026)  # 4/7 cai no sábado
    assert any(n == "Black Friday" and d == date(2026, 11, 27) for d, n in us.comemorativas(2026))
    assert br.feriados(2026) == apuracao_br.feriados_nacionais(2026)
    assert "Thanksgiving Day" not in [n for _d, n in br.feriados(2026)]
    assert (us.MOEDA, us.COBRANCA, br.MOEDA) == ("USD", "stripe", "BRL")


def main():
    unidades()
    as_("07:00", DOM)
    g = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', g.get("/cadastro").text).group(1)
    g.post("/cadastro", data={"csrf": tok, "aceito": "1", "empresa": "Taco Bar", "nome": "Gil",
                              "email": "gil@t.com", "senha": "segredo1"})
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Taco Bar"))
        assert e.pais == "BR"
        e.pais, e.fuso = "US", "America/New_York"
        apuracao.aplicar_estado(e, "CA")
        e.frequencia, e.periodo_ancora, e.semana_inicio = "weekly", DOM, 6
        e.intervalo_obrigatorio = True
        ana = Usuario(empresa_id=e.id, nome="Ana Souza", papel="funcionario")
        db.add(ana)
        db.commit()
        eid, aid = e.id, ana.id
        assert paises.regras_de(e) is us
        assert (e.estado, e.ot_dia_min, e.dt_dia_min, e.setimo_dia, e.meal_premium) == ("CA", 480, 720, True, True)

    def trabalha(d, *horas):
        with SessionLocal() as db:
            for i, hora in enumerate(horas):
                h, m = map(int, hora.split(":"))
                db.add(Batida(empresa_id=eid, usuario_id=aid, tipo="entrada" if i % 2 == 0 else "saida",
                              momento=datetime(d.year, d.month, d.day, h, m) + timedelta(hours=FUSO), dia=d))
            db.commit()

    as_("08:00", DOM + timedelta(days=8))
    for n in range(7):
        trabalha(DOM + timedelta(days=n), "08:00", "12:30", "13:00", "17:30")
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        r = paises.regras_de(e).apurar(db, e, DOM, DOM + timedelta(days=6))[0]
    h = regras.fmt_min
    assert (h(r["trabalhado"]), h(r["regular"]), h(r["ot"]), h(r["dt"])) == ("63h00", "40h00", "22h00", "1h00"), r
    assert r["meal_premios"] == 0
    assert "7º dia seguido na semana" in r["dias"][6]["obs"]

    seg = DOM + timedelta(days=8)
    as_("20:00", seg)
    trabalha(seg, "09:00", "16:00")
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        r = paises.regras_de(e).apurar(db, e, seg, seg)[0]
    assert (r["regular"], r["ot"], r["meal_premios"]) == (420, 0, 60), r
    assert "sem refeição no prazo: 1 h de meal premium" in r["dias"][0]["obs"]

    # licença remunerada (sick leave) conta horas pagas, não horas trabalhadas
    ter = seg + timedelta(days=1)
    with SessionLocal() as db:
        db.add(Turno(empresa_id=eid, usuario_id=aid, data=ter, inicio="09:00",
                     fim="17:00", intervalo_min=30))
        db.add(Ausencia(empresa_id=eid, usuario_id=aid, tipo="atestado", inicio=ter, fim=ter,
                        situacao="aprovado"))
        db.commit()
    as_("20:00", ter)
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        r = paises.regras_de(e).apurar(db, e, ter, ter)[0]
    assert (r["pto_min"], r["trabalhado"], r["faltas"]) == (450, 0, 0), r

    # fechar o período grava as colunas americanas e não mexe nas brasileiras
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        f = paises.regras_de(e).fechar(db, e, DOM, DOM + timedelta(days=6), "Gil")
        db.commit()
        linha = f.linhas[0]
        assert (f.estado, linha.regular, linha.ot, linha.dt) == ("CA", 2400, 1320, 60)
        assert linha.extra_normal == 0 and linha.saldo_final == 0
    print("OK: horas dos EUA passou")


if __name__ == "__main__":
    main()
