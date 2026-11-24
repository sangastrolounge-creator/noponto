"""Reconhecimento facial: termo, consentimento, cadastro do rosto e conferência na batida.
Rodar da raiz: .venv/Scripts/python tests/test_facial.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco, facial, push  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Alerta, Batida, Empresa, Historico, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_usuarios = lambda lista: avisos.extend(lista)


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def rosto(semente, ruido=0.0):
    """Um "rosto" de teste: 128 números com norma perto de 1, como os do face-api."""
    import math
    v = [math.sin(semente * (i + 1) * 0.37) + ruido * math.cos(i * 1.7) for i in range(128)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def bate(cli, tk, **extra):
    return cli.post("/app/ponto", json={"lat": -2.53, "lng": -44.3, "precisao": 10, **extra}, headers={"X-CSRF": tk})


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar Azul", nome="Gil", email="gil@b.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/equipe", tg, nome="Ana Souza", celular="98999990001")
    posta(g, "/painel/equipe", tg, nome="Bia Lima", celular="98999990002")
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: (u.id, u.convite_token) for u in db.scalars(select(Usuario)) if u.convite_token}

    def entra(nome):
        cli = novo()
        tok = ids[nome][1]
        posta(cli, f"/convite/{tok}", csrf(cli, f"/convite/{tok}"), senha="senha123", consentimento="1")
        return cli, csrf(cli, "/app")
    ana, ta = entra("Ana")
    bia, tb = entra("Bia")

    # ---- desligado: nada aparece; a batida não leva marca
    assert "Reconhecimento facial" not in ana.get("/app").text
    assert ana.get("/app/facial").headers["location"] == "/app"
    tela = g.get("/painel/config").text
    assert 'id="facial"' in tela and "/painel/config/termo-facial" in tela
    termo = g.get("/painel/config/termo-facial").text
    assert "Bar Azul" in termo and "art. 11, I" in termo and "Imprimir ou salvar em PDF" in termo

    # ---- liga: cada pessoa vê o chamado para ler o termo
    posta(g, "/painel/config/facial", tg, ativo="1")
    with SessionLocal() as db:
        assert db.get(Empresa, 1).facial_ativo
    assert "Sua empresa passou a oferecer o ponto com o rosto" in ana.get("/app").text
    t = ana.get("/app/facial").text
    assert "Li e aceito" in t and "Não aceito" in t and "Ana Souza" in t and "art. 18" in t
    assert "2 ainda" not in g.get("/painel/config").text and ">2</strong><span>ainda não responderam" in g.get("/painel/config").text

    # ---- cadastro só depois de aceitar; dados inválidos recusados
    r = ana.post("/app/facial/cadastro", json={"descritor": rosto(1)}, headers={"X-CSRF": ta})
    assert r.status_code == 400 and "Aceite o termo" in r.json()["erro"]
    posta(ana, "/app/facial/consentimento", ta, escolha="aceito")
    assert "Cadastre o seu rosto" in ana.get("/app").text
    for ruim in ([0.1] * 10, ["x"] * 128, [0.0] * 128, [5.0] * 128, None):
        r = ana.post("/app/facial/cadastro", json={"descritor": ruim}, headers={"X-CSRF": ta})
        assert r.status_code == 400, ruim
    r = ana.post("/app/facial/cadastro", json={"descritor": rosto(1)}, headers={"X-CSRF": ta})
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        a = db.get(Usuario, ids["Ana"][0])
        assert a.facial_consentimento == "aceito" and a.facial_consentido_em and len(a.facial_descritor) > 500
        assert db.scalar(select(Historico).where(Historico.detalhe == "Ana Souza aceitou o termo de reconhecimento facial."))
    tela = ana.get("/app").text
    assert "/static/facial.js" in tela and "var FACIAL = true" in tela and "Cadastre o seu rosto" not in tela
    assert "Testar reconhecimento" in ana.get("/app/facial").text
    r = ana.post("/app/facial/testar", json={"descritor": rosto(1, 0.05)}, headers={"X-CSRF": ta})
    assert r.status_code == 200 and r.json() == {"confere": True}
    r = ana.post("/app/facial/testar", json={"descritor": rosto(7)}, headers={"X-CSRF": ta})
    assert r.json() == {"confere": False}
    with SessionLocal() as db:   # testar não cria batida
        assert not db.scalar(select(Batida.id).where(Batida.usuario_id == ids["Ana"][0]))
    assert "Aceito eletronicamente por Ana Souza" in ana.get("/app/facial/termo").text

    # ---- Bia recusa: continua batendo sem rosto e sem marca
    posta(bia, "/app/facial/consentimento", tb, escolha="recusado")
    assert "var FACIAL = false" in bia.get("/app").text
    as_("08:00", SEG)
    assert bate(bia, tb).status_code == 200

    # ---- batidas da Ana: mesmo rosto (com variação) confere; outro rosto não confere; sem rosto fica marcado
    assert bate(ana, ta, facial=rosto(1, 0.05)).status_code == 200
    as_("12:00", SEG)
    assert bate(ana, ta, facial=rosto(7)).status_code == 200            # outra pessoa: registra e avisa
    as_("13:00", SEG)
    assert bate(ana, ta).status_code == 200                               # aparelho sem suporte
    with SessionLocal() as db:
        marcas = [(b.facial, b.facial_dist) for b in db.scalars(select(Batida).where(
            Batida.usuario_id == ids["Ana"][0]).order_by(Batida.momento))]
        assert [m[0] for m in marcas] == ["ok", "diverge", "sem"], marcas
        assert marcas[0][1] < facial.LIMIAR < marcas[1][1]
        assert db.scalar(select(Batida.facial).where(Batida.usuario_id == ids["Bia"][0])) == ""
        al = db.scalar(select(Alerta).where(Alerta.tipo == "facial"))
        assert al and "Ana Souza" in al.texto and "não conferiu" in al.texto
    aj = g.get(f"/painel/ponto/ajuste?de={SEG}&ate={SEG}").text
    assert "O rosto não conferiu nesta batida" in aj and "facial-diverge" in aj

    # ---- totem: depois do PIN, o tablet confere o rosto de quem cadastrou (Bia recusou: só PIN)
    posta(g, "/painel/totens", tg, nome="Tablet da entrada")
    link = re.search(r'value="(http[^"]+/totem/ativar/[^"]+)"', g.get("/painel/locais").text).group(1)
    tablet = novo()
    tablet.get(link.split("testserver")[-1])
    pag = tablet.get("/totem").text
    assert "/static/facial.js" in pag and "Toque no seu nome" in pag
    assert re.search(r'data-uid="%d"[^>]*data-facial="1"' % ids["Ana"][0], pag)
    assert re.search(r'data-uid="%d"[^>]*data-facial=""' % ids["Bia"][0], pag)
    posta(ana, "/app/pin", ta, senha="senha123", pin="4821")
    posta(bia, "/app/pin", tb, senha="senha123", pin="5937")
    tt = re.search(r'"X-CSRF": "([^"]+)"', pag).group(1)
    def no_totem(uid, pin, **extra):
        return tablet.post("/totem/ponto", json={"uid": uid, "pin": pin, **extra}, headers={"X-CSRF": tt})
    TER = SEG + timedelta(days=1)
    as_("09:00", TER)
    r = no_totem(ids["Ana"][0], "4821", facial=rosto(1, 0.05))
    assert r.status_code == 200 and r.json()["facial"] == "ok" and r.json()["titulo"] == "Ana", r.text
    as_("12:00", TER)
    r = no_totem(ids["Ana"][0], "4821", facial=rosto(7))
    assert r.json()["facial"] == "diverge" and "não conferiu" in r.json()["mensagem"], r.text
    as_("13:00", TER)
    assert no_totem(ids["Ana"][0], "4821").json()["facial"] == "sem"   # tablet sem suporte
    assert no_totem(ids["Bia"][0], "5937").json()["facial"] == ""
    with SessionLocal() as db:
        assert db.scalar(select(Alerta.id).where(Alerta.tipo == "facial", Alerta.texto.contains("12:00")))
    with SessionLocal() as db:   # empresa desliga: o totem volta a pedir só o PIN
        db.get(Empresa, 1).facial_ativo = False
        db.commit()
    assert "/static/facial.js" not in tablet.get("/totem").text
    with SessionLocal() as db:
        db.get(Empresa, 1).facial_ativo = True
        db.commit()

    # ---- gestor vê a situação e manda refazer; revogar apaga; desligar apaga
    eq = g.get("/painel/equipe").text
    assert "Rosto cadastrado" in eq and "Recusou o reconhecimento facial" in eq
    posta(g, f"/painel/equipe/{ids['Ana'][0]}/facial-refazer", tg)
    with SessionLocal() as db:
        assert db.get(Usuario, ids["Ana"][0]).facial_descritor == ""
    assert "Cadastre o seu rosto" in ana.get("/app").text
    ana.post("/app/facial/cadastro", json={"descritor": rosto(1)}, headers={"X-CSRF": ta})
    posta(ana, "/app/facial/revogar", ta)
    with SessionLocal() as db:
        a = db.get(Usuario, ids["Ana"][0])
        assert a.facial_descritor == "" and a.facial_consentimento == "recusado"
    posta(ana, "/app/facial/consentimento", ta, escolha="aceito")
    ana.post("/app/facial/cadastro", json={"descritor": rosto(1)}, headers={"X-CSRF": ta})
    posta(g, f"/painel/equipe/{ids['Ana'][0]}/desligar", tg, data=(SEG - timedelta(days=1)).isoformat(),
          tipo="pedido", aviso="trabalhado")
    with SessionLocal() as db:
        assert db.get(Usuario, ids["Ana"][0]).facial_descritor == ""

    # ---- plano sem reconhecimento facial: some do app e da configuração; a foto simples continua
    from app.db import Plano
    with SessionLocal() as db:
        pl = Plano(nome="Básico", preco_centavos=1000, limite_funcionarios=0, com_facial=False, com_foto=True)
        db.add(pl)
        db.flush()
        e = db.get(Empresa, 1)
        e.plano_id, e.teste_ate, e.pago_ate, e.isenta = pl.id, None, banco.agora() + timedelta(days=20), False
        e.exigir_foto = True
        db.commit()
    with SessionLocal() as db:   # sessão nova: a empresa já com o plano novo
        assert not facial.ligado(db.get(Empresa, 1))
    tela = g.get("/painel/config").text
    assert "O reconhecimento facial não está no seu plano." in tela and "Fora do seu plano" in tela
    posta(g, "/painel/config/facial", tg, ativo="1")
    assert "não está no seu plano" in g.get("/painel/config").text
    assert bia.get("/app/facial").headers["location"] == "/app"
    tela = bia.get("/app").text
    assert "var FACIAL = false" in tela and "var EXIGE_FOTO = true" in tela and "/static/facial.js" not in tela
    m = novo()   # o Master vê a opção no plano
    assert "Inclui reconhecimento facial no ponto" in open("app/templates/master/planos.html").read()

    # ---- a biblioteca e os modelos são servidos pelo próprio site
    for arq in ("/static/vendor/face/face-api.esm.js", "/static/vendor/face/model/face_recognition_model-weights_manifest.json",
                "/static/facial.js"):
        assert novo().get(arq).status_code == 200, arq

    print("OK: reconhecimento facial passou")


if __name__ == "__main__":
    main()
