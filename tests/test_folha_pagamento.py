"""Folha de pagamento: cálculo do holerite, lançamentos, finalização, holerite no app e a
revisão pelo Claude (com a API simulada). Rodar da raiz: .venv/Scripts/python tests/test_folha_pagamento.py"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/teste.db".replace("\\", "/")
os.environ["SECRET_KEY"] = "teste"
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL", "ASAAS_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

import anthropic  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import calculo_folha as cf  # noqa: E402
from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Empresa, FolhaItem, FolhaPagamento, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

INI, FIM = date(2026, 11, 1), date(2026, 11, 30)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora: str, d: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


# ---- API do Claude simulada: guarda o pedido e devolve uma revisão pronta
pedidos = []


class FalsoClaude:
    def __init__(self, **kw):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        pedidos.append(kw)
        dados = json.loads(kw["messages"][0]["content"].split("\n\n", 1)[1])
        resp = {"resumo": f"Folha conferida: {len(dados['funcionarios'])} holerites.",
                "conferidos": len(dados["funcionarios"]), "divergencias": 0,
                "alertas": [{"funcionario": "Bruno Lima", "gravidade": "alta",
                             "mensagem": "Sem salário cadastrado."},
                            {"funcionario": "Geral", "gravidade": "baixa",
                             "mensagem": "Lembre de recolher o FGTS."}]}
        return SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5-5",
                               content=[SimpleNamespace(type="text", text=json.dumps(resp))])


def main():
    t = cf.TABELAS_PADRAO
    # ---- tabelas de 2026
    assert cf.inss(300000, t) == 24860                      # R$ 3.000: 7,5% + 9% + 12%
    assert cf.inss(1000000, t) == cf.inss(847555, t)       # teto
    assert cf.irrf(450000, cf.inss(450000, t), 0, t)[0] == 0      # isento até R$ 5.000
    assert cf.irrf(600000, cf.inss(600000, t), 0, t)[0] == 38510  # com o redutor
    alto = cf.irrf(1000000, cf.inss(1000000, t), 2, t)
    assert alto[1]["redutor"] == 0 and alto[0] > 140000

    as_("07:00", INI)
    g = TestClient(app, follow_redirects=False)
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar A", nome="Gil", email="gil@a.com",
          senha="segredo1")
    tg = csrf(g, "/painel/config")
    assert posta(g, "/painel/config", tg, nome="Bar A", raio_m="150", tolerancia_min="10",
                 fuso="America/Sao_Paulo", ajuste_dias="7", fotos_dias="45", controle="extra",
                 intervalo_padrao_min="0", tolerancia_dia_min="10", pct_extra_normal="50",
                 pct_extra_especial="100", pct_noturno="20", dsr_faltas="1",
                 dsr_extras="1").status_code == 303
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convite = db.get(Usuario, ids["Ana"]).convite_token
    a = TestClient(app, follow_redirects=False)
    assert posta(a, f"/convite/{convite}", csrf(a, f"/convite/{convite}"),
                 senha="senha123").status_code == 303
    for nome in ("Ana", "Bruno"):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data="2026-11-02",
              modo="trabalho", inicio="08:00", fim="16:00", intervalo="0", repetir="uteis", fixar="1")
        for d in ("2026-11-07", "2026-11-08"):
            posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=d, modo="folga",
                  repetir="dia", fixar="1")
    with SessionLocal() as db:                 # escala fixa do mês inteiro (domingo 1º = folga)
        regras.gerar_da_fixa(db, db.get(Empresa, 1), INI, FIM)
        db.commit()

    as_("08:00", FIM + timedelta(days=1))
    d = INI
    while d <= FIM:
        if d.weekday() < 5:
            for nome in ("Ana", "Bruno"):
                if nome == "Ana" and d == date(2026, 11, 3):
                    continue                                   # falta
                saida = "18:00" if nome == "Ana" and d == date(2026, 11, 2) else "16:00"
                for tipo, hora in (("entrada", "08:00"), ("saida", saida)):
                    posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo,
                          data=d.isoformat(), hora=hora, obs="teste")
        d += timedelta(days=1)

    # ---- dados salariais: Ana com salário; Bruno fica sem
    assert "Dados salariais da equipe" in g.get("/painel/pagamento").text
    r = posta(g, "/painel/pagamento/dados", tg, **{
        f"sal_{ids['Ana']}": "3.000,00", f"jor_{ids['Ana']}": "220", f"dep_{ids['Ana']}": "0",
        f"vt_{ids['Ana']}": "", f"sal_{ids['Bruno']}": "", f"jor_{ids['Bruno']}": "220",
        f"dep_{ids['Bruno']}": "0", f"vt_{ids['Bruno']}": ""})
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(Usuario, ids["Ana"]).salario_centavos == 300000

    posta(g, "/painel/fechamento", tg, de=INI.isoformat(), ate=FIM.isoformat())
    tela = g.get("/painel/pagamento").text
    fid_fech = re.search(r'<option value="(\d+)">01/11/2026 a 30/11/2026', tela).group(1)
    r = posta(g, "/painel/pagamento/gerar", tg, fechamento_id=fid_fech)
    folha_url = r.headers["location"]
    fid = int(folha_url.rsplit("/", 1)[1])
    with SessionLocal() as db:
        f = db.get(FolhaPagamento, fid)
        ana = next(i for i in f.itens if i.usuario_id == ids["Ana"])
        calc = json.loads(ana.calculo)
        val = {l["cod"]: l["valor"] for l in calc["linhas"]}
        iid_ana = ana.id
    # 2 h extras a 50% (valor da hora 3000/220); DSR das extras: 3 h ÷ 21 úteis × 9 folgas = 77 min
    # Turnos de 8 h sem intervalo: 20 dias × 1 h de intervalo não concedido a 50% (art. 71, §4º),
    # indenizatório (fora da base de INSS e FGTS): 20 h × 13,64 × 1,5 = R$ 409,09
    assert val == {"salario": 300000, "extra_normal": 4091, "dsr_extras": 1750,
                   "intervalo_suprimido": 40909,
                   "faltas": 10909, "dsr_perdido": 21818, "inss": 22149}, val
    assert (ana.bruto, ana.descontos, ana.liquido, ana.fgts) == (346750, 54876, 291874, 21849)
    tela = g.get(folha_url).text
    assert "R$ 3.018,01" in tela and "Sem salário cadastrado" in tela
    assert "A revisão pelo Claude ainda não foi ligada" in tela

    # ---- lançamento manual: comissão tributável recalcula INSS
    hol = f"/painel/pagamento/{fid}/holerite/{iid_ana}"
    assert "Recibo de pagamento de salário" in g.get(hol).text
    posta(g, f"{hol}/lancamento".replace("/holerite/", "/item/"), tg, descricao="Comissão",
          tipo="p", valor="200,00", tributavel="1")
    posta(g, f"{hol}/lancamento".replace("/holerite/", "/item/"), tg, descricao="Adiantamento",
          tipo="d", valor="500,00")
    with SessionLocal() as db:
        ana = db.get(FolhaItem, iid_ana)
        assert ana.bruto == 366750 and ana.liquido == 366750 - (21818 + cf.inss(304023, t) + 50000)
    posta(g, f"/painel/pagamento/{fid}/item/{iid_ana}/lancamento/1/remover", tg)
    with SessionLocal() as db:
        assert len(json.loads(db.get(FolhaItem, iid_ana).lancamentos)) == 1

    # ---- revisão pelo Claude (API simulada)
    tm = csrf(g, "/master/folha")
    assert "Desligada: falta a chave" in g.get("/master/folha").text
    posta(g, "/master/folha/chave", tm, chave="sk-ant-teste-0000000000000000000000000000")
    assert "sk-ant-teste" not in g.get("/master/folha").text      # a chave não volta na tela
    anthropic.Anthropic = FalsoClaude
    r = posta(g, f"/painel/pagamento/{fid}/revisar", tg)
    assert r.status_code == 303
    for _ in range(50):
        with SessionLocal() as db:
            if db.get(FolhaPagamento, fid).revisao_situacao != "andamento":
                break
        time.sleep(0.1)
    assert pedidos and pedidos[0]["model"] == "claude-sonnet-5-5"
    assert pedidos[0]["fallbacks"] == "default"
    assert pedidos[0]["output_config"]["format"]["type"] == "json_schema"
    enviado = json.loads(pedidos[0]["messages"][0]["content"].split("\n\n", 1)[1])
    assert enviado["funcionarios"][0]["funcionario"] == "Ana Souza"
    tela = g.get(folha_url).text
    assert "Folha conferida: 2 holerites." in tela and "Importante" in tela
    assert tela.index("Importante") < tela.index("Lembrete")
    # mudar um valor invalida a revisão
    posta(g, f"/painel/pagamento/{fid}/recalcular", tg)
    with SessionLocal() as db:
        assert db.get(FolhaPagamento, fid).revisao_situacao == ""

    # ---- finalizar: o funcionário vê o holerite; o ponto não pode ser reaberto
    assert a.get(f"/app/holerite/{iid_ana}").headers["location"] == "/app/folha"  # rascunho
    posta(g, f"/painel/pagamento/{fid}/finalizar", tg, acao="finalizar")
    with SessionLocal() as db:
        fech = db.get(FolhaPagamento, fid).fechamento_id
        iid_bruno = next(i.id for i in db.get(FolhaPagamento, fid).itens if i.id != iid_ana)
    posta(g, f"/painel/fechamento/{fech}/reabrir", tg)
    with SessionLocal() as db:
        assert db.get(FolhaPagamento, fid) is not None
    assert "Ver holerite" in a.get("/app/folha").text
    hol_app = a.get(f"/app/holerite/{iid_ana}").text
    assert "Recibo de pagamento de salário" in hol_app and "Comissão" in hol_app
    assert a.get(f"/app/holerite/{iid_bruno}").headers["location"] == "/app/folha"  # do colega não
    planilha = g.get(f"/painel/pagamento/{fid}/planilha.csv").text
    assert "Ana Souza" in planilha and "Comissão: +200,00" in planilha
    todos = g.get(f"/painel/pagamento/{fid}/holerites").text
    assert todos.count("Recibo de pagamento de salário") == 2
    # desfazer a finalização e reabrir o ponto apaga a folha
    posta(g, f"/painel/pagamento/{fid}/finalizar", tg, acao="desfazer")
    posta(g, f"/painel/fechamento/{fech}/reabrir", tg)
    with SessionLocal() as db:
        assert db.get(FolhaPagamento, fid) is None
    print("OK: folha de pagamento passou")


if __name__ == "__main__":
    main()
