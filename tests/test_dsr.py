"""DSR automático: desconto na semana com falta e reflexo das horas extras.
Rodar da raiz: .venv/Scripts/python tests/test_dsr.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/teste.db".replace("\\", "/")
os.environ["SECRET_KEY"] = "teste"
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL", "ASAAS_API_KEY"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import apuracao  # noqa: E402
from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Empresa, Fechamento, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
FIM = SEG + timedelta(days=13)          # domingo 29/11: duas semanas inteiras
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def dia(n):
    return SEG + timedelta(days=n)


def as_(hora: str, d: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def config(g, tg, **extra):
    base = dict(nome="Bar A", raio_m="150", tolerancia_min="10", fuso="America/Sao_Paulo",
                ajuste_dias="7", fotos_dias="45", controle="extra", intervalo_padrao_min="0",
                tolerancia_dia_min="10", pct_extra_normal="50", pct_extra_especial="100",
                pct_noturno="20", dsr_faltas="1", dsr_extras="1")
    base.update(extra)
    assert posta(g, "/painel/config", tg, **base).status_code == 303


def apurado(nome):
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        return next(r for r in apuracao.apurar(db, e, SEG, FIM) if r["u"].nome.startswith(nome))


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar A", nome="Gil", email="gil@a.com",
          senha="segredo1")
    tg = csrf(g, "/painel/config")
    tela = g.get("/painel/config").text      # empresa nova já nasce com o DSR ligado
    assert 'name="dsr_faltas" value="1" checked' in tela and 'name="dsr_extras" value="1" checked' in tela
    config(g, tg)
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002"),
                      ("Carla Dias", "98999990003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
    for nome in ("Ana", "Bruno"):            # segunda a sexta, 08:00 às 16:00, toda semana
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=SEG.isoformat(),
              modo="trabalho", inicio="08:00", fim="16:00", intervalo="0", repetir="uteis", fixar="1")
        for n in (5, 6):                     # sábado e domingo marcados como Folga na escala
            posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=dia(n).isoformat(),
                  modo="folga", repetir="dia", fixar="1")
    posta(g, "/painel/feriados", tg, data=dia(4).isoformat(), nome="Consciência Negra")  # sexta

    def trabalha(nome, n, saida="16:00", entrada="08:00"):
        for tipo, hora in (("entrada", entrada), ("saida", saida)):
            posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo,
                  data=dia(n).isoformat(), hora=hora, obs="teste")

    as_("08:00", FIM + timedelta(days=1))
    # Ana: 2 h extras na 1ª segunda; falta na terça (semana 1) e na quarta e quinta (semana 2)
    trabalha("Ana", 0, saida="18:00")
    for n in (2, 3, 7, 8, 11):
        trabalha("Ana", n)
    # Bruno: não falta; trabalha 4 h no feriado; atestado na quarta da semana 2
    for n in (0, 1, 2, 3, 7, 8, 10, 11):
        trabalha("Bruno", n)
    trabalha("Bruno", 4, saida="12:00")
    posta(g, "/painel/ausencias", tg, usuario_id=ids["Bruno"], tipo="atestado",
          inicio=dia(9).isoformat(), fim=dia(9).isoformat())

    h = regras.fmt_min
    a, b = apurado("Ana"), apurado("Bruno")
    # descanso = as 4 folgas da escala (sáb. e dom.) + o feriado; os outros 9 dias são úteis
    assert (a["dias_uteis"], a["dias_descanso"]) == (9, 5)
    # Ana: 3 faltas em 2 semanas -> perde 2 descansos (8 h cada)
    assert (a["faltas"], h(a["falta_min"]), a["dsr_perdidos"], h(a["dsr_perdido_min"])) == (
        3, "24h00", 2, "16h00"), a
    # 2 h a 50% = 3 h; 3 h ÷ 9 dias úteis × 5 descansos = 1h40
    assert (h(a["extra_normal"]), h(a["dsr_extras"])) == ("2h00", "1h40")
    # Bruno: atestado não tira o DSR; 4 h no feriado a 100% = 8 h; 8 h ÷ 9 × 5 = 4h27
    assert (b["faltas"], b["dsr_perdidos"], b["dias_ausencia"]) == (0, 0, 1)
    assert (h(b["extra_especial"]), h(b["dsr_extras"])) == ("4h00", "4h27")

    # ---- Carla trabalha num sábado sem escala: não vira extra sozinho, fica pendente
    trabalha("Carla", 5)
    c = apurado("Carla")
    assert (c["sem_escala"], h(c["sem_escala_min"]), h(c["trabalhado"])) == (1, "8h00", "8h00")
    assert c["extra_normal"] == c["extra_especial"] == c["debito"] == 0
    assert "trabalhou sem escala: defina o dia na Escala" in c["dias"][5]["obs"]
    assert "1 dia(s) trabalhado(s) sem escala" in g.get(f"/painel/fechamento?de={SEG}&ate={FIM}").text
    # o gestor marca o dia como Folga: vira extra de folga (100%)
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Carla"], data=dia(5).isoformat(),
          modo="folga", repetir="dia")
    c = apurado("Carla")
    assert (c["sem_escala"], h(c["extra_especial"])) == (0, "8h00")
    # ou põe o horário na escala: é um sábado normal, sem extra
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Carla"], data=dia(5).isoformat(),
          modo="trabalho", inicio="08:00", fim="16:00", intervalo="0", repetir="dia")
    c = apurado("Carla")
    assert (c["sem_escala"], c["extra_normal"], c["extra_especial"], h(c["previsto"])) == (0, 0, 0, "8h00")
    assert "sem escala" not in g.get(f"/painel/fechamento?de={SEG}&ate={FIM}").text

    tela = g.get(f"/painel/fechamento?de={SEG}&ate={FIM}").text
    assert "<th>DSR</th>" in tela and "−2 dia(s)" in tela and "+1h40 s/ extras" in tela and "+4h27 s/ extras" in tela
    resumo = g.get(f"/painel/fechamento/resumo?de={SEG}&ate={FIM}").text
    for trecho in ("DSR descontado", "2 dia(s) · 16h00", "DSR sobre extras", "1h40", "4h27", "6h07",
                   "÷ dias úteis × folgas da escala e feriados"):
        assert trecho in resumo, trecho
    folha = g.get(f"/painel/folha?func={ids['Ana']}&de={SEG}&ate={FIM}").text
    assert "DSR descontado (semana com falta)" in folha and "DSR sobre horas extras" in folha

    # ---- banco de horas: sem reflexo das extras; o desconto por falta continua
    config(g, tg, controle="banco")
    a = apurado("Ana")
    assert (a["dsr_extras"], a["dsr_perdidos"]) == (0, 2)
    assert "só é calculado no controle por hora extra" in g.get(f"/painel/fechamento?de={SEG}&ate={FIM}").text
    # ---- desligado: nada é aplicado e as colunas somem do resumo
    config(g, tg, dsr_faltas="", dsr_extras="")
    a = apurado("Ana")
    assert (a["dsr_extras"], a["dsr_perdidos"], a["dsr_perdido_min"]) == (0, 0, 0)
    resumo = g.get(f"/painel/fechamento/resumo?de={SEG}&ate={FIM}").text
    assert "DSR descontado" not in resumo and "DSR sobre extras" not in resumo
    assert "Faltas em dias" in resumo
    config(g, tg)

    # ---- o fechamento guarda os valores e a planilha leva as colunas
    posta(g, "/painel/fechamento", tg, de=SEG.isoformat(), ate=FIM.isoformat())
    with SessionLocal() as db:
        f = db.scalar(select(Fechamento))
        linhas = {l.usuario.nome.split()[0]: (l.dsr_perdidos, l.dsr_perdido_min, l.dsr_extras)
                  for l in f.linhas}
        fid = f.id
    assert linhas == {"Ana": (2, 960, 100), "Bruno": (0, 0, 267), "Carla": (0, 0, 0)}, linhas
    planilha = g.get(f"/painel/fechamento/{fid}/planilha.csv").text
    assert "DSR descontado (dias);DSR descontado (horas);DSR sobre horas extras (em horas normais)" in planilha
    assert "não assinada;2;16h00;1h40" in planilha and "não assinada;0;0h00;4h27" in planilha
    assert "DSR:" in g.get("/painel/historico").text
    print("OK: DSR passou")


if __name__ == "__main__":
    main()
