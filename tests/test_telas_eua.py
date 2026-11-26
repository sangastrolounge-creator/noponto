"""Telas de uma empresa dos EUA na plataforma unificada: país trocado pelo Master, configuração
americana, fechamento com horas extras/dobro, planilha para o provedor de folha, reabertura com
PTO e rotas americanas fechadas para empresas do Brasil.
Rodar da raiz: python tests/test_telas_eua.py"""
import os
import re
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})
for k in ("ANTHROPIC_API_KEY", "ASAAS_API_KEY", "PUBLIC_URL"):
    os.environ.pop(k, None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app.core import dinheiro  # noqa: E402
from app.db import Batida, Empresa, Fechamento, MovimentoPTO, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

DOM = date(2026, 11, 15)          # domingo; semana de trabalho de domingo a sábado
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora, d, fuso=5):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=fuso)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def cadastra(empresa, nome, email):
    c = TestClient(app, follow_redirects=False)
    posta(c, "/cadastro", csrf(c, "/cadastro"), aceito="1", empresa=empresa, nome=nome, email=email,
          senha="segredo1")
    return c


def main():
    assert dinheiro(123456, "US") == "$1,234.56" and dinheiro(123456, "BR") == "R$ 1.234,56"
    as_("07:00", DOM, fuso=3)
    m = cadastra("Plataforma", "Dono", "dono@plataforma.com")
    br = cadastra("Bar do Brasil", "Bia", "bia@br.com")
    with SessionLocal() as db:
        bid = db.scalar(select(Empresa.id).where(Empresa.nome == "Bar do Brasil"))
    us = cadastra("Taco Bar", "Gil", "gil@us.com")
    with SessionLocal() as db:
        eid = db.scalar(select(Empresa.id).where(Empresa.nome == "Taco Bar"))
        dono_eid = db.scalar(select(Empresa.id).where(Empresa.nome == "Plataforma"))
    assert 'name="pais"' in m.get(f"/master/empresas/{eid}").text

    # a empresa do dono não muda de país; a do cliente vira EUA
    tm = csrf(m, "/master")
    posta(m, f"/master/empresas/{dono_eid}/dados", tm, plano="0", pais="US")
    posta(m, f"/master/empresas/{eid}/dados", tm, plano="0", isenta="1", pais="US")
    posta(m, f"/master/empresas/{bid}/dados", tm, plano="0", isenta="1")
    with SessionLocal() as db:
        assert db.get(Empresa, dono_eid).pais == "BR"
        e = db.get(Empresa, eid)
        assert (e.pais, e.estado, e.fuso) == ("US", "FL", "America/New_York")
        assert (e.intervalo_padrao_min, e.intervalo_auto, e.pct_noturno) == (30, False, 0)

    # configuração: a tela americana aparece só para a empresa americana
    tb = csrf(br, "/painel/config")
    tela_br = br.get("/painel/config").text
    assert "California ★" not in tela_br and "anco de horas" in tela_br
    tg = csrf(us, "/painel/config")
    tela = us.get("/painel/config").text
    assert 'value="FL" selected' in tela and "California ★" in tela and "Eastern (ET)" in tela
    assert "Banco de horas" not in tela
    r = posta(us, "/painel/config", tg, nome="Taco Bar", raio_m="150", tolerancia_min="10",
              fuso="America/New_York", ajuste_dias="7", fotos_dias="45", estado="CA",
              semana_inicio="6", frequencia="weekly", periodo_ancora=DOM.isoformat(),
              intervalo_padrao_min="30", tolerancia_dia_min="7", pct_noturno="0",
              pct_feriado="0", intervalo_obrigatorio="1")
    assert r.status_code == 303
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        assert (e.estado, e.ot_dia_min, e.dt_dia_min, e.setimo_dia, e.meal_premium) == ("CA", 480, 720, True, True)
        assert (e.frequencia, e.periodo_ancora, e.fuso) == ("weekly", DOM, "America/New_York")
        assert (e.controle, e.pct_extra_normal) == ("banco", 50)   # regras brasileiras intactas
    assert "Regras de CA em uso" in us.get("/painel/config").text

    # rota só dos EUA: a empresa brasileira recebe 404
    assert posta(br, "/painel/pto/regra", tb, sick_por_horas="30", sick_limite="40",
                 pto_por_horas="0", pto_limite="0").status_code == 404
    assert posta(us, "/painel/pto/regra", tg, sick_por_horas="30", sick_limite="40",
                 pto_por_horas="0", pto_limite="0").status_code == 303

    # uma semana com 7 dias seguidos de 9 h na Califórnia
    with SessionLocal() as db:
        ana = Usuario(empresa_id=eid, nome="Ana Souza", papel="funcionario", taxa_hora_centavos=2000)
        db.add(ana)
        db.flush()
        aid = ana.id
        for n in range(7):
            d = DOM + timedelta(days=n)
            for tipo, hm in (("entrada", "08:00"), ("saida", "12:30"), ("entrada", "13:00"), ("saida", "17:30")):
                h, mi = map(int, hm.split(":"))
                db.add(Batida(empresa_id=eid, usuario_id=aid, tipo=tipo, dia=d,
                              momento=datetime(d.year, d.month, d.day, h, mi) + timedelta(hours=5)))
        db.commit()
    as_("08:00", DOM + timedelta(days=8))
    sab = DOM + timedelta(days=6)
    tela = us.get(f"/painel/fechamento?de={DOM}&ate={sab}").text
    assert "Extra 1,5x" in tela and "Dobro 2x" in tela and "Meal premium" in tela
    assert "22h00" in tela and "40h00" in tela

    # fechar o período, baixar a planilha americana e reabrir
    assert posta(us, "/painel/fechamento", tg, de=DOM.isoformat(), ate=sab.isoformat()).status_code == 303
    with SessionLocal() as db:
        f = db.scalar(select(Fechamento).where(Fechamento.empresa_id == eid))
        linha = f.linhas[0]
        assert (f.estado, linha.regular, linha.ot, linha.dt) == ("CA", 2400, 1320, 60)
        fid = f.id
        ganho = db.scalar(select(MovimentoPTO).where(MovimentoPTO.fechamento_id == fid))
        assert ganho is not None and ganho.banco == "sick" and ganho.minutos > 0
    csv = us.get(f"/painel/fechamento/{fid}/planilha.csv")
    assert "Regular hours" in csv.text and "Ana Souza" in csv.text and "40.00,22.00,1.00" in csv.text
    assert "timesheets-" in csv.headers["content-disposition"]
    assert us.get(f"/painel/fechamento/resumo?de={DOM}&ate={sab}").status_code == 200
    assert us.get(f"/painel/fechamento/horas?de={DOM}&ate={sab}").status_code == 303
    assert posta(us, f"/painel/fechamento/{fid}/reabrir", tg).status_code == 303
    with SessionLocal() as db:
        assert db.get(Fechamento, fid) is None
        assert db.scalar(select(MovimentoPTO).where(MovimentoPTO.motivo == "acumulo")) is None

    # depois do primeiro fechamento o país não muda mais
    posta(us, "/painel/fechamento", tg, de=DOM.isoformat(), ate=sab.isoformat())
    posta(m, f"/master/empresas/{eid}/dados", tm, plano="0", isenta="1", pais="BR")
    with SessionLocal() as db:
        assert db.get(Empresa, eid).pais == "US"

    # ficha americana: valor por hora em dólar, ou salário anual (isento de hora extra)
    tela = us.get("/painel/equipe/novo").text
    assert "Por ano (salário)" in tela and "W-2" in tela and "CPF" not in tela
    posta(us, "/painel/equipe/novo", tg, nome="Bob", sobrenome="Smith", celular="3055550123",
          nivel="funcionario", classificacao="w2", funcao_0="Cook", valor_0="18.50", tipo_0="hora")
    posta(us, "/painel/equipe/novo", tg, nome="Carol", sobrenome="Lee", celular="3055550124",
          nivel="funcionario", classificacao="w2", funcao_0="Chef", valor_0="52000", tipo_0="anual")
    with SessionLocal() as db:
        bob = db.scalar(select(Usuario).where(Usuario.nome == "Bob Smith"))
        carol = db.scalar(select(Usuario).where(Usuario.nome == "Carol Lee"))
        assert (bob.taxa_hora_centavos, bob.isento, bob.cargo) == (1850, False, "Cook")
        assert (carol.isento, carol.salario_periodo_centavos) == (True, 100000)   # 52.000 / 52 semanas

    # pagamento bruto americano no mesmo endereço; o prefixo interno não abre direto
    assert us.get("/eua/painel/pagamento").status_code == 404
    tela = us.get("/painel/pagamento").text
    assert "Resumos de pagamento" in tela or "Gerar" in tela
    with SessionLocal() as db:
        fid = db.scalar(select(Fechamento.id).where(Fechamento.empresa_id == eid))
    r = posta(us, "/painel/pagamento/gerar", tg, fechamento_id=str(fid))
    assert r.status_code == 303, r.status_code
    tela = us.get(r.headers["location"]).text
    assert "Bruto total" in tela and "$1,500.00" in tela, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tela))[:2500]
    assert "INSS" not in tela and "R$" not in tela

    # demais telas da empresa americana
    tela = us.get("/painel/ausencias").text
    assert "Saldos de sick leave e PTO" in tela
    tela = us.get("/painel/conformidade").text
    assert "regras federais e as do seu estado" in tela and "Pausas de descanso (Califórnia)" in tela
    assert us.get("/eua/painel/conformidade").status_code == 404
    assert "CLT" not in us.get("/painel/conformidade").text
    assert us.get("/painel/ponto/problemas").status_code == 200
    assert us.get("/painel").status_code == 200
    assert us.get(f"/painel/escala?semana={DOM}").status_code == 200
    tela = us.get(f"/painel/fechamento?de={DOM}&ate={sab}").text
    assert "11/15" in tela or "11/21" in tela      # datas no formato americano
    # e as da brasileira continuam brasileiras
    tela = br.get("/painel/conformidade").text
    assert "regras federais e as do seu estado" not in tela
    assert br.get("/painel/ausencias").status_code == 200 and "Saldos de sick leave" not in br.get("/painel/ausencias").text
    assert br.get("/painel/ponto/problemas").status_code == 200

    # a empresa brasileira continua no Brasil, com as regras de lá
    with SessionLocal() as db:
        b = db.scalar(select(Empresa).where(Empresa.nome == "Bar do Brasil"))
        assert (b.pais, b.estado, b.controle) == ("BR", "", "banco")
    print("OK: telas dos EUA passou")


if __name__ == "__main__":
    main()
