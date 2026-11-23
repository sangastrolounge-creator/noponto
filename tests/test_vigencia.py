"""Vigência do ponto: a pessoa recebe o convite antes, conhece o app, mas só bate o ponto a partir
da data definida na ficha (ou na planilha de importação). Rodar da raiz: python tests/test_vigencia.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import regras  # noqa: E402
from app.db import Batida, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 23, 15, 0)}
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    m = novo()
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Bar Mar", "nome": "Gil",
                              "email": "gil@bar.com", "senha": "segredo1"})
    t = csrf(g, "/painel/equipe")
    g.post("/painel/equipe", data={"csrf": t, "nome": "Ana Souza", "celular": "98999990001"})
    with SessionLocal() as db:
        aid = db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza")).id

    # ---- a ficha mostra o campo e guarda a data
    tela = g.get(f"/painel/equipe/{aid}/ficha").text
    assert 'name="ponto_desde"' in tela and "Vigência do ponto" in tela
    r = g.post(f"/painel/equipe/{aid}/ficha", data={"csrf": t, "nome": "Ana Souza", "celular": "98999990001",
                                                    "nivel": "funcionario", "classificacao": "clt",
                                                    "data_admissao": "2026-11-23", "ponto_desde": "2026-12-01"})
    assert r.status_code == 303, r.text[:500]
    with SessionLocal() as db:
        ana = db.get(Usuario, aid)
        assert ana.ponto_desde == date(2026, 12, 1) and ana.data_admissao == date(2026, 11, 23)

    # ---- data inválida é recusada
    r = g.post(f"/painel/equipe/{aid}/ficha", data={"csrf": t, "nome": "Ana Souza", "celular": "98999990001",
                                                    "nivel": "funcionario", "classificacao": "clt",
                                                    "ponto_desde": "31/02/2026x"})
    assert "Data de vigência inválida" in r.text

    # ---- em branco: libera na hora
    r = g.post(f"/painel/equipe/{aid}/ficha", data={"csrf": t, "nome": "Ana Souza", "celular": "98999990001",
                                                    "nivel": "funcionario", "classificacao": "clt",
                                                    "ponto_desde": ""})
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(Usuario, aid).ponto_desde is None

    r = g.post(f"/painel/equipe/{aid}/ficha", data={"csrf": t, "nome": "Ana Souza", "celular": "98999990001",
                                                    "nivel": "funcionario", "classificacao": "clt",
                                                    "data_admissao": "2026-11-23", "ponto_desde": "2026-12-01"})
    assert r.status_code == 303

    # ---- antes da vigência: não bate o ponto
    with SessionLocal() as db:
        try:
            regras.bater(db, db.get(Usuario, aid), None, None, None, aparelho="aparelho-ana-123", ua="teste")
            raise AssertionError("bateu antes da vigência")
        except regras.ErroPonto as erro:
            assert "01/12/2026" in str(erro)
        assert not db.scalar(select(Batida.id).where(Batida.usuario_id == aid).limit(1))

    # ---- no dia da vigência: bate normalmente
    relogio["agora"] = datetime(2026, 12, 1, 12, 0)
    with SessionLocal() as db:
        b, _ = regras.bater(db, db.get(Usuario, aid), None, None, None, aparelho="aparelho-ana-123", ua="teste")
        assert b.id and b.usuario_id == aid

    # ---- importação por planilha aceita a coluna Vigência
    from app import importar
    with SessionLocal() as db:
        eid = db.get(Usuario, aid).empresa_id
        linhas, erro = importar.ler("Nome;Celular;Admissão;Vigência\r\n"
                                    "Bia Lima;98 99999-0202;23/11/2026;01/12/2026\r\n", db, eid)
    assert not erro and linhas[0]["dados"]["vigencia"] == date(2026, 12, 1), (erro, linhas)
    print("ok: vigência do ponto")


if __name__ == "__main__":
    main()
