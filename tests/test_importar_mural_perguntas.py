"""Importar a equipe por planilha, leitura do mural e perguntas de fim de turno.
Rodar da raiz: .venv/Scripts/python tests/test_importar_mural_perguntas.py"""
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

from app import db as banco  # noqa: E402
from app import push  # noqa: E402
from app.db import FuncaoUsuario, RespostaTurno, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_gestores = lambda *a, **k: avisos.append(a)


def as_(hora, d):   # Flórida em novembro: UTC-5
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def entrar(token):
    c = novo()
    c.post(f"/convite/{token}", data={"csrf": csrf(c, f"/convite/{token}"), "senha": "senha123", "consentimento": "1"})
    return c, csrf(c, "/app")


PLANILHA = ("Name;Phone;Email;Role;Hourly rate;Tips;Hire date;Birth date\n"
            "Ana Souza;305 555-0101;ana@x.com;Server;R$ 12,50;yes;03/15/2025;\n"
            "Leo Lima;(305) 555-0102;;Cook;18;no;;2010-06-01\n"
            ";305 555-0103;;;;;;\n"
            "Bia Reis;123;;;;;;\n"
            "Caio Dias;305 555-0101;;;;;;\n"
            "Dan Cruz;305 555-0104;;Host;abc;;;\n")


def main():
    as_("08:00", SEG)
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Miami Pizza", "nome": "Gil",
                              "email": "gil@mp.com", "senha": "segredo1"})
    tg = csrf(g, "/painel/config")
    g.post("/painel/config", data={"csrf": tg, "nome": "Miami Pizza", "lat": "25.77", "lng": "-80.19", "raio_m": "150",
                                   "tolerancia_min": "10", "fuso": "America/Sao_Paulo", "bloquear_fora": "1",
                                   "ajuste_dias": "7", "fotos_dias": "45", "estado": "FL", "semana_inicio": "6",
                                   "frequencia": "weekly", "periodo_ancora": (SEG - timedelta(days=1)).isoformat(),
                                   "intervalo_padrao_min": "30", "tolerancia_dia_min": "7",
                                   "pct_noturno": "0", "pct_feriado": "0"})

    # ---- importar planilha
    assert "Importar planilha" in g.get("/painel/equipe").text
    modelo = g.get("/painel/importar-equipe/modelo.csv")
    assert modelo.headers["content-type"].startswith("text/csv") and "Hourly rate" in modelo.text
    r = g.post("/painel/importar-equipe", data={"csrf": tg}, files={"arquivo": ("t.csv", b"Foo;Bar\n1;2\n", "text/csv")})
    assert r.status_code == 400 and "coluna do nome" in r.text
    r = g.post("/painel/importar-equipe", data={"csrf": tg}, files={"arquivo": ("t.csv", PLANILHA.encode(), "text/csv")})
    t = r.text
    assert "Prévia: 2 pessoa(s) prontas, 4 com erro" in t
    assert "sem nome" in t and "celular inválido" in t and "celular repetido na planilha" in t and "valor da hora inválido" in t
    with SessionLocal() as db:
        assert db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza")) is None   # nada salvo na prévia
    conteudo = re.search(r'<textarea name="conteudo" hidden>(.*?)</textarea>', t, re.S).group(1)
    import html
    g.post("/painel/importar-equipe/confirmar", data={"csrf": tg, "conteudo": html.unescape(conteudo)})
    assert "2 pessoa(s) importada(s). 4 linha(s) ficaram de fora" in g.get("/painel/equipe").text
    with SessionLocal() as db:
        ana = db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza"))
        leo = db.scalar(select(Usuario).where(Usuario.nome == "Leo Lima"))
        assert (ana.celular, ana.email, ana.cargo, ana.taxa_hora_centavos, ana.gorjeta, ana.data_admissao) == \
               ("3055550101", "ana@x.com", "Server", 1250, True, date(2025, 3, 15))
        assert leo.nascimento == date(2010, 6, 1) and leo.convite_token and leo.taxa_hora_centavos == 1800
        assert db.scalar(select(FuncaoUsuario).where(FuncaoUsuario.usuario_id == ana.id)).nome == "Server"
        tokens = {"Ana": ana.convite_token, "Leo": leo.convite_token}
    # importar de novo: os mesmos celulares viram erro
    r = g.post("/painel/importar-equipe", data={"csrf": tg}, files={"arquivo": ("t.csv", PLANILHA.encode(), "text/csv")})
    assert "celular já cadastrado" in r.text

    # ---- mural: lida por X de Y
    ana_c, ta = entrar(tokens["Ana"])
    leo_c, tl = entrar(tokens["Leo"])
    g.post("/painel/mural", data={"csrf": tg, "texto": "Reunião sexta às 15h"})
    m = g.get("/painel/mural").text
    assert "Lida por 0 de 2" in m and "Ainda não leram: Ana Souza, Leo Lima" in m
    ana_c.get("/app/mural")
    m = g.get("/painel/mural").text
    assert "Lida por 1 de 2" in m and "Ainda não leram: Leo Lima" in m
    leo_c.get("/app/mural")
    assert "✓ Lida por todos (2)" in g.get("/painel/mural").text
    assert "Lida por" not in ana_c.get("/app/mural").text          # funcionário não vê a contagem

    # ---- perguntas de fim de turno
    assert "Nenhuma pergunta ainda" in g.get("/painel/perguntas").text
    g.post("/painel/perguntas", data={"csrf": tg, "texto": "Houve algum acidente hoje?", "tipo": "sim_nao", "avisar_se": "sim"})
    g.post("/painel/perguntas", data={"csrf": tg, "texto": "Gorjeta em dinheiro?", "tipo": "numero"})
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ana.id, "tipo": "entrada", "data": SEG.isoformat(),
                                         "hora": "08:00", "obs": "t"})
    as_("10:00", SEG)
    assert "Antes de ir embora" not in ana_c.get("/app").text            # ainda trabalhando
    g.post("/painel/ponto/manual", data={"csrf": tg, "usuario_id": ana.id, "tipo": "saida", "data": SEG.isoformat(),
                                         "hora": "10:00", "obs": "t"})
    as_("10:05", SEG)
    t = ana_c.get("/app").text
    assert "Antes de ir embora" in t and "Houve algum acidente hoje?" in t and "Como foi o seu turno?" not in t
    ana_c.post("/app/perguntas", data={"csrf": ta, f"p_{1}": "talvez"})
    assert "Responda Sim ou Não" in ana_c.get("/app").text
    ana_c.post("/app/perguntas", data={"csrf": ta, "p_1": "sim", "p_2": "abc"})
    assert "Use só números" in ana_c.get("/app").text
    ana_c.post("/app/perguntas", data={"csrf": ta, "p_1": "sim", "p_2": "R$ 32,50"})
    t = ana_c.get("/app").text
    assert "Antes de ir embora" not in t and "Como foi o seu turno?" in t   # a avaliação vem depois
    assert avisos and avisos[-1][1] == "Fim de turno: Ana" and "respondeu Sim" in avisos[-1][2]
    p = g.get("/painel/perguntas").text
    assert "Ana Souza" in p and "<strong>Sim</strong>" in p and "R$ 32,50" in p
    with SessionLocal() as db:
        assert len(list(db.scalars(select(RespostaTurno)))) == 2
    # desligar a pergunta
    g.post("/painel/perguntas/1", data={"csrf": tg, "acao": "desligar"})
    assert "<s class=\"suave\">Houve algum acidente hoje?</s>" in g.get("/painel/perguntas").text
    print("OK: importar equipe, leitura do mural e perguntas de fim de turno passou")


if __name__ == "__main__":
    main()
