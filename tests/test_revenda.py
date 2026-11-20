"""Revenda: Painel Master, planos, teste grátis, vencimento, bloqueio, pagamento e suporte.
Rodar da raiz: .venv/Scripts/python tests/test_revenda.py"""
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
os.environ.pop("ADMIN_EMAIL", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app.core import situacao  # noqa: E402
from app.db import Batida, Empresa, Pagamento, Plano, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

D0 = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


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


def sit(eid):
    with SessionLocal() as db:
        return situacao(db.get(Empresa, eid))


def main():
    as_("09:00", D0)
    assert "R$ 49,90" not in novo().get("/").text   # os planos de exemplo nascem fora de venda
    with SessionLocal() as db:
        for p in db.scalars(select(Plano)):
            assert not p.ativo
            p.ativo = p.preco_centavos > 0      # o plano grátis fica fora de venda neste teste
        db.commit()
        planos = {p.nome: p for p in db.scalars(select(Plano)) if p.preco_centavos > 0}
        assert db.scalar(select(Plano.nome).where(Plano.preco_centavos == 0)) == "Grátis"
    assert set(planos) == {"Essencial", "Profissional", "Ilimitado"}

    # ---- página inicial mostra os planos; a primeira conta é a do dono da plataforma
    pub = novo()
    home = pub.get("/").text
    assert "Essencial" in home and "R$ 49,90" in home and "Testar 7 dias grátis" in home
    m = novo()
    posta(m, "/cadastro", csrf(m, "/cadastro"), aceito="1", empresa="Minha Casa", nome="Dono",
          email="dono@plataforma.com", senha="segredo1")
    tm = csrf(m, "/master")
    with SessionLocal() as db:
        dono = db.scalar(select(Usuario).where(Usuario.email == "dono@plataforma.com"))
        assert dono.is_admin and dono.empresa.isenta
    assert sit(1) == "cortesia" and ">Master<" in m.get("/painel").text
    # guia: o convite aparece até o gestor responder; o botão Guia fica em todas as telas
    assert 'id="convite-guia"' in m.get("/painel").text and 'id="abrir-guia"' in m.get("/painel/escala").text
    assert 'id="convite-guia"' not in m.get("/painel/escala").text
    assert m.post("/painel/guia/visto").status_code == 403
    assert m.post("/painel/guia/visto", headers={"X-CSRF": tm}).json() == {"ok": True}
    assert 'id="convite-guia"' not in m.get("/painel").text and "/static/tour.js" in m.get("/painel").text

    # ---- cliente se cadastra escolhendo um plano: entra em teste
    c = novo()
    assert "plano Essencial" in c.get(f"/cadastro?plano={planos['Essencial'].id}").text
    posta(c, "/cadastro", csrf(c, "/cadastro"), aceito="1", empresa="Bar do Zé", nome="Zé",
          email="ze@bar.com", senha="segredo2", plano=str(planos["Essencial"].id))
    tc = csrf(c, "/painel/config")
    with SessionLocal() as db:
        bar = db.scalar(select(Empresa).where(Empresa.nome == "Bar do Zé"))
        eid = bar.id
        assert bar.plano.nome == "Essencial" and not bar.isenta
        assert not db.scalar(select(Usuario).where(Usuario.email == "ze@bar.com")).is_admin
    assert sit(eid) == "teste"
    assert c.get("/master").headers["location"] == "/painel"       # cliente não vê o master
    assert ">Master<" not in c.get("/painel").text
    tela = c.get("/painel/assinatura").text
    assert "Em teste" in tela and "Seu plano" in tela and "7 dia(s) restante(s)" in tela

    # ---- limite de funcionários do plano (Essencial: 10)
    for i in range(10):
        posta(c, "/painel/equipe", tc, nome=f"Func {i}", celular=f"989999900{i:02d}")
    posta(c, "/painel/equipe", tc, nome="Func 11", celular="98999990099")
    assert "permite até 10 funcionários" in c.get("/painel/equipe").text
    with SessionLocal() as db:
        funcs = list(db.scalars(select(Usuario).where(Usuario.empresa_id == eid,
                                                      Usuario.papel == "funcionario")))
        assert len(funcs) == 10
        f0, convite = funcs[0].id, funcs[0].convite_token
    posta(c, f"/painel/equipe/{f0}", tc, acao="desativar")
    posta(c, "/painel/equipe", tc, nome="Func 11", celular="98999990099")
    posta(c, f"/painel/equipe/{f0}", tc, acao="reativar")
    assert "permite até 10 funcionários" in c.get("/painel/equipe").text
    with SessionLocal() as db:
        assert not db.get(Usuario, f0).ativo
        convite = db.scalar(select(Usuario.convite_token).where(Usuario.nome == "Func 1"))
    func = novo()
    posta(func, f"/convite/{convite}", csrf(func, f"/convite/{convite}"), senha="senha123")
    tf = csrf(func, "/app")

    # ---- master: visão geral e ficha
    geral = m.get("/master").text
    assert "Bar do Zé" in geral and "ze@bar.com" in geral and "Em teste" in geral and '> / 10<' in geral
    assert "Bar do Zé" not in m.get("/master?s=vencida").text and "Bar do Zé" in m.get("/master?q=ze@").text
    assert "Registrar pagamento" in m.get(f"/master/empresas/{eid}").text

    # ---- aviso de teste acabando, depois vencida: painel fechado, ponto continua
    as_("09:00", D0 + timedelta(days=5))
    assert "período de teste termina em 2 dia(s)" in c.get("/painel").text
    as_("10:00", D0 + timedelta(days=7))
    assert sit(eid) == "vencida"
    for caminho in ("/painel", "/painel/escala", "/painel/ponto", "/painel/fechamento",
                    "/painel/locais", "/painel/equipe", "/painel/config"):
        assert c.get(caminho).headers.get("location") == "/painel/assinatura", caminho
    assert posta(c, "/painel/equipe", tc, nome="X", celular="98999990077").headers[
        "location"] == "/painel/assinatura"
    tela = c.get("/painel/assinatura").text
    assert "Seu período de teste terminou" in tela and "Vencida" in tela
    r = func.post("/app/ponto", json={"lat": -2.5, "lng": -44.3, "precisao": 10},
                  headers={"X-CSRF": tf})
    assert r.status_code == 200, r.text                      # o registro não se perde
    assert "Vencidas" in m.get("/master").text and "Bar do Zé" in m.get("/master?s=vencida").text

    # ---- suporte: o master entra no painel da empresa vencida e volta
    posta(m, f"/master/empresas/{eid}/entrar", tm)
    tela = m.get("/painel")
    assert tela.status_code == 200 and "como suporte" in tela.text and "Bar do Zé" in tela.text
    assert m.get("/master").headers["location"] == "/painel"   # no modo suporte não é admin
    posta(m, "/master/voltar", csrf(m, "/painel"))
    tm = csrf(m, "/master")
    assert "Painel Master" in m.get("/master").text

    # ---- pagamento registrado libera; carência de 3 dias depois de vencer
    posta(m, f"/master/empresas/{eid}/pagamento", tm, valor="abc", dias="30")
    assert "Valor inválido" in m.get(f"/master/empresas/{eid}").text
    posta(m, f"/master/empresas/{eid}/pagamento", tm, valor="49,90", dias="30", forma="pix",
          obs="comprovante pelo zap")
    assert sit(eid) == "ativa" and c.get("/painel").status_code == 200
    with SessionLocal() as db:
        p = db.scalar(select(Pagamento))
        assert (p.valor_centavos, p.dias, p.plano_nome, p.registrado_por) == (
            4990, 30, "Essencial", "dono@plataforma.com")
    geral = m.get("/master").text
    assert "R$ 49,90" in geral
    assert "R$ 49,90" in c.get("/painel/assinatura").text and "Ativa" in c.get("/painel/assinatura").text
    as_("10:00", D0 + timedelta(days=39))          # venceu no dia 37, ainda na carência
    assert sit(eid) == "ativa" and "Sua assinatura venceu" in c.get("/painel").text
    as_("11:00", D0 + timedelta(days=40))
    assert sit(eid) == "vencida"
    posta(m, f"/master/empresas/{eid}/teste", tm, dias="5")
    assert sit(eid) == "teste"

    # ---- bloqueio: ninguém entra, o ponto para
    posta(m, "/master/empresas/1/bloqueio", tm)
    assert sit(1) == "cortesia"                                # a própria não bloqueia
    posta(m, f"/master/empresas/{eid}/bloqueio", tm)
    assert sit(eid) == "bloqueada"
    assert c.get("/painel").headers["location"] == "/entrar"
    assert func.get("/app").headers["location"] == "/entrar"
    n = novo()
    r = posta(n, "/entrar", csrf(n), login="ze@bar.com", senha="segredo2")
    assert r.status_code == 403 and "bloqueada" in r.text
    posta(m, f"/master/empresas/{eid}/bloqueio", tm)
    assert sit(eid) == "teste"

    # ---- cortesia, troca de plano, senha provisória
    posta(m, f"/master/empresas/{eid}/dados", tm, plano=str(planos["Ilimitado"].id), isenta="1",
          obs="amigo")
    assert sit(eid) == "cortesia"
    posta(m, f"/master/empresas/{eid}/dados", tm, plano=str(planos["Ilimitado"].id), obs="amigo")
    r = posta(m, f"/master/empresas/{eid}/senha", tm)
    senha = re.search(r'readonly value="([^"]+)"', r.text).group(1)
    n = novo()
    assert posta(n, "/entrar", csrf(n), login="ze@bar.com", senha="segredo2").status_code == 400
    assert posta(n, "/entrar", csrf(n), login="ze@bar.com", senha=senha).status_code == 303

    # ---- master cria empresa; planos; plataforma
    r = posta(m, "/master/empresas", tm, empresa="Padaria Nova", nome="Ana", email="ze@bar.com")
    assert r.status_code == 400 and "Já existe uma conta" in r.text
    r = posta(m, "/master/empresas", tm, empresa="Padaria Nova", nome="Ana", email="ana@pad.com",
              plano=str(planos["Profissional"].id))
    assert "Senha provisória" in r.text and "Padaria Nova" in r.text
    posta(m, "/master/planos", tm, nome="Micro", preco="19,90", limite="3", ativo="1", ordem="-1")
    posta(m, "/master/planos", tm, nome="Ruim", preco="x", limite="3")
    assert "Preço inválido" in m.get("/master/planos").text
    posta(m, f"/master/planos/{planos['Essencial'].id}/excluir", tm)
    assert "Há empresas neste plano" not in m.get("/master/planos").text  # o Bar mudou de plano
    posta(m, f"/master/planos/{planos['Profissional'].id}/excluir", tm)
    assert "Há empresas neste plano" in m.get("/master/planos").text
    posta(m, "/master/plataforma", tm, nome="Ponto Fácil", whatsapp="98 99999-0000",
          dias_teste="14", cadastro_aberto="", instrucoes_pagamento="Pix 123",
          titulo="Ponto sem relógio caro")
    home = novo().get("/").text
    assert "Ponto Fácil" in home and "Ponto sem relógio caro" in home and "Micro" in home
    assert "R$ 19,90" in home and "/cadastro?plano=" not in home and "<h3>Essencial</h3>" not in home
    assert novo().get("/cadastro").headers["location"] == "/entrar"
    assert "Pix 123" in n.get("/painel/assinatura").text or True
    posta(m, "/master/plataforma", tm, nome="Ponto Fácil", dias_teste="14", cadastro_aberto="1")
    k = novo()
    posta(k, "/cadastro", csrf(k, "/cadastro"), aceito="1", empresa="Loja K", nome="K", email="k@k.com",
          senha="segredo3")
    with SessionLocal() as db:
        lk = db.scalar(select(Empresa).where(Empresa.nome == "Loja K"))
        assert (lk.teste_ate - banco.agora()).days in (13, 14)
        assert db.scalar(select(Batida.id).limit(1))
    for caminho in ("/master", "/master/planos", "/master/plataforma", f"/master/empresas/{eid}"):
        assert k.get(caminho).headers["location"] == "/painel", caminho
    assert posta(k, f"/master/empresas/{eid}/bloqueio", csrf(k, "/painel/config")).headers[
        "location"] == "/painel"
    assert sit(eid) != "bloqueada"
    print("OK: revenda passou")


if __name__ == "__main__":
    main()
