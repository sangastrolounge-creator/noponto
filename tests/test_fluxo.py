"""Teste de ponta a ponta. Rodar da raiz: .venv/Scripts/python tests/test_fluxo.py"""
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

from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Alerta, Batida, SessionLocal, Turno, Usuario  # noqa: E402
from app.main import app, ciclo_alertas  # noqa: E402

# Relógio controlado: segunda-feira 16/11/2026, em horário de Brasília (UTC-3).
HOJE = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora: str, dia: date = HOJE):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


LAT, LNG = -2.5307, -44.3068
LONGE = (LAT + 0.01, LNG)  # ~1,1 km


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho="/entrar"):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def bater(cli, token, lat=LAT, lng=LNG):
    return cli.post("/app/ponto", json={"lat": lat, "lng": lng, "precisao": 12},
                    headers={"X-CSRF": token})


def alertas(tipo=None):
    with SessionLocal() as db:
        q = select(Alerta)
        if tipo:
            q = q.where(Alerta.tipo == tipo)
        return list(db.scalars(q))


def main():
    as_("08:00")
    # ---- cadastro do gestor e configuração
    g = novo()
    r = posta(g, "/cadastro", csrf(g, "/cadastro"), empresa="Padaria A", nome="Gil Gestor",
              email="gil@a.com", senha="segredo1")
    assert r.status_code == 303 and r.headers["location"] == "/painel/config", r.text
    tg = csrf(g, "/painel/config")
    assert "ainda não foi definido" in g.get("/painel").text
    r = posta(g, "/painel/config", tg, nome="Padaria A", endereco="Rua 1", lat=str(LAT),
              lng=str(LNG).replace(".", ","), raio_m="150", tolerancia_min="10",
              fuso="America/Sao_Paulo", bloquear_fora="1")
    assert r.status_code == 303
    assert "ainda não foi definido" not in g.get("/painel").text
    assert posta(g, "/painel/config", "errado", nome="x").status_code == 403  # CSRF

    # ---- equipe e primeiro acesso
    posta(g, "/painel/equipe", tg, nome="Ana Souza", celular="(98) 99999-0001", cargo="Caixa")
    posta(g, "/painel/equipe", tg, nome="Bruno Lima", celular="98 99999-0002")
    r = posta(g, "/painel/equipe", tg, nome="Repetido", celular="98999990001")
    pagina = g.get("/painel/equipe").text
    assert "já está cadastrado" in pagina and "Repetido" not in pagina
    links = re.findall(r'data-copiar="[^"]*(/convite/[^"]+)"', pagina)
    assert len(links) == 2, links
    with SessionLocal() as db:
        ana_id = db.scalar(select(Usuario.id).where(Usuario.nome == "Ana Souza"))
        bruno_id = db.scalar(select(Usuario.id).where(Usuario.nome == "Bruno Lima"))
        tok = {u.id: u.convite_token for u in db.scalars(select(Usuario)) if u.convite_token}

    def primeiro_acesso(uid):
        c = novo()
        caminho = f"/convite/{tok[uid]}"
        assert "Crie uma senha" in c.get(caminho).text
        assert posta(c, caminho, csrf(c, caminho), senha="123").status_code == 400
        r = posta(c, caminho, csrf(c, caminho), senha="senha123")
        assert r.status_code == 303 and r.headers["location"] == "/app", r.text
        assert c.get(caminho).status_code == 404  # link só vale uma vez
        return c, csrf(c, "/app")

    ana, ta = primeiro_acesso(ana_id)
    bruno, tb = primeiro_acesso(bruno_id)
    assert ana.get("/painel").headers["location"] == "/app"  # funcionário não entra no painel
    assert "Aguardando 1º acesso" not in g.get("/painel/equipe").text

    # login pelo celular
    c = novo()
    r = posta(c, "/entrar", csrf(c), login="98 99999-0001", senha="senha123")
    assert r.headers["location"] == "/app", r.text
    assert posta(c, "/entrar", csrf(c), login="98999990001", senha="errada").status_code == 400

    # ---- escala
    seg = HOJE.isoformat()
    posta(g, "/painel/escala/turno", tg, usuario_id=ana_id, data=seg, modo="trabalho",
          inicio="09:00", fim="17:00", repetir="uteis")
    posta(g, "/painel/escala/turno", tg, usuario_id=bruno_id, data=seg, modo="trabalho",
          inicio="08:00", fim="16:00", repetir="dia")
    posta(g, "/painel/escala/turno", tg, usuario_id=bruno_id,
          data=(HOJE + timedelta(days=6)).isoformat(), modo="folga")
    r = posta(g, "/painel/escala/turno", tg, usuario_id=bruno_id, data=seg, modo="trabalho",
              inicio="8h", fim="16:00")
    grade = g.get(f"/painel/escala?semana={seg}").text
    assert "Informe o horário" in grade
    assert grade.count('class="celula trabalho"') == 6 and grade.count('class="celula folga"') == 1
    assert "40h00 na semana" in grade and "8h00 na semana" in grade
    assert "09:00 às 17:00" in ana.get("/app").text
    assert "09:00 às 17:00" in ana.get("/app/escala").text
    with SessionLocal() as db:
        noite = Turno(usuario_id=1, empresa_id=1, data=HOJE, inicio="18:00", fim="02:00")
        assert regras.minutos_turno(noite) == 480

    # ---- ponto: fora da área, entrada, sem ponto, atraso
    as_("08:50")
    r = bater(ana, ta, *LONGE)
    assert r.status_code == 400 and "m da empresa" in r.json()["erro"], r.text
    assert len(alertas("fora_area")) == 1
    bater(ana, ta, *LONGE)
    assert len(alertas("fora_area")) == 1  # não repete o aviso em 10 min
    r = bater(ana, ta)
    assert r.status_code == 200 and r.json()["tipo"] == "entrada" and r.json()["hora"] == "08:50", r.text
    assert "Atraso" not in r.json()["mensagem"]
    r = bater(ana, ta)
    assert r.status_code == 400 and "há instantes" in r.json()["erro"]
    assert bater(ana, "errado").status_code == 403
    assert "Bater saída" in ana.get("/app").text

    ciclo_alertas()
    ciclo_alertas()
    sem = alertas("sem_ponto")
    assert len(sem) == 1 and "Bruno Lima" in sem[0].texto, [a.texto for a in alertas()]
    painel = g.get("/painel").text
    assert "Não bateu o ponto" in painel and "Trabalhando" in painel
    assert "Bruno Lima não bateu o ponto" in painel

    as_("09:30")
    r = bater(bruno, tb)
    assert r.status_code == 200 and "Atraso de 90 min" in r.json()["mensagem"], r.text
    assert "chegou 90 min atrasado" in alertas("atraso")[0].texto
    assert "atrasou 90 min" in g.get("/painel").text
    posta(g, "/painel/alertas/lidos", tg)
    assert "Nenhum aviso novo" in g.get("/painel").text

    # ---- tarefas
    posta(g, "/painel/tarefas", tg, titulo="Fechar o caixa", usuario_id="0",
          **{f"d{i}": "1" for i in range(7)})
    posta(g, "/painel/tarefas", tg, titulo="Limpar a vitrine", usuario_id=str(ana_id), d0="1")
    posta(g, "/painel/tarefas", tg, titulo="Só domingo", usuario_id="0", d6="1")
    r = posta(g, "/painel/tarefas", tg, titulo="Sem dia", usuario_id="0")
    assert "pelo menos um dia" in g.get("/painel/tarefas").text
    pa, pb = ana.get("/app").text, bruno.get("/app").text
    assert "Fechar o caixa" in pa and "Limpar a vitrine" in pa and "Só domingo" not in pa
    assert "Fechar o caixa" in pb and "Limpar a vitrine" not in pb
    posta(ana, "/app/tarefas/1", ta, dia=seg)
    assert "Ana às 09:30" in g.get("/painel").text
    posta(bruno, "/app/tarefas/1", tb, dia=seg)  # Bruno não desmarca a tarefa da Ana
    assert "marcada por Ana" in bruno.get("/app").text
    posta(bruno, "/app/tarefas/2", tb, dia=seg)  # nem marca a tarefa só dela
    assert "1<span" in g.get("/painel").text.split("Tarefas feitas")[1][:120]

    # ---- saída, sem saída, lançamento manual, espelho
    as_("17:05")
    r = bater(ana, ta)
    assert r.json()["tipo"] == "saida" and r.json()["hora"] == "17:05", r.text
    ciclo_alertas()
    assert "Bruno Lima não registrou a saída" in alertas("sem_saida")[0].texto
    assert "Sem saída registrada" in g.get("/painel").text
    r = posta(g, "/painel/ponto/manual", tg, usuario_id=bruno_id, tipo="saida", data=seg,
              hora="16:00", obs="esqueceu")
    assert r.status_code == 303
    r = posta(g, "/painel/ponto/manual", tg, usuario_id=bruno_id, tipo="saida", data=seg,
              hora="23:00")
    espelho = g.get(f"/painel/ponto?de={seg}&ate={seg}").text
    assert "no futuro" in espelho
    assert "8h15" in espelho and "6h30" in espelho and "atraso 90 min" in espelho, espelho
    assert "16:00*" in espelho
    planilha = g.get(f"/painel/ponto.csv?de={seg}&ate={seg}").text
    assert "Ana Souza;16/11/2026;09:00-17:00;08:50 | 17:05;8h15;0" in planilha, planilha
    with SessionLocal() as db:
        bid = db.scalar(select(Batida.id).where(Batida.manual.is_(True)))
    posta(g, f"/painel/ponto/{bid}/apagar", tg)
    aberto = g.get(f"/painel/ponto?de={seg}&ate={seg}").text
    assert ">trabalhando<" in aberto and "falta batida" not in aberto  # entrada recente sem saída

    # ---- copiar a semana
    prox = (HOJE + timedelta(days=7)).isoformat()
    posta(g, "/painel/escala/copiar", tg, semana=prox)
    grade = g.get(f"/painel/escala?semana={prox}").text
    assert "7 dia(s) copiado(s)" in grade and grade.count('class="celula trabalho"') == 6

    # ---- saída antecipada e ponto sem escala (terça)
    ter = HOJE + timedelta(days=1)
    as_("09:00", ter)
    assert bater(ana, ta).json()["tipo"] == "entrada"
    assert bater(bruno, tb).json()["tipo"] == "entrada"  # Bruno não está na escala de terça
    assert "sem estar na escala" in alertas("sem_escala")[0].texto
    as_("12:00", ter)
    assert bater(ana, ta).json()["tipo"] == "saida"
    assert "antes do fim do turno" in alertas("saida_antecipada")[0].texto
    as_("13:00", ter)  # volta do intervalo: não conta como atraso
    assert "Atraso" not in bater(ana, ta).json()["mensagem"]
    assert len(alertas("atraso")) == 1

    antigo = g.get(f"/painel/ponto?de={seg}&ate={seg}").text  # um dia depois, virou batida faltando
    assert "falta batida" in antigo and ">trabalhando<" not in antigo

    # ---- isolamento entre empresas
    h = novo()
    posta(h, "/cadastro", csrf(h, "/cadastro"), empresa="Loja B", nome="Hugo", email="hugo@b.com",
          senha="segredo2")
    th = csrf(h, "/painel/config")
    assert "Ana Souza" not in h.get("/painel/equipe").text
    assert "Ana Souza" not in h.get(f"/painel/ponto?de={seg}&ate={seg}").text
    posta(h, f"/painel/equipe/{ana_id}", th, acao="desativar")
    posta(h, "/painel/escala/turno", th, usuario_id=ana_id, data=seg, modo="limpar")
    posta(h, "/painel/tarefas/1", th, acao="apagar")
    with SessionLocal() as db:
        assert db.get(Usuario, ana_id).ativo
        assert db.scalar(select(Turno).where(Turno.usuario_id == ana_id, Turno.data == HOJE))
    assert "Fechar o caixa" in g.get("/painel/tarefas").text

    # ---- desativar e novo link
    posta(g, f"/painel/equipe/{bruno_id}", tg, acao="desativar")
    assert bruno.get("/app").headers["location"] == "/entrar"
    posta(g, f"/painel/equipe/{ana_id}", tg, acao="novo_link")
    c = novo()
    assert posta(c, "/entrar", csrf(c), login="98999990001", senha="senha123").status_code == 400
    assert "Aguardando 1º acesso" in g.get("/painel/equipe").text

    # ---- páginas soltas
    c = novo()
    assert c.get("/saude").json() == {"ok": True}
    assert c.get("/manifest.webmanifest").json()["start_url"] == "/"
    assert "showNotification" in c.get("/sw.js").text
    assert c.get("/static/icone-192.png").content[:4] == b"\x89PNG"
    assert c.get("/offline").status_code == 200 and c.get("/").status_code == 200
    assert c.get("/painel").headers["location"] == "/entrar"
    print("OK: fluxo completo passou")


if __name__ == "__main__":
    main()
