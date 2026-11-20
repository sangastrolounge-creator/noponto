"""Abre todas as telas (GET) do gestor, do Master e do funcionário numa empresa com dados de
verdade e confere que nenhuma quebra (erro 500). Pega template com variável faltando, função
que não existe mais, etc. Rodar da raiz: .venv/Scripts/python tests/test_telas.py"""
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
from app.db import Ausencia, Fechamento, FolhaPagamento, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

HOJE = date(2026, 11, 18)
SEG = date(2026, 11, 16)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]
LAT, LNG = -2.5307, -44.3068


def as_(hora: str, dia: date):
    h, m = hora.split(":")
    relogio["agora"] = datetime(dia.year, dia.month, dia.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def csrf(cli, caminho):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def posta(cli, caminho, token, **dados):
    r = cli.post(caminho, data={"csrf": token, **dados})
    assert r.status_code < 500, (caminho, r.status_code, r.text[-2000:])
    return r


def rotas_get(prefixo: str) -> list[str]:
    caminhos = []
    for r in app.routes:
        if "GET" in getattr(r, "methods", set()) and r.path.startswith(prefixo):
            caminhos.append(r.path)
    return sorted(set(caminhos))


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Padaria A", nome="Gil Gestor",
          email="gil@a.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Padaria A", lat=str(LAT), lng=str(LNG), raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", controle="extra", intervalo_padrao_min="60", intervalo_auto="1",
          tolerancia_dia_min="10")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        us = {u.nome.split()[0]: u for u in db.scalars(select(Usuario).where(Usuario.papel == "funcionario"))}
        ids = {k: u.id for k, u in us.items()}
        token_ana = us["Ana"].convite_token
        for u in us.values():
            u.data_admissao, u.salario_centavos = date(2024, 10, 1), 250000
        db.commit()
    ana = novo()
    posta(ana, f"/convite/{token_ana}", csrf(ana, f"/convite/{token_ana}"), senha="senha123", consentimento="1")
    ta = csrf(ana, "/app")

    # escala, batidas, ausência, férias e fechamento de uma semana passada
    for nome in ("Ana", "Bruno"):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=SEG.isoformat(), modo="trabalho",
              inicio="08:00", fim="17:00", intervalo="60", repetir="uteis")
    as_("18:00", HOJE)
    for dia in (SEG, SEG + timedelta(days=1)):
        for tipo, hora in (("entrada", "08:00"), ("saida", "12:00"), ("entrada", "12:40"), ("saida", "17:05")):
            posta(g, "/painel/ponto/manual", tg, usuario_id=ids["Ana"], tipo=tipo, data=dia.isoformat(),
                  hora=hora, obs="teste")
    posta(g, "/painel/ausencias", tg, usuario_id=ids["Bruno"], tipo="atestado", inicio=SEG.isoformat(),
          fim=SEG.isoformat(), obs="gripe")
    posta(g, "/painel/ferias", tg, usuario_id=ids["Bruno"], inicio="2027-01-04", fim="2027-01-23",
          abono="0", confirmar="1")
    posta(ana, "/app/ferias", ta, inicio="2027-02-01", fim="2027-02-20", abono="0")
    posta(g, "/painel/fechamento", tg, de=SEG.isoformat(), ate=(SEG + timedelta(days=1)).isoformat())
    with SessionLocal() as db:
        fech = db.scalar(select(Fechamento.id))
        assert fech, "o fechamento não foi criado"
        assert db.scalar(select(Ausencia.id).where(Ausencia.situacao == "pendente"))
    posta(g, "/painel/pagamento/gerar", tg, fechamento_id=str(fech))
    with SessionLocal() as db:
        folha = db.get(FolhaPagamento, db.scalar(select(FolhaPagamento.id)))
        item = folha.itens[0].id
        linha = folha.fechamento.linhas[0].id

    ids_rota = {"uid": ids["Ana"], "fid": ids["Ana"], "aid": 1, "lid": linha, "iid": item, "id_": 1}
    quebradas = []

    def abrir(cli, caminho, quem):
        r = cli.get(caminho)
        if r.status_code >= 500:
            quebradas.append((quem, caminho, r.status_code, r.text[-600:]))
        return r

    # gestor (o primeiro cadastro é o dono e o Master da plataforma)
    for p in rotas_get("/painel") + rotas_get("/master"):
        if "{" in p:
            continue
        abrir(g, p, "gestor")
    for p in (f"/painel/equipe/{ids['Ana']}/ficha", f"/painel/folha?func={ids['Ana']}&de={SEG}&ate={SEG}",
              f"/painel/fechamento/resumo?de={SEG}&ate={SEG + timedelta(days=1)}",
              f"/painel/fechamento/{fech}/planilha.csv", f"/painel/ponto.csv?de={SEG}&ate={SEG}",
              f"/painel/pagamento/{folha.id}", f"/painel/pagamento/{folha.id}/holerite/{item}",
              f"/painel/pagamento/{folha.id}/holerites", f"/painel/pagamento/{folha.id}/planilha.csv",
              f"/painel/escala?semana={SEG}", f"/painel/ponto?de={SEG}&ate={SEG}",
              "/master/empresas/1", "/"):
        abrir(g, p, "gestor")
    # funcionário
    for p in rotas_get("/app"):
        if "{" in p:
            continue
        abrir(ana, p, "funcionário")
    abrir(ana, f"/app/folha/{linha}", "funcionário")
    # públicas
    pub = novo()
    for p in ("/", "/entrar", "/cadastro", "/esqueci", "/terms", "/privacy", "/offline",
              "/manifest.webmanifest", "/sw.js", "/saude"):
        abrir(pub, p, "visitante")

    assert not quebradas, "\n\n".join(f"{q} {c} -> {s}\n{t}" for q, c, s, t in quebradas)
    total = len(rotas_get("/painel")) + len(rotas_get("/master")) + len(rotas_get("/app"))
    print(f"OK: telas passou ({total} rotas GET conferidas)")


if __name__ == "__main__":
    main()
