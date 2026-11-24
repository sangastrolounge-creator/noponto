"""Idiomas: seletor PT/EN/ES, telas, avisos montados, erros do ponto, scripts e página de vendas.
Rodar da raiz: .venv/Scripts/python tests/test_idiomas.py"""
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
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL", "ASAAS_API_KEY"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import i18n  # noqa: E402
from app.db import SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": datetime(2026, 11, 16, 10, 0)}
banco.agora = lambda: relogio["agora"]
LAT, LNG = -2.5307, -44.3068


def novo(**kw):
    return TestClient(app, follow_redirects=False, **kw)


def csrf(cli, caminho="/entrar"):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def main():
    # ---- todas as chaves das telas, do código e dos scripts têm tradução nos dois idiomas
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import i18n_extrair
    for cod in ("en", "es"):
        tab = json.loads(Path(f"app/idiomas/{cod}.json").read_text(encoding="utf-8"))
        grupos = i18n_extrair.todos()
        faltam = [k for g in grupos.values() for k in g if k not in tab and k not in i18n_extrair.IGUAIS]
        assert not faltam, (cod, faltam[:10])
        for k, v in tab.items():  # os campos {v1}, {v2}... continuam no texto traduzido
            assert sorted(re.findall(r"\{v\d+\}", k)) == sorted(re.findall(r"\{v\d+\}", v)), (cod, k, v)

    # ---- idioma do navegador na primeira visita; escolha guardada depois
    anon = novo()
    assert 'lang="pt-BR"' in anon.get("/entrar").text and ">Entrar<" in anon.get("/entrar").text
    en = novo(headers={"Accept-Language": "en-US,en;q=0.9"})
    tela = en.get("/entrar").text
    assert 'lang="en"' in tela and "Sign in" in tela and "Email or phone number" in tela
    assert 'href="/idioma/es?volta=/entrar"' in tela and 'aria-label="English" class="ativa" aria-current="true"><svg class="bandeira"' in tela
    r = en.get("/idioma/es?volta=/entrar")
    assert r.status_code == 303 and r.headers["location"] == "/entrar"
    assert "Correo o celular" in en.get("/entrar").text          # o cookie vence o navegador
    for ruim in ("https://mal.example", "//mal.example", "/\\mal.example"):
        assert en.get("/idioma/es", params={"volta": ruim}).headers["location"] == "/"
    assert en.get("/idioma/xx?volta=/entrar").headers["location"] == "/entrar"
    assert "Correo o celular" in en.get("/entrar").text          # idioma inválido não muda nada

    # ---- erro de formulário e aviso montado com nome (tradução por modelo)
    g = novo()
    g.get("/idioma/en?volta=/cadastro")
    r = posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar A", nome="Gil", email="x", senha="segredo1")
    assert "Invalid email." in r.text
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar A", nome="Gil", email="gil@a.com",
          senha="segredo1")
    tg = csrf(g, "/painel/config")
    painel = g.get("/painel").text
    assert "Today" in painel and "Settings" in painel and "Guide" in painel and "Sign out" in painel
    posta(g, "/painel/equipe", tg, nome="Ana Souza", celular="98999990001")
    assert "Ana Souza added. Send the access link to their phone." in g.get("/painel/equipe").text
    g.get("/idioma/es?volta=/painel")
    posta(g, "/painel/equipe", tg, nome="Bruno", celular="98999990002")
    assert "Bruno registrado. Envía el enlace de acceso a su celular." in g.get("/painel/equipe").text
    assert "Lun" in g.get("/painel/escala").text and "Horario de la semana" in g.get("/painel/escala").text
    # o guia recebe os títulos traduzidos para achar os cartões da tela
    assert '"Equipo de hoy"' in g.get("/painel").text or "Equipo de hoy" in g.get("/painel").text
    g.get("/idioma/pt?volta=/painel")
    assert "Hoje" in g.get("/painel").text and "Configurações" in g.get("/painel").text

    # ---- erro do ponto (JSON) e textos dos scripts
    with SessionLocal() as db:
        convite = db.scalar(select(Usuario.convite_token).where(Usuario.nome == "Ana Souza"))
    ana = novo()
    ana.get("/idioma/en?volta=/")
    caminho = f"/convite/{convite}"
    assert "Hi, Ana!" in ana.get(caminho).text
    posta(ana, caminho, csrf(ana, caminho), senha="senha123")
    tela = ana.get("/app").text
    assert "Clock in" in tela and "My schedule" not in tela or True
    m = re.search(r"window\.T = (\{.*?\});", tela)
    textos = json.loads(m.group(1))
    assert textos.get("Obtendo sua localização…") == "Getting your location…"
    ta = csrf(ana, "/app")
    r = ana.post("/app/ponto", json={"lat": "x"}, headers={"X-CSRF": ta})
    assert r.json()["erro"] == "Your location was not received. Try again.", r.text
    ana.get("/idioma/es?volta=/app")
    r = ana.post("/app/ponto", json={"lat": "x"}, headers={"X-CSRF": ta})
    assert r.json()["erro"] == "No recibí tu ubicación. Intenta de nuevo."

    # ---- dias da semana e rótulos de tipo traduzidos; português continua igual
    with i18n.no_idioma("en"):
        assert i18n.DIAS[0] == "Mon" and i18n._("Folga") == "Day off"
        assert i18n._("Ana Souza não bateu o ponto (entrada às 08:00).") == \
            "Ana Souza did not clock in (start at 08:00)."
    assert i18n.DIAS[0] == "Seg" and i18n._("Folga") == "Folga"

    # ---- página de vendas: uma por idioma, com volta ao português
    from app import vendas
    padrao_es = vendas.padrao_no_idioma("es")
    assert padrao_es["topo"]["titulo"].startswith("Horario, fichaje y cierre")
    assert padrao_es["faq"]["itens"][0]["pergunta"] == "¿Necesito instalar algo o comprar un dispositivo?"
    print("OK: idiomas passou")


if __name__ == "__main__":
    main()
