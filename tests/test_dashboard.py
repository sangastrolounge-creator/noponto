"""Tela Hoje como painel (gráficos, atalhos, banners do Master), página de Avisos,
nome curto do app e menu Escala sem submenus.
Rodar da raiz: .venv/Scripts/python tests/test_dashboard.py"""
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
os.environ.pop("ADMIN_EMAIL", None)
os.environ.pop("PUBLIC_URL", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import dashboard  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Alerta, Banner, SessionLocal, Turno, Usuario  # noqa: E402
from app.main import app  # noqa: E402

HOJE = date(2026, 11, 18)
relogio = {"agora": datetime(2026, 11, 18, 13, 0)}     # 10:00 em Brasília
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def main():
    # geometria pura do gráfico de barras
    g = dashboard.barras([dict(rotulo="Seg", valor=0), dict(rotulo="Ter", valor=7.5)])
    assert g["barras"][0]["caminho"] == "" and g["barras"][1]["caminho"].startswith("M")
    assert g["grade"][0]["valor"] == 0 and g["grade"][-1]["valor"] >= 7.5
    assert dashboard._passo(23) == 10 and dashboard._passo(0) == 1

    m = novo()        # o primeiro cadastro é o Master
    posta(m, "/cadastro", csrf(m, "/cadastro"), aceito="1", empresa="Bar do Zé", nome="Zé",
          email="ze@bar.com", senha="segredo1")
    tm = csrf(m, "/painel/config")
    posta(m, "/painel/equipe", tm, nome="Ana Souza", celular="98999990001")
    with SessionLocal() as db:
        ana = db.scalar(select(Usuario).where(Usuario.papel == "funcionario"))
        for k in range(7):
            db.add(Turno(empresa_id=1, usuario_id=ana.id, data=HOJE - timedelta(days=k),
                         inicio="09:00", fim="17:00", intervalo_min=60))
        db.add(Alerta(empresa_id=1, usuario_id=ana.id, tipo="sem_ponto", texto="Ana Souza não bateu o ponto", criado_em=relogio["agora"]))
        db.commit()
    posta(m, "/painel/ponto/manual", tm, usuario_id=ana.id, tipo="entrada",
          data=(HOJE - timedelta(days=1)).isoformat(), hora="09:00", obs="x")
    posta(m, "/painel/ponto/manual", tm, usuario_id=ana.id, tipo="saida",
          data=(HOJE - timedelta(days=1)).isoformat(), hora="17:00", obs="x")

    # ---- Hoje: sem checklist e sem bloco de avisos; com gráficos e atalhos
    t = m.get("/painel").text
    assert "Checklist de hoje" not in t and "Marcar como vistos" not in t
    assert "Horas trabalhadas · 7 dias" in t and "Situação da equipe agora" in t and "Rotinas diárias" in t
    assert 'class="barra' in t and "17/11 · " in t and "1 pessoa(s)" in t
    assert 'href="/painel/ajustes"' not in t and 'href="/painel/ponto/ajuste"' in t
    assert "Avisos novos" in t and 'href="/painel/avisos"' in t
    assert "Sem ponto: 1" in t                 # Ana não bateu às 10h
    assert "Bom dia, Zé!" in t and "Sugestões do sistema" in t and "Cadastre os feriados de 2026" in t
    assert 'class="rotina grupo forte" data-grupo="ponto" href="/painel/ponto/ajuste"' in t
    # menu Escala sem submenus
    assert "Turnos abertos e trocas" not in t and "Escala da semana" not in t

    # ---- Avisos
    t = m.get("/painel/avisos").text
    assert "Ana Souza não bateu o ponto" in t and "Marcar como vistos" in t
    r = posta(m, "/painel/alertas/lidos", tm)
    assert r.headers["location"] == "/painel/avisos" and "Marcar como vistos" not in m.get("/painel/avisos").text

    # ---- Banners do Master
    assert m.get("/master/banners").status_code == 200
    posta(m, "/master/banners", tm, titulo="", link="")
    assert "Dê um título" in m.get("/master/banners").text
    posta(m, "/master/banners", tm, titulo="X", link="javascript:alert(1)")
    assert "O link precisa" in m.get("/master/banners").text
    posta(m, "/master/banners", tm, titulo="X", inicio="2026-11-20", fim="2026-11-10")
    assert "antes do início" in m.get("/master/banners").text
    posta(m, "/master/banners", tm, titulo="Folha com 20% off", texto="Só em novembro",
          link="/painel/assinatura", botao="Quero", cor="roxo", ativo="1")
    posta(m, "/master/banners", tm, titulo="Ainda não", inicio="2026-12-01", ativo="1")
    posta(m, "/master/banners", tm, titulo="Desligado", ativo="")
    posta(m, "/master/banners", tm, titulo="Img ruim", imagem="https://x.com/a.png'); alert(1)", ativo="1")
    assert "A imagem precisa" in m.get("/master/banners").text
    posta(m, "/master/banners", tm, titulo="Com foto", imagem="https://exemplo.com/banner.jpg", ativo="1", ordem="5")
    # imagem enviada como arquivo (PNG de verdade) vira /banners/<nome> e é servida
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    r = m.post("/master/banners", data={"csrf": tm, "titulo": "Arquivo", "ativo": "1", "ordem": "6"},
               files={"arquivo": ("b.png", png, "image/png")})
    assert r.status_code == 303
    with SessionLocal() as db:
        img = db.scalar(select(Banner.imagem).where(Banner.titulo == "Arquivo"))
    assert img.startswith("/banners/") and img.endswith(".png"), img
    r = m.get(img)
    assert r.status_code == 200 and r.content == png
    assert novo().get(img).status_code == 200                 # pública (divulgação)
    assert m.get("/banners/../teste.db").status_code == 404 and m.get("/banners/x.png").status_code == 404
    m.post("/master/banners", data={"csrf": tm, "titulo": "Falso", "ativo": "1"},
           files={"arquivo": ("x.png", b"<script>alert(1)</script>", "image/png")})
    assert "JPG, PNG ou WebP" in m.get("/master/banners").text
    t = m.get("/painel").text
    assert "--banner-img: url('https://exemplo.com/banner.jpg')" in t and "Img ruim" not in t
    assert "Folha com 20% off" in t and "cor-roxo" in t and 'href="/painel/assinatura"' in t
    assert "Ainda não" not in t.split("data-banners")[1].split("</section>")[0] and "Desligado" not in t
    with SessionLocal() as db:
        bid = db.scalar(select(Banner.id).where(Banner.titulo == "Folha com 20% off"))
    posta(m, "/master/banners", tm, bid=bid, titulo="Folha com 30% off", cor="verde", ativo="1")
    assert "Folha com 30% off" in m.get("/painel").text
    posta(m, f"/master/banners/{bid}/remover", tm)
    assert "Folha com 30% off" not in m.get("/painel").text

    # quem não é Master não mexe nos banners
    c = novo()
    posta(c, "/cadastro", csrf(c, "/cadastro"), aceito="1", empresa="Outra", nome="Cli",
          email="cli@x.com", senha="segredo1")
    assert c.get("/master/banners").status_code in (303, 403, 404)
    tc = csrf(c, "/painel/config")
    posta(c, "/master/banners", tc, titulo="Invasão", ativo="1")
    with SessionLocal() as db:
        assert not db.scalar(select(Banner.id).where(Banner.titulo == "Invasão"))

    # ---- nome curto do app no celular
    t = m.get("/master/plataforma").text
    assert 'name="nome_curto"' in t
    dados = {k: v for k, v in re.findall(r'name="([a-z_]+)" value="([^"]*)"', t)}
    dados.update(nome="Data Worke Brasil", nome_curto="DW Brasil")
    dados.pop("csrf", None)
    posta(m, "/master/plataforma", tm, **dados)
    man = m.get("/manifest.webmanifest").json()
    assert man["name"] == "Data Worke Brasil" and man["short_name"] == "DW Brasil", man
    assert 'apple-mobile-web-app-title" content="DW Brasil"' in m.get("/entrar").text
    print("OK: painel Hoje, avisos, banners e nome curto passaram")


if __name__ == "__main__":
    main()
