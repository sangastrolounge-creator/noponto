"""Fase 2: jornada com intervalo, feriados, ausências, apuração, banco de horas, fechamento
e folha assinada. Rodar da raiz: .venv/Scripts/python tests/test_fase2.py"""
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
os.environ.pop("PUBLIC_URL", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import apuracao  # noqa: E402
from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import (Alerta, Ausencia, Batida, Empresa, Fechamento, FechamentoLinha,  # noqa: E402
                    Feriado, SessionLocal, Turno, Usuario)
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
TER, QUA, QUI, SEX, SAB = (SEG + timedelta(days=i) for i in range(1, 6))
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora: str, dia: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


LAT, LNG = -2.5307, -44.3068
JPEG = b"\xff\xd8\xff\xe0" + b"atestado" * 50


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, arquivos=None, **dados):
    return cli.post(caminho, data={"csrf": token, **dados}, files=arquivos)


def bate(cli, token, hora, dia):
    as_(hora, dia)
    r = cli.post("/app/ponto", json={"lat": LAT, "lng": LNG, "precisao": 10},
                 headers={"X-CSRF": token})
    assert r.status_code == 200, r.text
    return r.json()["tipo"]


def config(g, tg, **extra):
    base = dict(nome="Padaria A", lat=str(LAT), lng=str(LNG), raio_m="150", tolerancia_min="10",
                fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7", fotos_dias="45",
                controle="banco", intervalo_padrao_min="60", intervalo_auto="1",
                tolerancia_dia_min="10")
    base.update(extra)
    assert posta(g, "/painel/config", tg, **base).status_code == 303


def apurado(nome, ini=SEG, fim=SEX):
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        return next(r for r in apuracao.apurar(db, e, ini, fim) if r["u"].nome.startswith(nome))


def main():
    assert apuracao.pascoa(2026) == date(2026, 4, 5) and apuracao.pascoa(2027) == date(2027, 3, 28)
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Padaria A", nome="Gil Gestor",
          email="gil@a.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    config(g, tg)
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convites = {u.id: u.convite_token for u in db.scalars(select(Usuario)) if u.convite_token}
    cli = {}
    for nome in ("Ana", "Bruno"):
        c = novo()
        caminho = f"/convite/{convites[ids[nome]]}"
        assert posta(c, caminho, csrf(c, caminho), senha="senha123").status_code == 303
        cli[nome] = (c, csrf(c, "/app"))
    (ana, ta), (bruno, tb) = cli["Ana"], cli["Bruno"]

    # ---- escala com intervalo e feriados
    for nome in ("Ana", "Bruno"):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=SEG.isoformat(),
              modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="uteis")
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert "40h00 na semana" in grade and 'data-intervalo="60"' in grade
    posta(g, "/painel/feriados/nacionais", tg, ano="2026")
    posta(g, "/painel/feriados/nacionais", tg, ano="2026")  # não duplica
    with SessionLocal() as db:
        fer = {f.data: f.nome for f in db.scalars(select(Feriado))}
    assert len(fer) == 10 and fer[SEX] == "Consciência Negra" and date(2026, 4, 3) in fer
    posta(g, "/painel/feriados", tg, data="2026-09-08", nome="Aniversário de São Luís")
    tela = g.get("/painel/ausencias?ano=2026").text
    assert "Aniversário de São Luís" in tela and "Consciência Negra" in tela
    assert ">Feriado<" in g.get(f"/painel/escala?semana={SEG}").text

    # ---- segunda: Ana cumpre a jornada (intervalo descontado); Bruno faz 2h05 extras
    assert bate(ana, ta, "08:00", SEG) == "entrada" and bate(bruno, tb, "08:00", SEG) == "entrada"
    assert bate(ana, ta, "17:00", SEG) == "saida" and bate(bruno, tb, "19:05", SEG) == "saida"
    # ---- terça: Ana atrasa 20 min; Bruno falta
    bate(ana, ta, "08:20", TER)
    bate(ana, ta, "17:00", TER)
    # ---- quarta: Ana de atestado (lançado pelo gestor); Bruno bate o intervalo (5 min a mais: tolerância)
    r = posta(g, "/painel/ausencias", tg, usuario_id=ids["Ana"], tipo="atestado",
              inicio=QUA.isoformat(), fim=QUA.isoformat(), obs="gripe",
              arquivos={"anexo": ("atestado.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 303
    as_("08:30", QUA)
    with SessionLocal() as db:
        alertas, _ = regras.verificar_turnos(db)
        assert [a.usuario_id for a in alertas] == [ids["Bruno"]], [a.texto for a in alertas]
    painel = g.get("/painel").text
    assert "Atestado médico" in painel
    assert "Atestado" in g.get(f"/painel/escala?semana={SEG}").text
    for hora in ("08:35", "12:00", "13:00", "17:40"):
        bate(bruno, tb, hora, QUA)
    # ---- quinta: Ana normal; Bruno manda atestado pelo app e o gestor aceita
    bate(ana, ta, "08:00", QUI)
    bate(ana, ta, "17:00", QUI)
    as_("09:00", QUI)
    posta(bruno, "/app/ausencias", tb, tipo="atestado", inicio=QUI.isoformat(), fim=QUI.isoformat())
    assert "Anexe a foto" in bruno.get("/app/ajustes").text
    posta(bruno, "/app/ausencias", tb, tipo="atestado", inicio=QUI.isoformat(), fim=QUI.isoformat(),
          arquivos={"anexo": ("x.txt", b"nao e imagem", "text/plain")})
    assert "Envie uma foto" in bruno.get("/app/ajustes").text
    r = posta(bruno, "/app/ausencias", tb, tipo="atestado", inicio=QUI.isoformat(),
              fim=QUI.isoformat(), arquivos={"anexo": ("a.jpg", JPEG, "image/jpeg")})
    assert "Aguardando" in bruno.get("/app/ajustes").text
    with SessionLocal() as db:
        aus = db.scalar(select(Ausencia).where(Ausencia.situacao == "pendente"))
        aus_id = aus.id
        assert db.scalar(select(Alerta).where(Alerta.tipo == "ausencia"))
    assert apurado("Bruno", QUI, QUI)["dias_ausencia"] == 0  # pendente ainda não vale
    tela = g.get("/painel/ausencias").text
    assert "Enviados pelos funcionários (1)" in tela
    assert g.get(f"/anexo/{aus_id}").content == JPEG
    assert bruno.get(f"/anexo/{aus_id}").status_code == 200
    assert ana.get(f"/anexo/{aus_id}").status_code == 404  # colega não vê
    posta(g, f"/painel/ausencias/{aus_id}", tg, acao="aprovar")
    assert "Aceito" in bruno.get("/app/ajustes").text
    # ---- sexta: feriado. Ana trabalha 4 h (extra em feriado)
    bate(ana, ta, "08:00", SEX)
    bate(ana, ta, "12:00", SEX)

    # ---- apuração
    as_("23:00", SEX)
    tela = g.get(f"/painel/fechamento?de={SEG}&ate={SEX}").text
    assert "só pode ser fechado depois" in tela and 'type="submit" disabled' in tela
    r = posta(g, "/painel/fechamento", tg, de=SEG.isoformat(), ate=SEX.isoformat())
    with SessionLocal() as db:
        assert not db.scalar(select(Fechamento))
    as_("08:00", SAB)
    a, b = apurado("Ana"), apurado("Bruno")
    h = regras.fmt_min
    assert (h(a["previsto"]), h(a["trabalhado"])) == ("24h00", "27h40"), (a["previsto"], a["trabalhado"])
    assert (a["extra_normal"], h(a["extra_especial"]), a["debito"]) == (0, "4h00", 20)
    assert (a["faltas"], a["dias_ausencia"], apuracao.fmt_saldo(a["saldo_final"])) == (0, 1, "+3h40")
    assert "intervalo de 60 min descontado" in a["dias"][0]["obs"]
    assert (h(b["previsto"]), h(b["trabalhado"])) == ("16h00", "18h10"), (b["previsto"], b["trabalhado"])
    # quarta: entrou 08:35, saiu 17:40 batendo o intervalo = 8h05 (dentro da tolerância)
    assert (h(b["extra_normal"]), b["debito"], b["faltas"], h(b["falta_min"])) == ("2h05", 0, 1, "8h00")
    assert b["dias_ausencia"] == 1 and "falta" in b["dias"][1]["obs"]
    r = posta(g, "/painel/banco", tg, usuario_id=ids["Bruno"], data=QUI.isoformat(),
              sentido="debito", horas="1:00", motivo="horas pagas na folha")
    assert apuracao.fmt_saldo(apurado("Bruno")["saldo_final"]) == "+1h05"
    posta(g, "/painel/banco", tg, usuario_id=ids["Bruno"], data=QUI.isoformat(),
          sentido="debito", horas="uma", motivo="x")
    tela = g.get(f"/painel/fechamento?de={SEG}&ate={SEX}").text
    assert "formato 2:30" in tela
    for trecho in ("+3h40", "+1h05", "1 dia(s)"):
        assert trecho in tela, (trecho, re.findall(r"<td>.*?</td>", tela)[:40])
    assert 'type="submit" disabled' not in tela
    resumo = g.get(f"/painel/fechamento/resumo?de={SEG}&ate={SEX}").text
    # trabalhadas 27h40 + 18h10; extras 4h00 (feriado) + 2h05; Bruno faltou 1 dia = 8h00
    for trecho in ("45h50", "Horas extras", "6h05", "a 100% (folga ou feriado)", "Faltas em horas",
                   "Faltas em dias", "8h00"):
        assert trecho in resumo, trecho
    assert "Adicional noturno" not in resumo                 # ninguém trabalhou à noite
    folha = g.get(f"/painel/folha?func={ids['Ana']}&de={SEG}&ate={SEX}").text
    assert "Folha de ponto" in folha and "Feriado" in folha and "Atestado" in folha
    assert "período ainda aberto" in folha and "27h40" in folha
    linhas_folha = folha.split("<tbody>")[1].split("</tbody>")[0]
    assert "intervalo de 60 min descontado" not in linhas_folha and "lançada à mão" not in linhas_folha

    # ---- fechar o período: trava tudo
    r = posta(g, "/painel/fechamento", tg, de=SEG.isoformat(), ate=SEX.isoformat())
    assert r.headers["location"] == "/painel/fechamento", r.text
    with SessionLocal() as db:
        f = db.scalar(select(Fechamento))
        fid = f.id
        linhas = {l.usuario_id: l for l in f.linhas}
        assert linhas[ids["Ana"]].saldo_final == 220 and linhas[ids["Bruno"]].saldo_final == 65
        la, lb = linhas[ids["Ana"]].id, linhas[ids["Bruno"]].id
        bid = db.scalar(select(Batida.id).where(Batida.usuario_id == ids["Ana"]).limit(1))
    posta(g, "/painel/fechamento", tg, de=SEG.isoformat(), ate=SEX.isoformat())
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Fechamento)))) == 1  # não fecha duas vezes
    as_("09:00", SAB)
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Bruno"], tipo="entrada",
          data=TER.isoformat(), hora="08:00")
    assert "período de ponto já fechado" in g.get("/painel/ponto").text
    posta(g, f"/painel/ponto/{bid}/apagar", tg)
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=SEG.isoformat(), modo="limpar",
          repetir="semana")
    assert "5 dia(s) não foram alterados" in g.get(f"/painel/escala?semana={SEG}").text
    posta(g, "/painel/ausencias", tg, usuario_id=ids["Bruno"], tipo="abono",
          inicio=TER.isoformat(), fim=TER.isoformat())
    posta(g, f"/painel/ausencias/{aus_id}", tg, acao="remover")
    posta(g, "/painel/feriados", tg, data=TER.isoformat(), nome="Inventado")
    posta(ana, "/app/ajustes", ta, tipo="saida", data=SEX.isoformat(), hora="13:00",
          motivo="saí mais tarde")
    assert "período de ponto fechado" in ana.get("/app/ajustes").text
    with SessionLocal() as db:
        assert not db.scalar(select(Batida).where(Batida.anulada.is_(True)))
        assert len(list(db.scalars(select(Batida)))) == 14
        assert len(list(db.scalars(select(Turno).where(Turno.usuario_id == ids["Ana"])))) == 5
        assert len(list(db.scalars(select(Ausencia)))) == 2 and TER not in {
            x.data for x in db.scalars(select(Feriado))}
    assert apuracao.fmt_saldo(apurado("Ana")["saldo_final"]) == "+3h40"

    # ---- planilha para o contador
    planilha = g.get(f"/painel/fechamento/{fid}/planilha.csv").text
    assert ("Ana Souza;;16/11/2026 a 20/11/2026;24h00;27h40;0h00;4h00;8h00;0h00;0h20;0;0h00;1;0h00;0h00;+3h40;"
            "não assinada") in planilha, planilha
    assert "Bruno Lima;;16/11/2026 a 20/11/2026;16h00;18h10;2h05;0h00;3h08;0h00;0h00;1;8h00;1;-1h00;0h00;+1h05" in planilha
    assert "Extras em dia normal (50%);Extras em folga/feriado (100%)" in planilha
    # percentuais: mudar depois não altera a planilha de um período já fechado
    config(g, tg, pct_extra_normal="60", pct_extra_especial="120")
    assert "Extras (60%)" in g.get("/painel/fechamento").text
    assert "(50%)" in g.get(f"/painel/fechamento/{fid}/planilha.csv").text
    assert "Extras em folga/feriado (100%)" in ana.get(f"/app/folha/{la}").text
    config(g, tg, pct_extra_normal="999")
    assert "percentuais de hora extra" in g.get("/painel/config").text
    config(g, tg)

    # ---- funcionário confere e assina
    minhas = ana.get("/app/folha").text
    assert "Conferir e assinar" in minhas and "+3h40" in minhas
    assert "Assinar a folha" in ana.get(f"/app/folha/{la}").text
    assert ana.get(f"/app/folha/{lb}").headers["location"] == "/app/folha"  # folha do colega não
    posta(ana, f"/app/folha/{la}", ta, acao="aceitar", senha="errada")
    assert "Senha incorreta" in ana.get(f"/app/folha/{la}").text
    posta(bruno, f"/app/folha/{lb}", tb, acao="contestar", texto="na terça eu trabalhei")
    with SessionLocal() as db:
        assert "não concordou com a folha" in db.scalar(select(Alerta).where(Alerta.tipo == "folha")).texto
    posta(ana, f"/app/folha/{la}", ta, acao="aceitar", senha="senha123")
    folha = ana.get(f"/app/folha/{la}").text
    assert "Assinada eletronicamente por Ana Souza" in folha and "Assinar a folha" not in folha
    posta(ana, f"/app/folha/{la}", ta, acao="contestar", texto="mudei de ideia")  # já assinou
    tela = g.get("/painel/fechamento").text
    assert "1 de 2" in tela and "na terça eu trabalhei" in tela
    assert "Assinada eletronicamente" in g.get(f"/painel/folha?func={ids['Ana']}&de={SEG}&ate={SEX}").text
    assert "folha assinada" in g.get("/painel/historico").text

    # ---- outra empresa não enxerga nada
    hh = novo()
    posta(hh, "/cadastro", csrf(hh, "/cadastro"), aceito="1", empresa="Loja B", nome="Hugo", email="hugo@b.com",
          senha="segredo2")
    th = csrf(hh, "/painel/config")
    assert hh.get(f"/painel/fechamento/{fid}/planilha.csv").status_code == 303
    assert "Ana Souza" not in hh.get(f"/painel/fechamento/resumo?de={SEG}&ate={SEX}").text
    assert hh.get(f"/painel/folha?func={ids['Ana']}&de={SEG}&ate={SEX}").status_code == 303
    assert hh.get(f"/anexo/{aus_id}").status_code == 404
    posta(hh, f"/painel/fechamento/{fid}/reabrir", th)
    posta(hh, f"/painel/ausencias/{aus_id}", th, acao="remover")
    assert "Ana Souza" not in hh.get(f"/painel/fechamento?de={SEG}&ate={SEX}").text
    with SessionLocal() as db:
        assert db.get(Fechamento, fid) and db.get(Ausencia, aus_id)

    # ---- o saldo passa para o período seguinte
    seg2 = SEG + timedelta(days=7)
    posta(g, "/painel/escala/copiar", tg, semana=seg2.isoformat())
    bate(ana, ta, "08:00", seg2)
    bate(ana, ta, "18:00", seg2)
    as_("20:00", seg2)
    r = apurado("Ana", SAB, seg2)
    assert (r["saldo_anterior"], r["extra_normal"], apuracao.fmt_saldo(r["saldo_final"])) == (220, 60, "+4h40")
    assert "+4h40" in ana.get("/app/folha").text
    assert "+4h40" in g.get("/painel/fechamento").text  # período sugerido: 21/11 em diante
    config(g, tg, controle="extra")
    assert apurado("Ana", SAB, seg2)["saldo_anterior"] == 0
    config(g, tg)

    # ---- reabrir: destrava e descarta as assinaturas
    posta(g, f"/painel/fechamento/{fid}/reabrir", tg)
    with SessionLocal() as db:
        assert not db.scalar(select(Fechamento)) and not db.scalar(select(FechamentoLinha))
    hist = g.get("/painel/historico").text
    assert "período reaberto" in hist and "1 assinatura(s) descartada(s)" in hist
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Bruno"], tipo="entrada",
          data=TER.isoformat(), hora="08:00", obs="estava lá")
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Bruno"], tipo="saida",
          data=TER.isoformat(), hora="17:00", obs="estava lá")
    b = apurado("Bruno")
    assert b["faltas"] == 0 and "batida lançada à mão" in b["dias"][1]["obs"]
    assert h(b["previsto"]) == "24h00" and h(b["trabalhado"]) == "26h10"
    print("OK: fase 2 passou")


if __name__ == "__main__":
    main()
