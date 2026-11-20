"""Turnos abertos e trocas, mural da equipe, divisão de gorjetas e plano grátis.
Rodar da raiz: .venv/Scripts/python tests/test_equipe.py"""
import json
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
os.environ["IDIOMA_PADRAO"] = "pt"  # as conferências dos testes são em português
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL", "STRIPE_SECRET_KEY"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import push  # noqa: E402
from app.core import situacao  # noqa: E402
from app.db import (Empresa, FolhaItem, Local, Mensagem, Oferta, Plano,  # noqa: E402
                    SessionLocal, Turno, Usuario)
from app.main import app  # noqa: E402

DOM = date(2026, 11, 15)
SEG, TER, QUA, QUI, SEX, SAB = (DOM + timedelta(days=i) for i in range(1, 7))
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_usuarios = lambda lista: avisos.extend(lista)
push.avisar_gestores = lambda *a: avisos.append(a)


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def flash(cli, caminho):
    m = re.search(r'class="aviso[^"]*">([^<]+)<', cli.get(caminho).text)
    return m.group(1) if m else ""


def turno(uid, d):
    with SessionLocal() as db:
        t = db.scalar(select(Turno).where(Turno.usuario_id == uid, Turno.data == d))
        return (t.folga, t.inicio, t.fim, t.obs) if t else None


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Taco Shop", nome="Gil Gestor",
          email="gil@taco.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Taco Shop", lat="25.77", lng="-80.19", raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", estado="FL", semana_inicio="6", frequencia="weekly",
          periodo_ancora=DOM.isoformat(), intervalo_padrao_min="30", tolerancia_dia_min="7",
          pct_noturno="0", pct_feriado="0")
    for nome, cel, cargo in (("Ana Souza", "3055550001", "Server"), ("Bruno Lima", "3055550002", "Server"),
                             ("Caio Reis", "3055550003", "Server"), ("Duda Melo", "3055550004", "Cook")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel, cargo=cargo)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        convites = {u.id: u.convite_token for u in db.scalars(select(Usuario)) if u.convite_token}
    cli = {}
    for nome in ("Ana", "Bruno", "Caio", "Duda"):
        c = novo()
        caminho = f"/convite/{convites[ids[nome]]}"
        assert posta(c, caminho, csrf(c, caminho), senha="senha123", consentimento="1").status_code == 303
        cli[nome] = (c, csrf(c, "/app"))

    # escala: Bruno segunda a sexta 8h-17h com 1 h de intervalo (40 h); Ana segunda e quarta
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Bruno"], data=SEG.isoformat(),
          modo="trabalho", inicio="08:00", fim="17:00", intervalo="60", repetir="uteis")
    for d, ini, fim in ((SEG, "08:00", "12:00"), (QUA, "08:00", "17:00")):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=d.isoformat(),
              modo="trabalho", inicio=ini, fim=fim, intervalo="0", repetir="dia")
    assert turno(ids["Ana"], QUA)[1:3] == ("08:00", "17:00")

    # ================================================= turno aberto (cargo Server)
    r = posta(g, "/painel/ofertas", tg, data=SAB.isoformat(), inicio="08:00", fim="13:00",
              intervalo="0", cargo="Server", quantos="1", obs="brunch")
    assert r.status_code == 303
    assert "3 pessoa(s)" in flash(g, "/painel/escala")
    assert sorted(a[0] for a in avisos if a[1] == "Turno disponível") == sorted(
        [ids["Ana"], ids["Bruno"], ids["Caio"]])   # a cozinheira não é avisada
    duda, td = cli["Duda"]
    assert "Turnos disponíveis" not in duda.get("/app/escala").text
    with SessionLocal() as db:
        oid = db.scalar(select(Oferta.id))
    posta(duda, f"/app/ofertas/{oid}", td, acao="pedir")
    assert "cargo Server" in flash(duda, "/app/escala")
    bruno, tb = cli["Bruno"]
    tela = bruno.get("/app/escala").text
    assert "Turnos disponíveis" in tela and "brunch" in tela
    posta(bruno, f"/app/ofertas/{oid}", tb, acao="pedir")
    assert "Pedido enviado" in flash(bruno, "/app/escala")
    assert "Aguardando o gestor" in bruno.get("/app/escala").text
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert "1 para aprovar" in grade and "45h00 na semana: 1h00 além das 44 horas" in grade, grade[:0]
    posta(g, f"/painel/ofertas/{oid}", tg, acao="aprovar")
    assert turno(ids["Bruno"], SAB)[1:3] == ("08:00", "13:00")
    with SessionLocal() as db:
        assert db.get(Oferta, oid).situacao == "aprovada"
    assert any(a[0] == ids["Bruno"] and a[1] == "Turno aprovado" for a in avisos if len(a) == 4)

    # ================================================= Ana repassa a quarta
    ana, ta = cli["Ana"]
    with SessionLocal() as db:
        tid = db.scalar(select(Turno.id).where(Turno.usuario_id == ids["Ana"], Turno.data == QUA))
    assert "Não posso ir: oferecer" in ana.get("/app/escala").text
    posta(ana, f"/app/turnos/{tid}/oferecer", ta)
    assert "Turno oferecido" in flash(ana, "/app/escala")
    posta(ana, f"/app/turnos/{tid}/oferecer", ta)
    assert "já está oferecido" in flash(ana, "/app/escala")
    with SessionLocal() as db:
        rid = db.scalar(select(Oferta.id).where(Oferta.tipo == "repasse"))
    posta(bruno, f"/app/ofertas/{rid}", tb, acao="pedir")
    assert "já trabalha neste dia" in flash(bruno, "/app/escala")
    caio, tc = cli["Caio"]
    assert "Cobrir Ana" in caio.get("/app/escala").text
    posta(caio, f"/app/ofertas/{rid}", tc, acao="pedir")
    posta(g, f"/painel/ofertas/{rid}", tg, acao="recusar")   # gestor recusa: volta a ficar aberta
    with SessionLocal() as db:
        o = db.get(Oferta, rid)
        assert o.situacao == "aberta" and o.candidato_id is None
    posta(caio, f"/app/ofertas/{rid}", tc, acao="pedir")
    posta(g, f"/painel/ofertas/{rid}", tg, acao="aprovar")
    assert turno(ids["Caio"], QUA)[1:3] == ("08:00", "17:00")
    folga = turno(ids["Ana"], QUA)
    assert folga[0] and "Caio" in folga[3], folga
    assert "Folga" in ana.get("/app/escala").text

    # ================================================= sem aprovação: vale na hora
    posta(g, "/painel/ofertas/regra", tg, aprovacao="")
    posta(g, "/painel/ofertas", tg, data=SEX.isoformat(), inicio="18:00", fim="22:00",
          intervalo="0", cargo="", quantos="2")
    with SessionLocal() as db:
        abertas = list(db.scalars(select(Oferta.id).where(Oferta.data == SEX)))
    assert len(abertas) == 2
    posta(duda, f"/app/ofertas/{abertas[0]}", td, acao="pedir")
    assert "agora é seu" in flash(duda, "/app/escala")
    assert turno(ids["Duda"], SEX)[1:3] == ("18:00", "22:00")
    posta(duda, f"/app/ofertas/{abertas[1]}", td, acao="pedir")   # a segunda vaga, no mesmo dia
    assert "já trabalha neste dia" in flash(duda, "/app/escala")
    posta(g, f"/painel/ofertas/{abertas[1]}", tg, acao="cancelar")
    with SessionLocal() as db:
        assert db.get(Oferta, abertas[1]).situacao == "cancelada"
    # turno que já começou não pode ser oferecido
    as_("09:00", SEG)
    with SessionLocal() as db:
        hoje_ana = db.scalar(select(Turno.id).where(Turno.usuario_id == ids["Ana"], Turno.data == SEG))
    posta(ana, f"/app/turnos/{hoje_ana}/oferecer", ta)
    assert "ainda não começou" in flash(ana, "/app/escala")

    # ================================================= mural
    posta(g, "/painel/mural", tg, texto="Reunião sábado às 9h.", notificar="1", fixar="1")
    assert sum(1 for a in avisos if len(a) == 4 and a[1] == "Aviso de Gil") == 4
    assert 'class="selo-num"' in ana.get("/app").text
    tela = ana.get("/app/mural").text
    assert "Reunião sábado às 9h." in tela and "Fixada" in tela
    assert 'class="selo-num"' not in ana.get("/app").text   # já viu
    posta(ana, "/app/mural", ta, texto="Posso levar o bolo!")
    assert any(a[1] == "Mural: Ana" for a in avisos if len(a) >= 4 and isinstance(a[0], int) and a[0] == 1)
    with SessionLocal() as db:
        mid = db.scalar(select(Mensagem.id).where(Mensagem.autor_id == ids["Ana"]))
    posta(bruno, f"/app/mural/{mid}/apagar", tb)   # não é dele: nada acontece
    with SessionLocal() as db:
        assert db.get(Mensagem, mid)
    posta(g, "/painel/mural/regra", tg, equipe="")
    posta(bruno, "/app/mural", tb, texto="oi")
    assert "Só o gestor" in flash(bruno, "/app/mural")
    posta(ana, f"/app/mural/{mid}/apagar", ta)
    with SessionLocal() as db:
        assert not db.get(Mensagem, mid)
        assert db.scalar(select(Mensagem.texto)) == "Reunião sábado às 9h."

    # ================================================= gorjetas divididas por horas
    as_("08:00", DOM + timedelta(days=8))
    for nome, ent, sai in (("Ana", "08:00", "12:00"), ("Bruno", "08:00", "16:00")):
        for tipo, hora in (("entrada", ent), ("saida", sai)):
            posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo,
                  data=SEG.isoformat(), hora=hora, obs="t")
    dados = {}
    for nome in ("Ana", "Bruno", "Caio", "Duda"):
        dados[f"taxa_{ids[nome]}"] = "10.00"
        dados[f"sal_{ids[nome]}"] = ""
    posta(g, "/painel/pagamento/dados", tg, **dados)
    with SessionLocal() as db:     # "recebe gorjeta" vem da ficha
        for nome in ("Ana", "Bruno"):
            db.get(Usuario, ids[nome]).gorjeta = True
        db.commit()
    posta(g, "/painel/fechamento", tg, de=DOM.isoformat(), ate=SAB.isoformat())
    fech = re.search(r'<option value="(\d+)">15/11/2026 a 21/11/2026', g.get("/painel/pagamento").text).group(1)
    url = posta(g, "/painel/pagamento/gerar", tg, fechamento_id=fech).headers["location"]
    fid = int(url.rsplit("/", 1)[1])
    tela = g.get(url).text
    assert "Rateio de gorjetas" in tela
    with SessionLocal() as db:
        itens = {i.usuario.nome.split()[0]: i.id for i in db.scalars(select(FolhaItem))}
    assert re.search(rf'name="p_{itens["Ana"]}" value="1" checked', tela)
    assert not re.search(rf'name="p_{itens["Caio"]}" value="1" checked', tela)
    with SessionLocal() as db:
        antes = {n: db.get(FolhaItem, itens[n]).bruto for n in ("Ana", "Bruno")}
    posta(g, f"/painel/pagamento/{fid}/gorjetas", tg, total="100,00",
          modo="horas", descricao="Rateio", **{f"p_{itens['Ana']}": "1", f"p_{itens['Bruno']}": "1"})
    assert "divididos entre 2 pessoa(s)" in flash(g, url)
    with SessionLocal() as db:
        a, b = db.get(FolhaItem, itens["Ana"]), db.get(FolhaItem, itens["Bruno"])
        la, lb = json.loads(a.lancamentos), json.loads(b.lancamentos)
        assert la == [{"descricao": "Rateio", "tipo": "p", "centavos": 3333, "tributavel": True}], la
        assert lb[0]["centavos"] == 6667
        assert a.bruto >= antes["Ana"] + 3333 and b.bruto >= antes["Bruno"] + 6667, (a.bruto, b.bruto)
    posta(g, f"/painel/pagamento/{fid}/gorjetas", tg, total="30,00",
          modo="igual", **{f"p_{itens['Caio']}": "1", f"p_{itens['Duda']}": "1",
                           f"p_{itens['Ana']}": "1"})
    with SessionLocal() as db:   # Caio e Duda não trabalharam, mas em partes iguais recebem
        assert json.loads(db.get(FolhaItem, itens["Caio"]).lancamentos)[0]["centavos"] == 1000
    posta(g, f"/painel/pagamento/{fid}/gorjetas", tg, total="10,00", modo="horas",
          **{f"p_{itens['Caio']}": "1"})
    assert "Ninguém marcado tem horas" in flash(g, url)

    # ================================================= plano grátis
    with SessionLocal() as db:
        free = db.scalar(select(Plano).where(Plano.preco_centavos == 0))
        assert free and free.nome == "Grátis" and free.limite_funcionarios == 10 and free.limite_locais == 1
        assert not free.com_pagamento and not free.ativo
        free.ativo = True
        e = db.get(Empresa, 1)
        assert situacao(e) == "cortesia"   # a primeira conta é a do dono: tira a cortesia
        e.isenta = False
        db.commit()
        assert situacao(db.get(Empresa, 1)) == "vencida"   # o teste de 7 dias acabou
    tela = g.get("/painel/assinatura").text
    assert "Ou continue de graça" in tela and "Usar o plano Grátis" in tela
    assert "Grátis" in novo().get("/").text and "Começar grátis" in novo().get("/").text
    posta(g, "/painel/assinatura/gratis", tg)
    with SessionLocal() as db:
        e = db.get(Empresa, 1)
        assert e.plano.nome == "Grátis" and situacao(e) == "gratis"
    assert "A folha de pagamento não está no seu plano" in g.get("/painel/pagamento").text
    r = g.get(url)
    assert r.status_code == 303 and r.headers["location"] == "/painel/pagamento"
    posta(g, "/painel/locais", tg, nome="Food truck", lat="25.78", lng="-80.20", raio_m="100")
    assert "permite 1 local(is)" in flash(g, "/painel/locais")
    with SessionLocal() as db:
        assert not db.scalar(select(Local.id))
    # muito tempo depois o plano grátis continua valendo: o painel abre normalmente
    as_("08:00", DOM + timedelta(days=400))
    r = g.get("/painel/escala")
    assert r.status_code == 200, r.headers.get("location")
    assert "Grátis" in g.get("/painel/assinatura").text

    # ================================================= alertas de atraso/falta conforme o plano
    from app import regras
    from app.db import Alerta
    dia = DOM + timedelta(days=401)
    posta(g, "/painel/escala/turno", tg, usuario_id=ids["Ana"], data=dia.isoformat(),
          modo="trabalho", inicio="09:00", fim="13:00", intervalo="0", repetir="dia")
    with SessionLocal() as db:
        db.scalar(select(Plano).where(Plano.preco_centavos == 0)).com_alertas = False
        db.commit()
    as_("09:30", dia)
    with SessionLocal() as db:
        novos, _ = regras.verificar_turnos(db)
        assert not [a for a in novos if a.tipo == "sem_ponto"]
    assert "não estão no seu plano" in g.get("/painel/avisos").text
    with SessionLocal() as db:
        db.scalar(select(Plano).where(Plano.preco_centavos == 0)).com_alertas = True
        db.commit()
    with SessionLocal() as db:
        novos, _ = regras.verificar_turnos(db)
        assert [a.usuario_id for a in novos if a.tipo == "sem_ponto"] == [ids["Ana"]]
    assert "não estão no seu plano" not in g.get("/painel/avisos").text
    tm = g.get("/master/planos").text
    assert "Inclui alertas de atraso e de não comparecimento" in tm

    # ================================================= GPS e foto conforme o plano
    assert "Inclui ponto por GPS" in tm and "Inclui foto no ponto" in tm
    from app.db import Batida
    with SessionLocal() as db:
        free = db.scalar(select(Plano).where(Plano.preco_centavos == 0))
        free.com_gps = free.com_foto = False
        db.get(Empresa, 1).exigir_foto = True
        db.commit()
    tela = ana.get("/app").text
    assert "var USA_GPS = false" in tela and "var EXIGE_FOTO = false" in tela
    assert "não está no seu plano" in g.get("/painel/config").text
    assert "o ponto é batido no totem" in tela and 'id="bater" class="botao-ponto " hidden' in tela
    as_("09:35", dia)
    # sem GPS no plano o celular não bate ponto, nem mandando a localização: só pelo totem
    r = ana.post("/app/ponto", json={}, headers={"X-CSRF": ta})
    assert r.status_code == 400 and "totem" in r.json()["erro"], r.text
    r = ana.post("/app/ponto", json={"lat": 25.77, "lng": -80.19, "precisao": 10}, headers={"X-CSRF": ta})
    assert r.status_code == 400 and "totem" in r.json()["erro"], r.text
    posta(g, "/painel/totens", tg, nome="Tablet do balcão", local_id="")
    tela = g.get("/painel/locais").text
    assert "1 de 1" in tela and "Criar totem" not in tela   # um totem por local
    posta(g, "/painel/totens", tg, nome="Segundo tablet", local_id="")
    assert "até 1 totem(ns)" in flash(g, "/painel/locais")
    from app.db import Totem
    with SessionLocal() as db:
        codigo = regras.codigo_totem(db.scalar(select(Totem)))
    r = ana.post("/app/ponto", json={"codigo": codigo}, headers={"X-CSRF": ta})
    assert r.status_code == 200 and r.json()["tipo"] == "entrada", r.text
    with SessionLocal() as db:
        b = db.scalar(select(Batida).order_by(Batida.id.desc()))
        assert b.origem == "codigo" and b.lat is None and not b.foto
    with SessionLocal() as db:
        free = db.scalar(select(Plano).where(Plano.preco_centavos == 0))
        free.com_gps = True
        db.commit()
    assert "var USA_GPS = true" in ana.get("/app").text
    as_("12:00", dia)
    r = ana.post("/app/ponto", json={}, headers={"X-CSRF": ta})
    assert r.status_code == 400 and "localização" in r.json()["erro"], r.text
    r = ana.post("/app/ponto", json={"lat": 26.5, "lng": -80.19, "precisao": 10}, headers={"X-CSRF": ta})
    assert r.status_code == 400 and "Você está a" in r.json()["erro"], r.text
    print("OK: equipe (turnos abertos, trocas, mural, gorjetas, plano grátis) passou")


if __name__ == "__main__":
    main()
