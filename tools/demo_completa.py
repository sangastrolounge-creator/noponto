"""Empresa de DEMONSTRAÇÃO completa (equipe CLT, escala de duas semanas, ponto batido, hora extra,
admissões antigas para férias) no banco LOCAL, para ver as telas no navegador antes de publicar.
Criada pelas próprias telas do sistema, então também confere que o cadastro funciona.

Rodar da raiz (o visualizar.bat já faz isso, num banco separado dados/visualizar.db):
    .venv/Scripts/python tools/demo_completa.py

Login de teste (só existe nesse banco local): demo@noponto.test / demo1234
"""
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SEM_AGENDADOR", "1")
os.environ.setdefault("IDIOMA_PADRAO", "pt")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

EMAIL, SENHA = "demo@noponto.test", "demo1234"
EQUIPE = [  # nome, sobrenome, celular, função, salário, admissão (dias atrás), gorjeta
    ("Ana", "Souza", "11988880001", "Garçom", "2.100,00", 800, True),
    ("Bruno", "Lima", "11988880002", "Cozinheiro", "2.600,00", 400, False),
    ("Carla", "Reis", "11988880003", "Caixa", "1.900,00", 120, False),
    ("Diego", "Alves", "11988880004", "Garçom", "2.100,00", 30, True),
]
HORARIOS = {"Ana": ("11:00", "19:00"), "Bruno": ("09:00", "17:00"),
            "Carla": ("12:00", "20:00"), "Diego": ("15:00", "23:00")}


def _csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def _posta(cli, caminho, token, **dados):
    r = cli.post(caminho, data={"csrf": token, **dados})
    if r.status_code >= 400:
        raise SystemExit(f"falhou {caminho}: {r.status_code} {r.text[:300]}")
    return r


def criar(cli=None, hoje: date | None = None) -> dict:
    g = cli or TestClient(app, follow_redirects=False)
    hoje = hoje or date.today()
    _posta(g, "/cadastro", _csrf(g, "/cadastro"), aceito="1", empresa="Restaurante Demonstração",
           nome="Gestor Demo", email=EMAIL, senha=SENHA)
    tg = _csrf(g, "/painel/equipe/novo")
    for nome, sob, cel, funcao, sal, dias, gorjeta in EQUIPE:
        dados = dict(nome=nome, sobrenome=sob, celular=cel, nivel="funcionario", classificacao="clt",
                     funcao_0=funcao, valor_0=sal, tipo_0="mensal", na_escala="1",
                     data_admissao=(hoje - timedelta(days=dias)).isoformat())
        if gorjeta:
            dados["gorjeta"] = "1"
        _posta(g, "/painel/equipe/novo", tg, **dados)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(
            select(Usuario).where(Usuario.papel != "gestor"))}
    # escala da semana passada e desta; ponto batido nos dias que já passaram
    seg = hoje - timedelta(days=hoje.weekday() + 7)
    for i in range(14):
        d = seg + timedelta(days=i)
        if d.weekday() == 6:
            continue
        for nome, (ini, fim) in HORARIOS.items():
            _posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=d.isoformat(),
                   modo="trabalho", inicio=ini, fim=fim, intervalo="60", repetir="dia")
            if d >= hoje:
                continue
            saida = "19:00" if nome == "Bruno" and d.weekday() in (1, 3) else fim  # hora extra
            for tipo, hora in (("entrada", ini), ("saida", saida)):
                _posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo,
                       data=d.isoformat(), hora=hora, obs="demonstração")
    return ids


if __name__ == "__main__":
    ids = criar()
    print(f"Demonstração criada: {len(ids)} funcionários. Login: {EMAIL} / {SENHA}")
