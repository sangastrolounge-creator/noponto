"""Crescimento: pacote de planos, cupom, plano anual, indique e ganhe, faixas automáticas, recursos por
plano, programa de parceiros (comissão, resumo de horas, painel, master) e marca própria por domínio.
Asaas simulado. Rodar da raiz: python tests/test_crescimento.py"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com", "ASAAS_API_KEY": "teste"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import asaas, cobranca, core, crescimento, parceiros  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import (Comissao, Cupom, Empresa, Historico, Pagamento, Parceiro, Plano,  # noqa: E402
                    Plataforma, SessionLocal, Usuario)
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 20, 13, 0)}
banco.agora = lambda: relogio["agora"]
chamadas = []
estado = {"n": 0, "valor": 0}


def _id(prefixo):
    estado["n"] += 1
    return f"{prefixo}_{estado['n']}"


def _cobranca(cliente, centavos, descricao, ref, retorno=None):
    pid = _id("pay")
    chamadas.append(("cobranca", centavos, descricao))
    return {"id": pid, "invoiceUrl": f"https://asaas.test/i/{pid}"}


def _assinatura(cliente, centavos, descricao, ref, primeiro=None, retorno=None, ciclo="MONTHLY"):
    chamadas.append(("assinatura", centavos, ciclo))
    estado["valor"] = centavos
    pid = _id("pay")
    return {"id": _id("sub"), "primeira": {"id": pid, "invoiceUrl": f"https://asaas.test/i/{pid}"}}


def _mudar_valor(sid, centavos):
    chamadas.append(("valor", sid, centavos))
    estado["valor"] = centavos


asaas.criar_cliente = lambda nome, email, doc, ref: _id("cus")
asaas.criar_cobranca = _cobranca
asaas.criar_assinatura = _assinatura
asaas.mudar_valor_assinatura = _mudar_valor
asaas.valor_assinatura = lambda sid: estado["valor"]
asaas.consultar_cobranca = lambda pid: {"id": pid, "status": "PENDING"}
asaas.adiar_assinatura = lambda sid, meses=1: chamadas.append(("adiar", sid, meses)) or "2027-01-20"


def novo(host="testserver"):
    return TestClient(app, follow_redirects=False, base_url=f"http://{host}")


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def cadastrar(cli, empresa, email, **extra):
    r = cli.post("/cadastro", data={"csrf": csrf(cli, "/cadastro"), "aceito": "1", "empresa": empresa,
                                    "nome": "Gestor " + empresa, "email": email, "senha": "segredo1", **extra})
    assert r.status_code == 303, r.text[:500]
    with SessionLocal() as db:
        return db.scalar(select(Empresa).where(Empresa.nome == empresa)).id


def pagar_e_confirmar(cli, plano_id, modo="avulso", ciclo="mensal"):
    t = csrf(cli, "/painel/assinatura")
    r = cli.post("/painel/assinatura/pagar", data={"csrf": t, "plano": plano_id, "cpf_cnpj": "11.444.777/0001-61",
                                                   "modo": modo, "ciclo": ciclo})
    assert r.status_code == 303 and r.headers["location"].startswith("https://asaas.test/i/"), r.headers
    pid = r.headers["location"].rsplit("/", 1)[1]
    with SessionLocal() as db:
        reg = db.scalar(select(Pagamento).where(Pagamento.asaas_id == pid))
        assert cobranca.confirmar_pagamento(db, {"id": pid, "value": reg.valor_centavos / 100,
                                                 "billingType": "PIX"})
        return reg.valor_centavos, reg.dias


def main():
    # ---- valor por pessoa com desconto por volume
    p = Plano(nome="V", preco_centavos=0, por_pessoa_centavos=1000, minimo_centavos=0, descontos_volume="50:15;150:30")
    assert p.valor_mensal(10) == 10000 and p.valor_mensal(60) == 51000 and p.valor_mensal(200) == 140000

    # ---- master aplica o pacote recomendado
    m = novo()
    cadastrar(m, "Plataforma", "dono@plataforma.com")
    r = m.post("/master/pacote-planos", data={"csrf": csrf(m, "/master/planos")})
    assert r.status_code == 303
    with SessionLocal() as db:
        venda = list(db.scalars(select(Plano).where(Plano.ativo.is_(True)).order_by(Plano.ordem)))
        assert [x.nome for x in venda] == ["Iniciante", "Essencial", "Profissional", "Empresarial", "Ilimitado"]
        ids = {x.nome: x.id for x in venda}
        ini = db.get(Plano, ids["Iniciante"])
        assert ini.gratis and not ini.com_fechamento and ini.com_escala and ini.limite_efetivo == 12
        assert db.scalar(select(Cupom).where(Cupom.codigo == "FUNDADORES")).pct == 30
        assert db.get(Plataforma, 1).dias_teste == 14
        faixas = list(db.scalars(select(Plano).where(Plano.grupo == "por-pessoa")))
        assert len(faixas) == 5 and not any(f.ativo for f in faixas)
    assert "Cupons" in m.get("/master/cupons").text and "FUNDADORES" in m.get("/master/cupons").text
    assert "R$ 599,90" in novo().get("/").text

    # ---- empresa A: chega pelo link do cupom, cupom aplicado no cadastro
    a = novo()
    a.get("/cupom/fundadores")
    assert 'value="FUNDADORES"' in a.get("/cadastro").text
    aid = cadastrar(a, "Bar do Ze", "ze@bar.com")
    with SessionLocal() as db:
        e = db.get(Empresa, aid)
        assert e.cupom_id and core.situacao(e) == "teste"
        assert e.cupom_desde == e.teste_ate          # o prazo do cupom conta a partir do fim do teste
        assert db.scalar(select(Cupom).where(Cupom.codigo == "FUNDADORES")).usos == 1
        assert cobranca.valor_atual(db, e, db.get(Plano, ids["Essencial"]))[0] == 4193   # 59,90 - 30%
    t = a.get("/painel/assinatura").text
    assert "R$ 41,93/mês" in t and "ou R$ 419,30 por ano" in t and "Indique e ganhe" in t and "Anual: pague 10 meses" in t
    codigo_a = re.search(r'/i/([a-z0-9]+)', t).group(1)

    # ---- plano anual, cobrança automática: o Asaas recebe ciclo anual e 10 mensalidades
    t2 = csrf(a, "/painel/assinatura")
    r = a.post("/painel/assinatura/pagar", data={"csrf": t2, "plano": ids["Essencial"], "cpf_cnpj": "11.444.777/0001-61",
                                                 "modo": "automatica", "ciclo": "anual"})
    assert r.status_code == 303 and ("assinatura", 41930, "YEARLY") in chamadas, chamadas
    with SessionLocal() as db:
        e = db.get(Empresa, aid)
        assert e.ciclo == "anual" and e.valor_assinatura_centavos == 41930
        sub = e.asaas_assinatura_id
        reg = db.scalar(select(Pagamento).where(Pagamento.empresa_id == aid, Pagamento.status == "pendente"))
        assert reg.dias == 365
        cobranca.confirmar_pagamento(db, {"id": reg.asaas_id, "value": 419.30, "billingType": "PIX",
                                          "subscription": sub, "dueDate": "2026-12-04"})
        e = db.get(Empresa, aid)
        assert e.pago_ate.date().isoformat() == "2027-12-04" and core.situacao(e) == "ativa"

    # ---- empresa B indicada por A: paga avulso anual; os dois ganham 1 mês
    b = novo()
    b.get(f"/i/{codigo_a}")
    bid = cadastrar(b, "Padaria Sol", "sol@padaria.com")
    with SessionLocal() as db:
        assert db.get(Empresa, bid).indicada_por_id == aid
        antes_a = db.get(Empresa, aid).pago_ate
    valor, dias = pagar_e_confirmar(b, ids["Profissional"], "avulso", "anual")
    assert (valor, dias) == (139900, 365)
    with SessionLocal() as db:
        eb, ea = db.get(Empresa, bid), db.get(Empresa, aid)
        assert eb.indicacao_premiada
        assert ea.pago_ate == antes_a + timedelta(days=30)
        assert ("adiar", ea.asaas_assinatura_id, 1) in chamadas       # A tem cobrança automática: adiada
        assert eb.pago_ate == relogio["agora"] + timedelta(days=365 + 30)
        creditos = list(db.scalars(select(Pagamento).where(Pagamento.tipo == "credito")))
        assert len(creditos) == 2
    with SessionLocal() as db:   # pagar de novo não dá outro mês
        crescimento.premiar_indicacao(db, db.get(Empresa, bid))
        assert len(list(db.scalars(select(Pagamento).where(Pagamento.tipo == "credito")))) == 2
    assert "1 empresa(s) indicada(s) · 1 já pagaram e deram o mês grátis" in a.get("/painel/assinatura").text

    # ---- recursos por plano: Iniciante sem fechamento nem gerentes
    with SessionLocal() as db:
        e = db.get(Empresa, bid)
        e.plano, e.teste_ate, e.pago_ate = db.get(Plano, ids["Iniciante"]), None, None
        db.commit()
        assert core.situacao(db.get(Empresa, bid)) == "gratis"
    r = b.get("/painel/fechamento")
    assert r.status_code == 303 and r.headers["location"] == "/painel/assinatura"
    assert "não inclui o fechamento" in b.get("/painel/assinatura").text
    assert b.get("/painel/setores").headers["location"] == "/painel/assinatura"
    assert b.get("/painel/escala").status_code == 200
    with SessionLocal() as db:
        e = db.get(Empresa, bid)
        e.plano, e.pago_ate = db.get(Plano, ids["Ilimitado"]), relogio["agora"] + timedelta(days=30)
        db.commit()
    assert b.get("/painel/fechamento").status_code == 200

    # ---- faixas por pessoa: a empresa vai sozinha para a faixa certa
    pessoas_original = crescimento.pessoas
    with SessionLocal() as db:
        fx = {f.limite_funcionarios: f for f in db.scalars(select(Plano).where(Plano.grupo == "por-pessoa"))}
        for f in fx.values():
            f.ativo = True
        e = db.get(Empresa, bid)
        e.plano = fx[10]
        db.commit()
        e = db.get(Empresa, bid)
        crescimento.pessoas = lambda db_, e_, p_: 25
        valor, n, faixa = crescimento.calcular(db, e, e.plano)
        assert (valor, n, faixa.limite_funcionarios) == (14750, 25, 30)       # 25 × 5,90
        crescimento.pessoas = lambda db_, e_, p_: 30
        assert crescimento.calcular(db, e, e.plano)[0] == 15990             # nunca mais que a faixa seguinte
        assert core.limite_funcionarios(e) == 0                              # a última faixa é ilimitada
        e.asaas_assinatura_id, e.valor_assinatura_centavos, e.ciclo = "sub_faixa", 5520, "mensal"
        db.commit()
        estado["valor"] = 5520
        crescimento.pessoas = lambda db_, e_, p_: 25
        assert cobranca.ajustar_valores(db, forcar=True) >= 1
        e = db.get(Empresa, bid)
        assert e.plano.limite_funcionarios == 30 and ("valor", "sub_faixa", 14750) in chamadas
        assert db.scalar(select(Historico).where(Historico.empresa_id == bid, Historico.acao == "faixa"))
        e.asaas_assinatura_id = ""
        db.commit()
    crescimento.pessoas = pessoas_original

    # ---- programa de parceiros
    pc = novo()
    r = pc.post("/parceiros", data={"csrf": csrf(pc, "/parceiros"), "nome": "Carla Contadora",
                                    "escritorio": "Contabil Norte", "email": "carla@contabil.com",
                                    "celular": "98999990000", "senha": "segredo123", "aceito": "1"})
    assert r.headers["location"] == "/parceiro"
    t = pc.get("/parceiro").text
    assert "Indicador" in t and "15% de comissão" in t
    with SessionLocal() as db:
        par = db.scalar(select(Parceiro).where(Parceiro.email == "carla@contabil.com"))
        pid, pcod = par.id, par.codigo
    assert f"/p/{pcod}" in t

    c = novo()
    assert c.get(f"/p/{pcod}").headers["location"] == "/cadastro"
    assert "Você chegou pelo parceiro Contabil Norte" in c.get("/cadastro").text
    cid = cadastrar(c, "Restaurante Mar", "mar@rest.com", contador="1")
    with SessionLocal() as db:
        e = db.get(Empresa, cid)
        assert e.parceiro_id == pid and e.contador_acesso
    valor, _ = pagar_e_confirmar(c, ids["Profissional"])
    assert valor == 13990
    with SessionLocal() as db:
        com = db.scalar(select(Comissao).where(Comissao.parceiro_id == pid))
        assert (com.pct, com.valor_centavos, com.status) == (15, 2098, "a_pagar")
    t = pc.get("/parceiro").text
    assert "Restaurante Mar" in t and "R$ 20,98" in t and "Clientes pagantes" in t
    mes = relogio["agora"].strftime("%Y-%m")
    r = pc.get(f"/parceiro/empresa/{cid}/horas.csv?mes={mes}")
    assert r.status_code == 200 and "Horas trabalhadas" in r.text
    assert pc.get(f"/parceiro/empresa/{aid}/horas.csv").status_code == 403     # não é cliente dela
    # a empresa desliga o acesso do contador
    tc = csrf(c, "/painel/config")
    assert "Contador parceiro" in c.get("/painel/config").text
    c.post("/painel/config/contador", data={"csrf": tc})
    assert pc.get(f"/parceiro/empresa/{cid}/horas.csv").status_code == 403
    # empresa A se liga ao contador pelo código
    ta = csrf(a, "/painel/config")
    a.post("/painel/config/contador", data={"csrf": ta, "codigo": pcod, "acesso": "1"})
    with SessionLocal() as db:
        assert db.get(Empresa, aid).parceiro_id == pid
    # master: vê o parceiro e marca as comissões como pagas
    assert "Carla Contadora" in m.get("/master/parceiros").text
    m.post(f"/master/parceiros/{pid}/pagar", data={"csrf": csrf(m, "/master/parceiros")})
    with SessionLocal() as db:
        assert db.scalar(select(Comissao).where(Comissao.parceiro_id == pid)).status == "paga"
    # estorno de comissão ainda não paga
    with SessionLocal() as db:
        reg = Pagamento(empresa_id=cid, tipo="avulso", status="pago", valor_centavos=10000, asaas_id="pay_x",
                        criado_em=relogio["agora"])
        db.add(reg)
        db.commit()
        parceiros.registrar_comissao(db, db.get(Empresa, cid), reg)
        parceiros.estornar(db, reg)
        db.commit()
        assert db.scalar(select(Comissao).where(Comissao.pagamento_id == reg.id)).status == "cancelada"

    # ---- marca própria por domínio
    m.post(f"/master/parceiros/{pid}", data={"csrf": csrf(m, "/master/parceiros"), "ativo": "1",
                                             "marca_liberada": "1", "dominio": "ponto.contabilnorte.com.br",
                                             "nivel_manual": "5"})
    tp = csrf(pc, "/parceiro")
    r = pc.post("/parceiro/marca", data={"csrf": tp, "marca_nome": "Norte Ponto", "marca_cor": "#aa3300",
                                         "dominio": "ponto.contabilnorte.com.br"},
                files={"arquivo": ("logo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")})
    assert r.status_code == 303
    with SessionLocal() as db:
        logo = db.get(Parceiro, pid).marca_logo
        assert logo.endswith(".png")
    assert novo().get(f"/marca-logo/{logo}").status_code == 200
    w = novo("ponto.contabilnorte.com.br")
    t = w.get("/entrar").text
    assert "Norte Ponto" in t and "#aa3300" in t and "/entrar/google" not in t
    wid = cadastrar(w, "Loja Norte", "loja@norte.com")
    with SessionLocal() as db:
        assert db.get(Empresa, wid).parceiro_id == pid
        assert parceiros.nivel(db, db.get(Parceiro, pid))[1] == "Marca Própria"
    assert "Norte Ponto" not in novo().get("/entrar").text

    # ---- login e saída do parceiro
    pc.post("/parceiros/sair", data={"csrf": csrf(pc, "/parceiro")})
    assert pc.get("/parceiro").headers["location"] == "/parceiros/entrar"
    r = pc.post("/parceiros/entrar", data={"csrf": csrf(pc, "/parceiros/entrar"), "email": "carla@contabil.com",
                                           "senha": "errada"})
    assert r.status_code == 400
    r = pc.post("/parceiros/entrar", data={"csrf": csrf(pc, "/parceiros/entrar"), "email": "carla@contabil.com",
                                           "senha": "segredo123"})
    assert r.headers["location"] == "/parceiro"
    print("OK: crescimento (pacote, cupom, anual, indicação, faixas, recursos, parceiros, marca própria) passou")


if __name__ == "__main__":
    main()
