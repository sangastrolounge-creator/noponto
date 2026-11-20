"""Disponibilidade da equipe, avaliação do turno e relatórios.
Rodar da raiz: .venv/Scripts/python tests/test_relatorios.py"""
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

from app import apuracao, db as banco  # noqa: E402
from app import push  # noqa: E402
from app.db import AvaliacaoTurno, Disponibilidade, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
avisos = []
push.avisar_gestores = lambda *a, **k: avisos.append((a, k))


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def turno(g, tg, uid, d, ini, fim):
    posta(g, "/painel/escala/turno", tg, usuario_id=uid, data=d.isoformat(), modo="trabalho",
          inicio=ini, fim=fim, intervalo="0", repetir="dia")


def bate(g, tg, uid, tipo, d, hora):
    as_(hora, d)
    posta(g, "/painel/ponto/manual", tg, usuario_id=uid, tipo=tipo, data=d.isoformat(), hora=hora, obs="t")


def main():
    as_("07:00", SEG - timedelta(days=1))
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Gil", email="gil@d.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Diner", lat="25.77", lng="-80.19", raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", estado="FL", semana_inicio="6", frequencia="weekly",
          periodo_ancora=SEG.isoformat(), intervalo_padrao_min="30", tolerancia_dia_min="7",
          pct_noturno="0", pct_feriado="0")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bia Reis", "3055550002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        ana_u = db.get(Usuario, ids["Ana"])
        ana_u.cargo, ana_u.salario_centavos = "Cozinha", 440000      # R$ 4.400 / 220 h = R$ 20 a hora
        db.get(Usuario, ids["Bia"]).cargo = "Garçom"
        convite = ana_u.convite_token
        db.commit()
    ana = novo()
    posta(ana, f"/convite/{convite}", csrf(ana, f"/convite/{convite}"), senha="senha123", consentimento="1")
    ta = csrf(ana, "/app")

    # ---- disponibilidade
    assert "Minha disponibilidade" in ana.get("/app/escala").text
    r = posta(ana, "/app/disponibilidade", ta, tipo_0="parcial", ini_0="", fim_0="")
    assert r.headers["location"].startswith("/app/escala")
    assert "Informe o horário" in ana.get("/app/escala").text
    posta(ana, "/app/disponibilidade", ta, tipo_0="parcial", ini_0="09:00", fim_0="15:00",
          tipo_1="nao", obs_1="aula", tipo_2="livre")
    with SessionLocal() as db:
        ds = {d.dia_semana: (d.tipo, d.inicio, d.fim, d.obs) for d in db.scalars(select(Disponibilidade))}
    assert ds == {0: ("parcial", "09:00", "15:00", ""), 1: ("nao", "", "", "aula")}, ds
    assert len(avisos) == 1 and avisos[0][0][4] == "escala", avisos
    posta(ana, "/app/disponibilidade", ta, tipo_0="parcial", ini_0="09:00", fim_0="15:00", tipo_1="nao", obs_1="aula")
    assert len(avisos) == 1          # nada mudou: não avisa de novo
    assert 'value="parcial" selected' in ana.get("/app/escala").text

    turno(g, tg, ids["Ana"], SEG, "08:00", "12:00")                      # antes das 9: conflito
    turno(g, tg, ids["Ana"], SEG + timedelta(days=1), "10:00", "14:00")  # não pode: conflito
    turno(g, tg, ids["Ana"], SEG + timedelta(days=2), "09:00", "13:00")  # livre
    turno(g, tg, ids["Bia"], SEG, "09:00", "13:00")
    grade = g.get(f"/painel/escala?semana={SEG}").text
    assert grade.count('class="disp-aviso"') == 2, grade.count('class="disp-aviso"')
    assert 'data-disp="Não pode · aula"' in grade and 'data-disp="Pode 09:00–15:00"' in grade

    # ---- segunda: Ana chega 08:20 (atraso), sai 12:00; Bia falta
    bate(g, tg, ids["Ana"], "entrada", SEG, "08:20")
    bate(g, tg, ids["Ana"], "saida", SEG, "12:00")
    # ---- avaliação: aparece depois da saída, uma vez por dia
    as_("12:05", SEG)
    assert "Como foi o seu turno?" in ana.get("/app").text
    posta(ana, "/app/avaliar", ta, nota="7")
    assert "Escolha de 1 a 5" in ana.get("/app").text
    posta(ana, "/app/avaliar", ta, nota="2", comentario="Faltou gente na cozinha")
    assert "Como foi o seu turno?" not in ana.get("/app").text
    assert avisos[-1][0][1].startswith("Turno difícil") and avisos[-1][0][4] == "escala", avisos[-1]
    # ---- terça: no horário; pula a avaliação
    terca = SEG + timedelta(days=1)
    bate(g, tg, ids["Ana"], "entrada", terca, "10:00")
    bate(g, tg, ids["Ana"], "saida", terca, "14:00")
    as_("14:10", terca)
    n_avisos = len(avisos)
    posta(ana, "/app/avaliar", ta, nota="pular")
    assert "Como foi o seu turno?" not in ana.get("/app").text and len(avisos) == n_avisos
    with SessionLocal() as db:
        assert sorted(a.nota for a in db.scalars(select(AvaliacaoTurno))) == [0, 2]

    # ---- relatórios (quarta 08:00: o turno de quarta ainda não começou)
    as_("08:00", SEG + timedelta(days=2))
    url = f"/painel/relatorios?ini={SEG}&fim={SEG + timedelta(days=6)}"
    rel = g.get(url).text
    assert "Relatórios" in rel
    assert "50%" in rel                     # Ana: 1 pontual, 1 atraso
    assert "1 atraso(s), 20 min" in rel
    assert '<span class="etiqueta erro">1</span>' in rel     # Bia faltou
    assert "Faltou gente na cozinha" in rel
    assert "R$ 153,33" in rel                 # Ana: 3h40 + 4h = 7h40 x R$ 20
    resp = g.get(url + "&formato=csv")
    assert resp.headers["content-type"].startswith("text/csv")
    linhas = resp.text.lstrip("﻿").splitlines()
    assert linhas[1].startswith("Ana Souza;Cozinha;2;8,0;7,67;-0,33;50;1;0;2,0;153,33"), linhas[1]
    assert linhas[2].startswith("Bia Reis;Garçom;1;4,0;0,0;-4,0;;0;1;"), linhas[2]

    # ---- adicional noturno: Bia trabalha quinta 20h-23h (1h depois das 22h) e ganha R$ 10 a hora
    qui = SEG + timedelta(days=3)
    with SessionLocal() as db:
        db.get(Usuario, ids["Bia"]).salario_centavos = 220000
        db.get(Usuario, ids["Bia"]).empresa.pct_noturno = 20
        db.commit()
    as_("08:00", qui)
    turno(g, tg, ids["Bia"], qui, "20:00", "23:00")
    bate(g, tg, ids["Bia"], "entrada", qui, "20:00")
    bate(g, tg, ids["Bia"], "saida", qui, "23:00")
    as_("23:30", qui)
    with SessionLocal() as db:
        e = db.get(Usuario, ids["Bia"]).empresa
        noite = {r["u"].id: r["noturno"] for r in apuracao.apurar(db, e, SEG, qui)}[ids["Bia"]]
    assert noite >= 60, noite                                  # 1h (mais a hora reduzida, se houver)
    valor = round(noite * 1000 * 20 / 6000)
    rel = g.get(url).text
    assert "Adicional noturno" in rel and f"{noite // 60}h{noite % 60:02d}" in rel
    assert ("R$ " + f"{valor / 100:.2f}".replace(".", ",")) in rel and "(20%)" in rel
    linhas = g.get(url + "&formato=csv").text.lstrip("\ufeff").splitlines()
    assert "Horas noturnas" in linhas[0] and "Adicional noturno (R$)" in linhas[0]
    assert linhas[2].endswith(f'{f"{valor / 100:.2f}".replace(".", ",")}'), linhas[2]

    # ---- desligar a avaliação: não pergunta mais
    posta(g, "/painel/config/avaliar-turno", tg, ligado="")
    qua = SEG + timedelta(days=2)
    bate(g, tg, ids["Ana"], "entrada", qua, "09:00")
    bate(g, tg, ids["Ana"], "saida", qua, "13:00")
    assert "Como foi o seu turno?" not in ana.get("/app").text

    # ---- gerente sem a permissão Ponto não abre relatórios; sem Pagamento não vê custo
    posta(g, "/painel/gerentes", tg, nome="Lia", email="lia@d.com", p_escala="1")
    posta(g, "/painel/gerentes", tg, nome="Rui", email="rui@d.com", p_ponto="1")
    with SessionLocal() as db:
        tk = {u.email: u.convite_token for u in db.scalars(select(Usuario).where(
            Usuario.email.in_(("lia@d.com", "rui@d.com"))))}
    lia, rui = novo(), novo()
    posta(lia, f"/convite/{tk['lia@d.com']}", csrf(lia, f"/convite/{tk['lia@d.com']}"), senha="senha123")
    posta(rui, f"/convite/{tk['rui@d.com']}", csrf(rui, f"/convite/{tk['rui@d.com']}"), senha="senha123")
    assert lia.get("/painel/relatorios").status_code in (302, 303)
    t_rui = rui.get("/painel/relatorios").text
    assert "Por pessoa" in t_rui and "Custo da equipe" not in t_rui and "R$ 153,33" not in t_rui
    print("OK: disponibilidade, avaliação do turno e relatórios passou")


if __name__ == "__main__":
    main()
