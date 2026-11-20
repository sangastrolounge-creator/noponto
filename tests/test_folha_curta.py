"""Folha de ponto: observações curtas, coluna de horas noturnas e suspensão disciplinar.
Rodar da raiz: .venv/Scripts/python tests/test_folha_curta.py"""
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

from app import apuracao, push  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Empresa, SessionLocal, Usuario  # noqa: E402
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


def bate(g, tg, uid, tipo, d, hora):
    as_(hora, d)
    posta(g, "/painel/ponto/manual", tg, usuario_id=uid, tipo=tipo, data=d.isoformat(), hora=hora, obs="t")


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Bar", nome="Gil", email="gil@b.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/equipe", tg, nome="Mario Dias", celular="98999990009")
    with SessionLocal() as db:
        mid = db.scalar(select(Usuario.id).where(Usuario.nome == "Mario Dias"))
        db.get(Empresa, 1).pct_noturno = 20
        db.commit()
    for i in range(4):   # seg a qui, 15h às 23h
        posta(g, "/painel/escala/turno", tg, usuario_id=mid, data=(SEG + timedelta(days=i)).isoformat(),
              modo="trabalho", inicio="15:00", fim="23:00", intervalo="60", repetir="dia")
    # seg: trabalha 15h-17h e 18h-23h (1h depois das 22h); ter: falta; qua: suspensão; qui: atestado
    bate(g, tg, mid, "entrada", SEG, "15:00")
    bate(g, tg, mid, "saida", SEG, "17:00")
    bate(g, tg, mid, "entrada", SEG, "18:00")
    bate(g, tg, mid, "saida", SEG, "23:00")
    qua, qui = SEG + timedelta(days=2), SEG + timedelta(days=3)
    posta(g, "/painel/ausencias", tg, usuario_id=mid, tipo="suspensao", inicio=qua.isoformat(), fim=qua.isoformat())
    posta(g, "/painel/ausencias", tg, usuario_id=mid, tipo="atestado", inicio=qui.isoformat(), fim=qui.isoformat())
    as_("09:00", SEG + timedelta(days=4))
    with SessionLocal() as db:
        r = apuracao.apurar(db, db.get(Empresa, 1), SEG, qui, mid)[0]
    dias = {d["dia"]: d for d in r["dias"]}
    assert dias[SEG]["marcas"] == [] and dias[SEG]["noturno"] >= 60, dias[SEG]
    assert dias[SEG + timedelta(days=1)]["marcas"] == ["Falta"]
    assert dias[qua]["marcas"] == ["Suspensão"] and dias[qua]["falta"]
    assert dias[qui]["marcas"] == ["Atestado"] and not dias[qui]["falta"]
    # suspensão desconta como falta; atestado não
    assert r["faltas"] == 2 and r["falta_min"] == 2 * 7 * 60 and r["dias_ausencia"] == 1, r
    assert "Suspensão disciplinar" in g.get("/painel/ausencias").text

    folha = g.get(f"/painel/folha?func={mid}&de={SEG}&ate={qui}").text
    corpo = folha.split("<tbody>")[1].split("</tbody>")[0]
    assert "Falta" in corpo and "Suspensão" in corpo and "Atestado" in corpo
    assert "noturnas" not in corpo and "hora reduzida" not in corpo and "lançada à mão" not in corpo
    noite = regras_fmt(dias[SEG]["noturno"])
    assert "<th>Noturno</th>" in folha and noite in corpo
    assert noite in folha.split("<tfoot>")[1]                  # total do período

    print("OK: folha de ponto com observações curtas e noturno passou")


def regras_fmt(m):
    return f"{m // 60}h{m % 60:02d}"


if __name__ == "__main__":
    main()
