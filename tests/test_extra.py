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
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Padaria", nome="Gil", email="gil@d.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    posta(g, "/painel/config", tg, nome="Padaria", lat="-2.53", lng="-44.30", raio_m="150",
          tolerancia_min="10", fuso="America/Sao_Paulo", bloquear_fora="1", ajuste_dias="7",
          fotos_dias="45", controle="extra", intervalo_padrao_min="60", intervalo_auto="1",
          tolerancia_dia_min="10")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bia Reis", "98999990002")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
        db.get(Usuario, ids["Bia"]).classificacao = "pj"   # prestador PJ: sem hora extra, sem aviso
        db.commit()
    # padrão: aviso 30 min antes do fim da jornada; a regra aparece na tela das regras automáticas
    assert 'name="aviso_extra_min" min="0" max="720" value="30"' in g.get("/painel/ponto/problemas").text
    # segunda: escala 08:00–17:00 com 1 h de intervalo = jornada de 8 h
    seg = DOM + timedelta(days=1)
    for nome in ("Ana", "Bia"):
        posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=seg.isoformat(), modo="trabalho",
              inicio="08:00", fim="17:00", intervalo="60", repetir="dia")
    as_("13:00", seg)
    for nome in ("Ana", "Bia"):
        for tipo, hora in (("entrada", "08:00"), ("saida", "12:00"), ("entrada", "13:00")):
            posta(g, "/painel/ponto/manual", tg, usuario_id=ids[nome], tipo=tipo, data=seg.isoformat(),
                  hora=hora, obs="t")
    with SessionLocal() as db:
        as_("16:20", seg)          # 7h20: ainda não
        assert extra.verificar(db) == []
        as_("16:35", seg)          # 7h35: faltam menos de 30 min
        novos = extra.verificar(db)
        assert [a.tipo for a in novos] == ["perto_extra"] and novos[0].usuario_id == ids["Ana"], novos
        assert "a jornada termina daqui a 0h25" in novos[0].texto, novos[0].texto
        as_("16:45", seg)
        assert extra.verificar(db) == []          # um aviso de cada tipo por dia
        as_("17:15", seg)          # 8h15: passou da jornada mais a tolerância de 10 min
        novos = extra.verificar(db)
        assert [a.tipo for a in novos] == ["hora_extra"] and "está em hora extra" in novos[0].texto
        as_("18:00", seg)
        assert extra.verificar(db) == []
        as_("19:05", seg)          # 10h05: mais de 2 h extras (CLT art. 59)
        novos = extra.verificar(db)
        assert [a.tipo for a in novos] == ["limite_extra"] and "até 2 horas por dia" in novos[0].texto
    assert "Perto da hora extra" in push.TITULOS.values()

    # regra desligada: nada
    posta(g, "/painel/config/regras-ponto", tg, entrada_antecipada_min="0", saida_auto_min="0", aviso_extra_min="0")
    proxima = seg + timedelta(days=7)
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
