"""Aviso antes da hora extra e avisos no celular só para quem tem a permissão.
Rodar da raiz: .venv/Scripts/python tests/test_extra.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import extra, push  # noqa: E402
from app.db import Alerta, PushSub, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

DOM = date(2026, 11, 15)
relogio = {"agora": None}
banco.agora = lambda: relogio["agora"]


def as_(hora, d):
    h, m = hora.split(":")
    relogio["agora"] = datetime(d.year, d.month, d.day, int(h), int(m)) + timedelta(hours=3)


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def alertas(tipo):
    with SessionLocal() as db:
        return [a.texto for a in db.scalars(select(Alerta).where(Alerta.tipo == tipo))]


def main():
    as_("07:00", DOM)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Gil", email="gil@d.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Diner", lat="25.77", lng="-80.19", raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", estado="FL", semana_inicio="6", frequencia="weekly",
          periodo_ancora=DOM.isoformat(), intervalo_padrao_min="30", tolerancia_dia_min="7",
          pct_noturno="0", pct_feriado="0")
    for nome, cel in (("Ana Souza", "3055550001"), ("Bia Reis", "3055550002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        db.get(Usuario, ids["Bia"]).isento = True   # exempt: sem hora extra, sem aviso
        db.commit()
    # padrão: aviso 120 min antes; a regra aparece na tela das regras automáticas
    assert 'name="aviso_extra_min" min="0" max="720" value="120"' in g.get("/painel/ponto/problemas").text
    # segunda a quinta: 9 h por dia (36 h)
    as_("20:00", DOM + timedelta(days=4))
    for i in range(1, 5):
        for nome in ("Ana", "Bia"):
            for tipo, hora in (("entrada", "08:00"), ("saida", "17:00")):
                posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo,
                      data=(DOM + timedelta(days=i)).isoformat(), hora=hora, obs="t")
    sex = DOM + timedelta(days=5)
    as_("09:00", sex)
    for nome in ("Ana", "Bia"):
        posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo="entrada", data=sex.isoformat(),
              hora="09:00", obs="t")
    with SessionLocal() as db:
        as_("10:30", sex)          # 37h30: ainda não
        assert extra.verificar(db) == []
        as_("11:05", sex)          # 38h05: faltam menos de 2 h
        novos = extra.verificar(db)
        assert [a.tipo for a in novos] == ["perto_extra"] and novos[0].usuario_id == ids["Ana"]
        assert "a hora extra começa daqui a 1h55" in novos[0].texto, novos[0].texto
        as_("11:30", sex)
        assert extra.verificar(db) == []          # um aviso por semana
        as_("13:01", sex)          # 40h01: em hora extra
        novos = extra.verificar(db)
        assert [a.tipo for a in novos] == ["hora_extra"] and "está em hora extra" in novos[0].texto
        as_("15:00", sex)
        assert extra.verificar(db) == []
    assert "Perto da hora extra" in push.TITULOS.values()

    # regra desligada: nada
    posta(g, "/painel/config/regras-ponto", tg, entrada_antecipada_min="0", saida_auto_min="0", aviso_extra_min="0")
    proxima = sex + timedelta(days=9)
    as_("09:00", proxima)
    with SessionLocal() as db:
        assert extra.verificar(db) == []

    # ---- avisos no celular: o gerente sem a permissão Ponto não recebe alertas de ponto
    posta(g, "/painel/gerentes", tg, nome="Lia", email="lia@d.com", p_mural="1")
    with SessionLocal() as db:
        lia = db.scalar(select(Usuario).where(Usuario.email == "lia@d.com"))
        gil = db.scalar(select(Usuario).where(Usuario.email == "gil@d.com"))
        for u in (lia, gil):
            db.add(PushSub(usuario_id=u.id, endpoint=f"https://push.test/{u.id}", p256dh="x", auth="y"))
        db.commit()
        enviados = []
        original = push._mandar
        push._mandar = lambda db_, subs, *a, **k: enviados.append(sorted(s.usuario_id for s in subs)) or len(subs)
        push.enviar(db, 1, "t", "x", permissao="ponto")
        push.enviar(db, 1, "t", "x", permissao="mural")
        push.enviar(db, 1, "t", "x")
        push._mandar = original
    assert enviados == [[gil.id], sorted([gil.id, lia.id]), sorted([gil.id, lia.id])], enviados
    print("OK: aviso de hora extra e avisos por permissão passou")


if __name__ == "__main__":
    main()
