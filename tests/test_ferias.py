"""Férias (CLT) e intervalo intrajornada não concedido (art. 71, §4º).
Rodar da raiz: .venv/Scripts/python tests/test_ferias.py"""
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

from app import apuracao, clt, conformidade, ferias  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import (Ausencia, Empresa, Fechamento, FechamentoLinha, FeriasAjuste,  # noqa: E402
                    SessionLocal, Usuario)
from app.main import app  # noqa: E402

HOJE = date(2026, 11, 18)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
LAT, LNG = -2.5307, -44.3068


def as_(hora: str, dia: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def regras_puras():
    D = date
    assert [clt.dias_de_direito(n) for n in (0, 5, 6, 14, 15, 23, 24, 32, 33)] == [30, 30, 24, 24, 18, 18, 12, 12, 0]
    assert clt.somar_anos(D(2024, 2, 29), 1) == D(2025, 2, 28)
    assert clt.fim_concessivo(D(2025, 3, 9)) == D(2026, 3, 9)
    assert clt.avos(D(2025, 3, 10), D(2025, 6, 1)) == 3 and clt.avos(D(2025, 3, 10), D(2025, 5, 20)) == 2
    # admissão 10/03/2023; 7 faltas no 1º período (24 dias); 14 dias em jul/24 e 15 + 5 vendidos em abr/25
    p = clt.saldo_ferias(D(2023, 3, 10), D(2025, 6, 1), {D(2023, 3, 10): 7},
                         [{"inicio": D(2024, 7, 1), "fim": D(2024, 7, 14), "abono": 0},
                          {"inicio": D(2025, 4, 1), "fim": D(2025, 4, 15), "abono": 5}])
    assert [(x["direito"], x["usado"], x["saldo"], x["situacao"]) for x in p] == \
        [(24, 24, 0, "gozado"), (30, 10, 20, "a_gozar"), (30, 0, 30, "aquisicao")]
    assert p[0]["em_dobro"] == 5    # o 1º período venceu em 09/03/2025: o que sobrou dele saiu em dobro
    # mais de 6 meses afastado pelo INSS: perde as férias do período (art. 133, IV)
    p = clt.saldo_ferias(D(2024, 1, 1), D(2025, 6, 1), {}, [], afastamentos={D(2024, 1, 1): 200})
    assert p[0]["direito"] == 0 and p[0]["perdeu_afastamento"]
    # PJ não tem férias; estágio tem 30 dias de recesso sem a tabela de faltas
    assert clt.saldo_ferias(D(2024, 1, 1), D(2025, 6, 1), {}, [], contrato="pj") == []
    assert clt.saldo_ferias(D(2024, 1, 1), D(2025, 6, 1), {D(2024, 1, 1): 40}, [],
                            contrato="estagio")[0]["direito"] == 30
    # avisos ao marcar: menos de 5 dias, aviso de 30 dias e início antes de feriado/repouso
    per = dict(inicio=D(2024, 3, 10), fim=D(2025, 3, 9), saldo=20, direito=30, concessivo=D(2026, 3, 9))
    av = clt.conferir_ferias(D(2025, 6, 10), D(2025, 6, 13), 0, D(2025, 6, 1), per, [10], {D(2025, 6, 12)})
    assert any("5 dias corridos" in a for a in av) and any("30 dias de antecedência" in a for a in av)
    assert any("2 dias antes" in a for a in av)
    assert clt.conferir_ferias(D(2025, 8, 4), D(2025, 8, 23), 0, D(2025, 6, 1), per, [10], set()) == []
    av = clt.conferir_ferias(D(2025, 8, 4), D(2025, 8, 13), 11, D(2025, 6, 1), per, [], set())
    assert any("1/3" in a for a in av) and any("saldo" in a for a in av)
    assert clt.valor_ferias(300000, 30)["total"] == 400000
    # intervalo: maior intervalo do dia; pré-assinalado conta; 4 a 6 h pedem 15 min
    assert clt.intervalo_suprimido(480, [], False) == 60
    assert clt.intervalo_suprimido(480, [20, 25], False) == 35
    assert clt.intervalo_suprimido(480, [], True) == 0
    assert clt.intervalo_suprimido(300, [], False) == 15 and clt.intervalo_suprimido(240, [], False) == 0
    assert clt.intervalo_suprimido(480, [30], False, 30) == 0


def main():
    regras_puras()
    as_("09:00", HOJE)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), empresa="Padaria A", nome="Gil Gestor",
          email="gil@a.com", senha="segredo1", aceito="1")
    tg = csrf(g, "/painel/config")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002"), ("Caio PJ", "98999990003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        ids = {k: u.id for k, u in us.items()}
        token_bruno = us["Bruno"].convite_token
        us["Ana"].data_admissao = date(2024, 10, 1)     # 1º período venceu em 30/09/2026
        us["Ana"].salario_centavos = 300000
        us["Bruno"].data_admissao = date(2025, 1, 10)   # prazo do 1º período termina em 09/01/2027
        us["Caio"].classificacao = "pj"
        # 7 faltas num fechamento dentro do 2º período da Ana: ele fica com 24 dias
        f = Fechamento(empresa_id=1, inicio=date(2026, 3, 1), fim=date(2026, 3, 31), criado_por="Gil")
        f.linhas.append(FechamentoLinha(empresa_id=1, usuario_id=ids["Ana"], faltas=7))
        db.add(f)
        db.commit()

    # ---- saldos
    with SessionLocal() as db:
        ana = ferias.situacao(db, db.get(Usuario, ids["Ana"]), HOJE)
        bruno = ferias.situacao(db, db.get(Usuario, ids["Bruno"]), HOJE)
        assert ferias.situacao(db, db.get(Usuario, ids["Caio"]), HOJE) is None
    assert [(p["direito"], p["saldo"], p["situacao"]) for p in ana["periodos"][:2]] == \
        [(30, 30, "vencido"), (24, 24, "a_gozar")], ana["periodos"]
    assert ana["saldo"] == 54 and len(ana["vencidos"]) == 1
    assert bruno["saldo"] == 30 and bruno["vencendo"] and bruno["proximo"]["concessivo"] == date(2027, 1, 9)

    r = g.get("/painel/ferias")
    tela = r.text
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    assert "Vencido: pagar em dobro" in tela and "Ana Souza" in tela, tela[-3000:]
    assert "Caio PJ" not in tela.split('id="lancar"')[0]
    assert "54 dia(s)" in tela and 'href="/painel/ferias"' in tela

    # ---- conformidade: férias vencidas são problema
    with SessionLocal() as db:
        c = conformidade.verificar(db, db.get(Empresa, 1), db.scalar(select(Usuario).where(Usuario.email == "gil@a.com")))
    it = {i["chave"]: i for i in c["itens"]}
    assert it["ferias"]["situacao"] == "problema" and "Ana Souza" in it["ferias"]["detalhes"][0]

    # ---- marcar férias: com aviso (menos de 30 dias de antecedência) a tela pede confirmação
    r = posta(g, "/painel/ferias", tg, usuario_id=ids["Ana"], inicio="2026-12-01", fim="2026-12-30", abono="0")
    assert r.status_code == 200 and "Confira antes de lançar" in r.text and "30 dias de antecedência" in r.text
    with SessionLocal() as db:
        assert not db.scalar(select(Ausencia.id).where(Ausencia.tipo == "ferias"))
    r = posta(g, "/painel/ferias", tg, usuario_id=ids["Ana"], inicio="2026-12-01", fim="2026-12-30",
              abono="0", confirmar="1")
    assert r.status_code == 303
    with SessionLocal() as db:
        ana = ferias.situacao(db, db.get(Usuario, ids["Ana"]), HOJE)
    assert ana["periodos"][0]["saldo"] == 0 and ana["periodos"][0]["em_dobro"] == 30
    assert ana["saldo"] == 24
    # não deixa marcar por cima de outra ausência
    r = posta(g, "/painel/ferias", tg, usuario_id=ids["Ana"], inicio="2026-12-20", fim="2026-12-25",
              abono="0", confirmar="1")
    assert "já tem uma ausência" in g.get("/painel/ferias").text

    # ---- ajuste do gestor
    posta(g, "/painel/ferias/ajuste", tg, usuario_id=ids["Bruno"], periodo="2025-01-10", dias="-6",
          motivo="faltas de antes do sistema")
    with SessionLocal() as db:
        assert db.scalar(select(FeriasAjuste.dias)) == -6
        assert ferias.situacao(db, db.get(Usuario, ids["Bruno"]), HOJE)["saldo"] == 24

    # ---- funcionário pede férias pelo app; o gestor vê os avisos e aprova
    b = novo()
    caminho = f"/convite/{token_bruno}"
    assert posta(b, caminho, csrf(b, caminho), senha="senha123", consentimento="1").status_code == 303
    tb = csrf(b, "/app")
    tela = b.get("/app/ajustes").text
    assert "Minhas férias" in tela and "24 dia(s) para tirar" in tela
    r = posta(b, "/app/ferias", tb, inicio="2026-11-18", fim="2026-11-20")
    assert "depois de hoje" in b.get("/app/ajustes").text
    posta(b, "/app/ferias", tb, inicio="2027-01-04", fim="2027-01-13", abono="0", obs="viagem")
    with SessionLocal() as db:
        pedido = db.scalar(select(Ausencia).where(Ausencia.usuario_id == ids["Bruno"]))
        assert pedido.situacao == "pendente" and pedido.tipo == "ferias"
    tela = g.get("/painel/ausencias").text
    assert "Bruno Lima" in tela and "pagos em dobro" in tela          # termina depois de 09/01/2027
    posta(g, f"/painel/ausencias/{pedido.id}", tg, acao="aprovar")
    with SessionLocal() as db:
        assert db.get(Ausencia, pedido.id).situacao == "aprovado"
        bruno = ferias.situacao(db, db.get(Usuario, ids["Bruno"]), HOJE)
    assert bruno["saldo"] == 14 and bruno["periodos"][0]["em_dobro"] == 4

    # ---- intervalo não concedido: 8 h direto sem intervalo gera 1 h indenizada
    posta(g, "/painel/config", tg, nome="Padaria A", lat=str(LAT), lng=str(LNG), raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", controle="extra", intervalo_padrao_min="0", intervalo_auto="",
          tolerancia_dia_min="10", intervalo_minimo_min="60")
    with SessionLocal() as db:
        assert db.get(Empresa, 1).intervalo_minimo_min == 60
    dia = date(2026, 11, 16)
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia.isoformat(), modo="trabalho",
          inicio="08:00", fim="16:00", intervalo="0", repetir="dia")
    as_("18:00", HOJE)
    for tipo, hora in (("entrada", "08:00"), ("saida", "16:00")):
        posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Ana"], tipo=tipo, data=dia.isoformat(),
              hora=hora, obs="teste")
    with SessionLocal() as db:
        r = next(x for x in apuracao.apurar(db, db.get(Empresa, 1), dia, dia) if x["u"].id == ids["Ana"])
    assert r["intervalo_suprimido"] == 60 and "intervalo suprimido: 1h00 (art. 71, §4º)" in r["dias"][0]["obs"]
    tela = g.get(f"/painel/fechamento?de={dia}&ate={dia}").text
    assert "Intervalo não concedido" in tela
    r = posta(g, "/painel/config", tg, nome="Padaria A", lat=str(LAT), lng=str(LNG), raio_m="150",
              tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
              fotos_dias="45", controle="extra", intervalo_padrao_min="0", tolerancia_dia_min="10",
              intervalo_minimo_min="20")
    with SessionLocal() as db:
        assert db.get(Empresa, 1).intervalo_minimo_min == 60   # abaixo de 30 não aceita
    print("OK: férias e intervalo passaram")


if __name__ == "__main__":
    main()
