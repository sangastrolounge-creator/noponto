"""Página Ponto (resumo + calendário com feriados) e Ajuste de ponto (apropriação de horas):
pontos originais, histórico, ponto em atraso, preenchimento automático, motivo de ajuste,
atestados e afastamentos, sobreaviso e escala do dia.
Rodar da raiz: .venv/Scripts/python tests/test_apropriacao.py"""
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
os.environ["IDIOMA_PADRAO"] = "pt"
os.environ.pop("PUBLIC_URL", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import apuracao, calculo_folha, calendario  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import (Ausencia, Batida, Empresa, FechamentoLinha, MotivoAjuste,  # noqa: E402
                    SessionLocal, Sobreaviso, Turno, Usuario)
from app.main import app  # noqa: E402

HOJE = date(2026, 11, 18)          # quarta-feira
SEG, TER, SEX = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 13)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora: str, dia: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def flash(cli, caminho):
    return cli.get(caminho).text


def puras():
    c = dict(calendario.comemorativas(2026))
    assert c[date(2026, 5, 10)] == "Dia das Mães" and c[date(2026, 8, 9)] == "Dia dos Pais"
    assert c[date(2026, 11, 27)] == "Black Friday" and c[date(2026, 4, 5)] == "Páscoa"
    f = dict(calendario.facultativos(2026))
    assert f[date(2026, 2, 17)] == "Carnaval" and f[date(2026, 6, 4)] == "Corpus Christi"


def main():
    puras()
    as_("10:00", HOJE)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), empresa="Bar do Zé", nome="Zé Gestor",
          email="ze@bar.com", senha="segredo1", aceito="1")
    tg = csrf(g, "/painel/config")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        for uid in ids.values():
            for d in (SEX, SEG, TER, HOJE):
                db.add(Turno(empresa_id=1, usuario_id=uid, data=d, inicio="09:00", fim="17:00",
                             intervalo_min=60, intervalo_inicio="12:00"))
        db.commit()
    # Ana só bateu a entrada na terça
    as_("09:02", TER)
    posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Ana"], tipo="entrada", data=TER.isoformat(),
          hora="09:00", obs="teste")
    with SessionLocal() as db:   # é uma batida "do aparelho": tira a marca de manual
        b = db.scalar(select(Batida))
        b.manual, b.obs = False, ""
        db.commit()
    as_("10:00", HOJE)

    # ================================================= página Ponto: resumo e calendário
    r = g.get("/painel/ponto?de=2026-11-01&ate=2026-11-18")
    assert r.status_code == 200, r.text[-2000:]
    t = r.text
    assert "Novembro de 2026" in t and "Proclamação da República" in t and "Consciência Negra" in t
    assert "Black Friday" in t and "Batidas faltando" in t and "Horas trabalhadas" in t
    assert 'href="/painel/escala?semana=2026-11-16&dia=2026-11-16#dia-destaque"' in t
    assert "ainda não cadastrado na empresa" in t
    t = g.get("/painel/ponto?de=2026-11-01&ate=2026-11-18&mes=2026-12").text
    assert "Dezembro de 2026" in t and "Natal" in t and "Véspera de Natal" in t
    assert g.get("/painel/ponto?mes=xx").status_code == 200
    t = g.get("/painel/escala?semana=2026-11-16&dia=2026-11-18").text
    assert 'id="dia-destaque"' in t
    assert 'href="/painel/ponto/ajuste"' in g.get("/painel").text      # submenu

    # ================================================= Ajuste de ponto
    base = f"/painel/ponto/ajuste?de=2026-11-13&ate=2026-11-18&func={ids['Ana']}"
    r = g.get(base)
    assert r.status_code == 200, r.text[-2000:]
    t = r.text
    for rotulo in ("Coleta de pontos originais", "Histórico de alterações", "Lançar ponto em atraso",
                   "Preenchimento automático", "Lançar motivo de ajuste", "Lançar atestados e afastamentos",
                   "Lançar sobreaviso", "Consultar escala", "Colaboradores", "Bruno Lima"):
        assert rotulo in t, rotulo
    for acao in ("originais", "historico", "atraso", "preencher", "motivo", "ausencia", "sobreaviso"):
        r = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/{acao}")
        assert r.status_code == 200 and 'id="acao-conteudo"' in r.text, (acao, r.status_code)
    r = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/escala")
    assert r.headers["location"] == "/painel/escala?semana=2026-11-16&dia=2026-11-16"
    assert g.get(f"/painel/ponto/ajuste/{ids['Ana']}/xx/originais").status_code == 303

    # ---- preenchimento automático: pede motivo; preenche pela escala com o intervalo
    t = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/preencher").text
    assert "09:00" in t and "12:00" in t and "13:00" in t and "17:00" in t
    url = f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/preencher"
    r = posta(g, url, tg, motivo="", volta=base)
    assert r.headers["location"] == base and "Informe o motivo" in flash(g, base)
    r = posta(g, url, tg, motivo="totem desligado", volta=base)
    assert "preenchido pela escala" in flash(g, base)
    with SessionLocal() as db:
        bats = list(db.scalars(select(Batida).where(Batida.usuario_id == ids["Ana"], Batida.dia == SEG)
                               .order_by(Batida.momento)))
    assert [(b.tipo, b.manual) for b in bats] == [("entrada", True), ("saida", True), ("entrada", True), ("saida", True)]
    assert all(b.obs == "Preenchimento automático: totem desligado" for b in bats)
    posta(g, url, tg, motivo="de novo", volta=base)
    assert "Nada a preencher" in flash(g, base)
    # terça: só completa a saída que faltou
    posta(g, f"/painel/ponto/ajuste/{ids['Ana']}/{TER.isoformat()}/preencher", tg, motivo="esqueceu", volta=base)
    with SessionLocal() as db:
        bats = list(db.scalars(select(Batida).where(Batida.usuario_id == ids["Ana"], Batida.dia == TER)
                               .order_by(Batida.momento)))
    assert [(b.tipo, b.manual) for b in bats] == [("entrada", False), ("saida", True)]
    # hoje às 10h: o horário da escala ainda não passou, só a entrada entra
    t = g.get(f"/painel/ponto/ajuste/{ids['Bruno']}/{HOJE.isoformat()}/preencher").text
    assert "09:00" in t and "17:00" not in t.split("Vamos lançar")[1].split("</ul>")[0]
    # volta não pode levar para fora
    r = posta(g, url, tg, motivo="x x x", volta="https://mal.example/painel/ponto/ajuste")
    assert r.headers["location"] == "/painel/ponto/ajuste"

    # ---- editar uma marcação clicando nela: anula a original e lança a corrigida
    with SessionLocal() as db:
        orig = db.scalar(select(Batida).where(Batida.usuario_id == ids["Ana"], Batida.dia == TER,
                                              Batida.tipo == "entrada"))
        oid = orig.id
    t = g.get(base).text
    assert f'href="/painel/ponto/ajuste/batida/{oid}?volta=' in t
    assert f"/painel/ponto/ajuste/{ids['Bruno']}/{TER.isoformat()}/atraso?tipo=entrada" not in t   # Bruno não é o atual
    t = g.get(f"/painel/ponto/ajuste/batida/{oid}").text
    assert 'id="acao-conteudo"' in t and "Novo horário" in t and 'value="09:00"' in t
    eurl = f"/painel/ponto/ajuste/batida/{oid}/editar"
    posta(g, eurl, tg, hora="08:55", motivo="", volta=base)
    assert "motivo da correção" in flash(g, base)
    posta(g, eurl, tg, hora="09:00", motivo="nada mudou", volta=base)
    assert "mesmo de antes" in flash(g, base)
    r = posta(g, eurl, tg, hora="08:55", motivo="relógio do totem adiantado", volta=base)
    assert r.headers["location"] == base and "corrigida: 09:00 → 08:55" in flash(g, base)
    with SessionLocal() as db:
        velha = db.get(Batida, oid)
        nova = db.scalar(select(Batida).where(Batida.usuario_id == ids["Ana"], Batida.dia == TER,
                                              Batida.tipo == "entrada", Batida.anulada.is_(False)))
        assert velha.anulada and velha.anulada_por == "Zé Gestor"
        assert nova.manual and nova.obs == "Corrigida de 09:00: relógio do totem adiantado" and nova.dia == TER
    posta(g, eurl, tg, hora="08:50", motivo="de novo", volta=base)
    assert "já foi anulada" in flash(g, base)
    t = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{TER.isoformat()}/historico").text
    assert "Corrigiu a entrada de Ana Souza em 17/11/2026: 09:00 → 08:55" in t
    t = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{TER.isoformat()}/originais").text
    assert "anulada" in t and "Corrigida de 09:00" in t
    # a batida que falta vira um link para lançá-la, já com o horário da escala
    t = g.get(f"/painel/ponto/ajuste/{ids['Bruno']}/{TER.isoformat()}/atraso?tipo=saida").text
    assert 'value="17:00"' in t and '<option value="saida" selected>' in t

    # ---- ponto em atraso (lançar batida) volta para o ajuste
    r = posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Bruno"], tipo="entrada", data=TER.isoformat(),
              hora="09:10", obs="sem bateria", volta=base)
    assert r.headers["location"] == base

    # ---- motivo de ajuste com abono: a falta da sexta some
    with SessionLocal() as db:
        r0 = apuracao.apurar(db, db.get(Empresa, 1), SEX, SEX, ids["Bruno"])[0]
    assert r0["faltas"] == 1
    murl = f"/painel/ponto/ajuste/{ids['Bruno']}/{SEX.isoformat()}/motivo"
    posta(g, murl, tg, motivo="Inventado", volta=base)
    assert "Escolha o motivo" in flash(g, base)
    posta(g, murl, tg, motivo="Outro", obs="", volta=base)
    assert "escreva o motivo" in flash(g, base)
    posta(g, murl, tg, motivo="Falta justificada", obs="consulta do filho", abonar="1", volta=base)
    with SessionLocal() as db:
        assert db.scalar(select(MotivoAjuste.motivo)) == "Falta justificada"
        assert db.scalar(select(Ausencia.tipo).where(Ausencia.usuario_id == ids["Bruno"])) == "abono"
        r1 = apuracao.apurar(db, db.get(Empresa, 1), SEX, SEX, ids["Bruno"])[0]
        assert r1["faltas"] == 0 and r1["dias"][0]["motivos"][0].motivo == "Falta justificada"
        mid = db.scalar(select(MotivoAjuste.id))
    assert "Falta justificada" in g.get(f"/painel/ponto/ajuste?de=2026-11-13&ate=2026-11-18&func={ids['Bruno']}").text

    # ---- atestado pelo ajuste volta para a mesma página
    r = g.post("/painel/ausencias", data={"csrf": tg, "usuario_id": ids["Ana"], "tipo": "atestado",
                                          "inicio": HOJE.isoformat(), "fim": HOJE.isoformat(),
                                          "obs": "CID J11", "volta": base})
    assert r.headers["location"] == base
    with SessionLocal() as db:
        assert db.scalar(select(Ausencia.tipo).where(Ausencia.usuario_id == ids["Ana"])) == "atestado"

    # ---- sobreaviso: 18:00 às 06:00 = 12 h, pago a 1/3 da hora
    surl = f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/sobreaviso"
    posta(g, surl, tg, inicio="18:00", fim="06:00", obs="plantão", volta=base)
    posta(g, surl, tg, inicio="06:00", fim="20:00", volta=base)        # 12 + 14 > 24 h
    assert "passa de 24 horas" in flash(g, base)
    posta(g, surl, tg, inicio="25:00", fim="06:00", volta=base)
    with SessionLocal() as db:
        sob = list(db.scalars(select(Sobreaviso)))
        assert [(s.inicio, s.fim, s.minutos) for s in sob] == [("18:00", "06:00", 720)]
        r2 = apuracao.apurar(db, db.get(Empresa, 1), SEG, SEG, ids["Ana"])[0]
    assert r2["sobreaviso"] == 720 and "sobreaviso: 12h00" in r2["dias"][0]["obs"]
    t = g.get(base).text
    assert "Sobreaviso" in t and "sobreaviso: 12h00" in t
    # folha: 12 h × (2.200 ÷ 220 = R$ 10/h) ÷ 3 = R$ 40,00, tributável
    with SessionLocal() as db:
        calc = calculo_folha.calcular(220000, 220, 0, 0, [], {"sobreaviso": 720}, 50, 100, 20, "extra",
                                      calculo_folha.tabelas(db))
    linha = next(l for l in calc["linhas"] if l["cod"] == "sobreaviso")
    assert linha["valor"] == 4000 and linha["trib"], linha

    # ---- histórico e pontos originais do dia
    t = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/historico").text
    assert "Preencheu o ponto de Ana Souza" in t and "Sobreaviso de Ana Souza" in t
    assert "Bruno" not in t.split('id="acao-conteudo"')[1]
    t = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{TER.isoformat()}/originais").text
    assert "Lançada pelo gestor" in t and "Celular (GPS)" in t

    # ---- remover
    with SessionLocal() as db:
        sid = db.scalar(select(Sobreaviso.id))
    posta(g, f"/painel/ponto/ajuste/sobreaviso/{sid}/remover", tg, volta=base)
    posta(g, f"/painel/ponto/ajuste/motivo/{mid}/remover", tg, volta=base)
    with SessionLocal() as db:
        assert not db.scalar(select(Sobreaviso.id)) and not db.scalar(select(MotivoAjuste.id))
    posta(g, surl, tg, inicio="20:00", fim="23:00", volta=base)

    # ---- fechamento leva o sobreaviso; dia fechado não muda mais
    with SessionLocal() as db:
        apuracao.fechar(db, db.get(Empresa, 1), date(2026, 11, 1), TER, "Zé")
        db.commit()
        assert db.scalar(select(FechamentoLinha.sobreaviso).where(FechamentoLinha.usuario_id == ids["Ana"])) == 180
    posta(g, surl, tg, inicio="01:00", fim="02:00", volta=base)
    assert "período de ponto já fechado" in flash(g, base)
    t = g.get(f"/painel/ponto/ajuste/{ids['Ana']}/{SEG.isoformat()}/preencher").text
    assert "já fechado" in t
    t = g.get("/painel/fechamento?de=2026-11-01&ate=2026-11-17").text
    assert "Sobreaviso" in t

    print("OK: calendário do ponto e ajuste de ponto passaram")


if __name__ == "__main__":
    main()
