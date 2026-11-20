"""Configuração guiada (triagem): respostas viram configuração pronta.
Rodar da raiz: .venv/Scripts/python tests/test_triagem.py"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime
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
from app import triagem  # noqa: E402
from app.db import Empresa, Local, SessionLocal, Tarefa  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 19, 15, 0)}
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


RESPOSTAS = {"segmento": "restaurante", "porte": "16-30", "locais": "2-3", "estado": "MT",
             "abre": "11:00", "fecha": "23:00", "gorjeta": "sim", "menores": "sim", "ponto": "ambos",
             "frequencia": "extra", "provedor": "noponto"}


def main():
    # modelos de turno
    assert [m["nome"] for m in triagem.modelos("11:00", "23:00")] == ["Abertura", "Fechamento", "Integral"]
    assert triagem.modelos("11:00", "23:00")[0] == {"nome": "Abertura", "inicio": "11:00", "fim": "17:30"}
    assert triagem.modelos("11:00", "23:00")[1] == {"nome": "Fechamento", "inicio": "16:30", "fim": "23:00"}
    assert triagem.modelos("09:00", "17:00") == [{"nome": "Integral", "inicio": "09:00", "fim": "17:00"}]
    assert [m["nome"] for m in triagem.modelos("00:00", "23:59")] == ["Manhã", "Tarde", "Noite"]
    assert triagem.modelos("18:00", "02:00") == [{"nome": "Integral", "inicio": "18:00", "fim": "02:00"}]
    assert triagem.modelos("16:00", "03:00")[1]["fim"] == "03:00"

    g = novo()
    r = g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Pike Grill", "nome": "Gil",
                                  "email": "gil@pike.com", "senha": "segredo1"})
    assert r.headers["location"] == "/painel/comecar"
    t = g.get("/painel/comecar").text
    assert "Vamos configurar" in t and "Restaurante ou bar" in t and 'data-passo="7"' in t
    assert "Configure em 2 minutos" in g.get("/painel").text
    tg = csrf(g, "/painel/comecar")

    r = g.post("/painel/comecar", data={"csrf": tg, **{**RESPOSTAS, "segmento": "x"}})
    assert r.status_code == 400 and "Escolha o tipo do negócio" in r.text
    r = g.post("/painel/comecar", data={"csrf": tg, **{**RESPOSTAS, "fecha": "11:00"}})
    assert r.status_code == 400 and "hora em que abre" in r.text

    r = g.post("/painel/comecar", data={"csrf": tg, **{**RESPOSTAS, "estado": "XX"}})
    assert r.status_code == 400 and "Escolha o estado (UF)" in r.text
    r = g.post("/painel/comecar", data={"csrf": tg, **{**RESPOSTAS, "frequencia": "biweekly"}})
    assert r.status_code == 400 and "Escolha como as horas a mais são tratadas" in r.text

    r = g.post("/painel/comecar", data={"csrf": tg, **RESPOSTAS, "local_nome": ["Centro", "Shopping", ""]})
    assert r.headers["location"] == "/painel/comecar/pronto"
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Pike Grill"))
        assert (e.segmento, e.porte, e.uf, e.fuso) == ("restaurante", "16-30", "MT", "America/Manaus")
        assert (e.abre, e.fecha, e.controle, e.provedor_folha) == ("11:00", "23:00", "extra", "noponto")
        assert e.saida_auto_min == 60 and e.entrada_antecipada_min == 15 and e.triagem_em
        funcoes = {f["nome"]: f["gorjeta"] for f in json.loads(e.funcoes_padrao)}
        assert funcoes["Garçom"] is True and funcoes["Cozinha"] is False
        assert len(json.loads(e.modelos_turno)) == 3
        assert sorted(l.nome for l in db.scalars(select(Local).where(Local.empresa_id == e.id))) == ["Centro", "Shopping"]
        assert db.scalar(select(Tarefa).where(Tarefa.empresa_id == e.id, Tarefa.titulo == "Fechar o caixa"))
        n_tarefas = len(list(db.scalars(select(Tarefa).where(Tarefa.empresa_id == e.id))))

    p = g.get("/painel/comecar/pronto").text
    assert "Pronto! Pike Grill está configurada" in p
    assert "Mato Grosso: fuso horário America/Manaus" in p
    assert "Salário mínimo em uso: R$ 1.621,00 por mês" in p, p
    assert "Hora extra paga no mês" in p and "Abertura 11:00–17:30" in p
    assert "Criar totem" in p and "data de nascimento" in p and "Definir o local" in p and "Abrir Pagamento" in p
    assert "Configure em 2 minutos" not in g.get("/painel").text

    # usar no app: modelos na escala, sugestões na ficha
    esc = g.get("/painel/escala").text
    assert 'data-ini="11:00" data-fim="17:30"' in esc
    assert "Garçom" in g.get("/painel/equipe/novo").text

    # refazer: não duplica tarefas
    g.post("/painel/comecar", data={"csrf": tg, **{**RESPOSTAS, "segmento": "cafe", "gorjeta": "nao"}})
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Pike Grill"))
        assert len(list(db.scalars(select(Tarefa).where(Tarefa.empresa_id == e.id)))) == n_tarefas
        assert all(not f["gorjeta"] for f in json.loads(e.funcoes_padrao))

    # só celular, sem menores e com contador: sem totem, sem data de nascimento, sem Pagamento
    h = novo()
    h.post("/cadastro", data={"csrf": csrf(h, "/cadastro"), "aceito": "1", "empresa": "Loja SP", "nome": "Hal",
                              "email": "hal@sp.com", "senha": "segredo1"})
    th = csrf(h, "/painel/comecar")
    h.post("/painel/comecar", data={"csrf": th, **{**RESPOSTAS, "estado": "SP", "segmento": "varejo",
                                                    "locais": "1", "ponto": "celular", "menores": "nao",
                                                    "provedor": "contador", "frequencia": "banco"}})
    p = h.get("/painel/comecar/pronto").text
    assert "São Paulo: fuso horário America/Sao_Paulo" in p and "Banco de horas" in p
    assert "Criar totem" not in p and "data de nascimento" not in p and "Abrir Pagamento" not in p
    print("OK: configuração guiada passou")


if __name__ == "__main__":
    main()
