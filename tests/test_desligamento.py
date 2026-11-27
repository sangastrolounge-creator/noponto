"""Equipe: janela de desligamento (demitir, desfazer) e exclusão do cadastro.
Rodar da raiz: .venv/Scripts/python tests/test_desligamento.py"""
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

from app import push, regras  # noqa: E402
from app import db as banco  # noqa: E402
from app.db import Batida, EscalaFixa, Historico, SessionLocal, Turno, Usuario  # noqa: E402
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


def main():
    as_("07:00", SEG)
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Cafe", nome="Gil", email="gil@c.com", senha="segredo1")
    tg = csrf(g, "/painel/config")
    for nome, cel in (("Ana Souza", "98999990001"), ("Bruno Lima", "98999990002"), ("Caio Reis", "98999990003")):
        posta(g, "/painel/equipe", tg, nome=nome, celular=cel)
    with SessionLocal() as db:
        ids = {u.nome.split()[0]: u.id for u in db.scalars(select(Usuario))}
    for nome in ("Ana", "Bruno", "Caio"):
        for i in range(0, 14):
            posta(g, "/painel/escala/turno", tg, usuario_id=ids[nome], data=(SEG + timedelta(days=i)).isoformat(),
                  modo="trabalho", inicio="08:00", fim="12:00", intervalo="0", repetir="dia")
    with SessionLocal() as db:   # Ana já trabalhou: tem marcação de ponto
        db.add(Batida(empresa_id=1, usuario_id=ids["Ana"], tipo="entrada", dia=SEG - timedelta(days=2),
                     momento=relogio["agora"] - timedelta(days=2)))
        db.commit()

    tela = g.get("/painel/equipe").text
    assert tela.count('class="desl"') == 3 and "Gil" in tela             # o dono não tem a janela
    assert "Dispensa sem justa causa" in tela and "art. 477" in tela and "Confirmar desligamento" in tela
    assert tela.count("Este cadastro não pode ser excluído") == 1         # só a Ana
    assert 'value="desativar"' not in tela

    # ---- validações
    posta(g, f"/painel/equipe/{ids['Ana']}/desligar", tg, data="", tipo="pedido", aviso="trabalhado")
    assert "Informe o último dia" in g.get("/painel/equipe").text
    posta(g, f"/painel/equipe/{ids['Ana']}/desligar", tg, data=SEG.isoformat(), tipo="xx", aviso="trabalhado")
    assert "Escolha o tipo" in g.get("/painel/equipe").text
    posta(g, f"/painel/equipe/{ids['Ana']}/desligar", tg, data=(SEG + timedelta(days=200)).isoformat(),
          tipo="pedido", aviso="trabalhado")
    assert "90 dias" in g.get("/painel/equipe").text
    with SessionLocal() as db:
        dono = db.scalar(select(Usuario.id).where(Usuario.email == "gil@c.com"))
    posta(g, f"/painel/equipe/{dono}/desligar", tg, data=SEG.isoformat(), tipo="pedido", aviso="trabalhado")
    with SessionLocal() as db:
        assert db.get(Usuario, dono).ativo and not db.get(Usuario, dono).desligado_em

    # ---- Ana: desligada ontem -> perde o acesso já; turnos depois do último dia saem; o ponto fica
    ontem = SEG - timedelta(days=1)
    posta(g, f"/painel/equipe/{ids['Ana']}/desligar", tg, data=ontem.isoformat(), tipo="sem_justa_causa",
          aviso="indenizado", obs="Exame demissional marcado")
    with SessionLocal() as db:
        a = db.get(Usuario, ids["Ana"])
        assert not a.ativo and a.desligado_em == ontem and a.desligamento_tipo == "sem_justa_causa"
        assert a.aviso_previo == "indenizado" and a.convite_token is None and not a.na_escala
        assert not db.scalar(select(Turno.id).where(Turno.usuario_id == a.id, Turno.data > ontem))
        assert db.scalar(select(Batida.id).where(Batida.usuario_id == a.id))
        h = db.scalar(select(Historico).where(Historico.acao == "desligamento"))
        assert "Ana Souza" in h.detalhe and "Dispensa sem justa causa" in h.detalhe and h.usuario_id == a.id
    tela = g.get("/painel/equipe").text
    assert f"Desligado em {ontem:%d/%m/%Y}" in tela and "Desfazer desligamento" in tela
    assert "Ana Souza" in g.get("/painel/historico").text

    # ---- Caio: último dia daqui a 3 dias -> continua trabalhando até lá; o agendador encerra depois
    fim = SEG + timedelta(days=3)
    posta(g, f"/painel/equipe/{ids['Caio']}/desligar", tg, data=fim.isoformat(), tipo="pedido", aviso="trabalhado")
    with SessionLocal() as db:
        c = db.get(Usuario, ids["Caio"])
        assert c.ativo and c.desligado_em == fim
        datas = set(db.scalars(select(Turno.data).where(Turno.usuario_id == c.id)))
        assert max(datas) == fim and SEG in datas
    assert f"Último dia: {fim:%d/%m/%Y}" in g.get("/painel/equipe").text
    as_("07:00", fim)
    with SessionLocal() as db:
        regras.verificar_turnos(db)
        assert db.get(Usuario, ids["Caio"]).ativo                       # ainda é o último dia
    as_("07:00", fim + timedelta(days=1))
    with SessionLocal() as db:
        regras.verificar_turnos(db)
        assert not db.get(Usuario, ids["Caio"]).ativo
        assert not db.scalar(select(Turno.id).where(Turno.usuario_id == ids["Caio"], Turno.data > fim))

    # ---- desfazer: volta para a equipe, sem os dados do desligamento
    tg = csrf(g, "/painel/equipe")
    posta(g, f"/painel/equipe/{ids['Caio']}/desfazer-desligamento", tg)
    with SessionLocal() as db:
        c = db.get(Usuario, ids["Caio"])
        assert c.ativo and c.na_escala and c.desligado_em is None and c.desligamento_tipo == ""

    # ---- excluir quem tem ponto e não foi desligado: não pode (Caio trabalhou)
    with SessionLocal() as db:
        db.add(Batida(empresa_id=1, usuario_id=ids["Caio"], tipo="entrada", dia=SEG,
                      momento=relogio["agora"] - timedelta(days=3)))
        db.commit()
    posta(g, f"/painel/equipe/{ids['Caio']}/excluir", tg, confirmar="Caio Reis")
    assert "Primeiro faça o desligamento" in g.get("/painel/equipe").text
    with SessionLocal() as db:
        assert db.get(Usuario, ids["Caio"]).celular

    # ---- Ana (desligada ontem, com ponto): a exclusão apaga os dados pessoais e guarda o ponto 5 anos
    tela = g.get("/painel/equipe").text
    assert "O que acontece" in tela and "5 anos" in tela and "data-confirma" in tela
    posta(g, f"/painel/equipe/{ids['Ana']}/excluir", tg, confirmar="Ana")
    with SessionLocal() as db:
        assert db.get(Usuario, ids["Ana"]).cadastro_excluido_em is None      # nome errado: nada muda
    posta(g, f"/painel/equipe/{ids['Ana']}/excluir", tg, confirmar="ana souza")
    with SessionLocal() as db:
        a = db.get(Usuario, ids["Ana"])
        assert a.cadastro_excluido_em and a.apagar_registros_em == ontem.replace(year=ontem.year + 5)
        assert a.celular is None and a.email is None and a.senha_hash is None and a.nome == "Ana Souza"
        assert db.scalar(select(Batida.id).where(Batida.usuario_id == a.id))   # o ponto fica
        h = db.scalar(select(Historico).where(Historico.acao == "cadastro excluído", Historico.usuario_id == a.id))
        assert h and "dados pessoais apagados" in h.detalhe
    tela = g.get("/painel/equipe").text
    assert f'id="p{ids["Ana"]}"' not in tela                                 # some da Equipe
    posta(g, f"/painel/equipe/{ids['Ana']}/desfazer-desligamento", tg)
    with SessionLocal() as db:
        assert not db.get(Usuario, ids["Ana"]).ativo                        # não volta

    posta(g, f"/painel/equipe/{ids['Bruno']}/excluir", tg, confirmar="Bruno")
    assert "nome completo" in g.get("/painel/equipe").text
    posta(g, f"/painel/equipe/{ids['Bruno']}/excluir", tg, confirmar="  bruno   LIMA ")
    with SessionLocal() as db:
        assert db.get(Usuario, ids["Bruno"]) is None
        assert not db.scalar(select(Turno.id).where(Turno.usuario_id == ids["Bruno"]))
        assert not db.scalar(select(EscalaFixa.id).where(EscalaFixa.usuario_id == ids["Bruno"]))
        assert db.scalar(select(Historico).where(Historico.acao == "cadastro excluído"))
    tela = g.get("/painel/equipe").text
    assert f'id="p{ids["Bruno"]}"' not in tela and f"desl-{ids['Bruno']}" not in tela

    # ---- outra empresa não mexe em ninguém daqui
    h = novo()
    posta(h, "/cadastro", csrf(h, "/cadastro"), aceito="1", empresa="Loja", nome="Hugo", email="hugo@l.com",
          senha="segredo2")
    th = csrf(h, "/painel/config")
    posta(h, f"/painel/equipe/{ids['Caio']}/desligar", th, data=SEG.isoformat(), tipo="pedido", aviso="trabalhado")
    posta(h, f"/painel/equipe/{ids['Caio']}/excluir", th, confirmar="Caio Reis")
    with SessionLocal() as db:
        c = db.get(Usuario, ids["Caio"])
        assert c and c.ativo and c.desligado_em is None

    # ---- rotina diária: fim do prazo legal apaga o ponto da Ana; 1 ano depois some o histórico
    from app import desligamento
    with SessionLocal() as db:
        assert desligamento.limpar_excluidos(db, ontem.replace(year=ontem.year + 5) - timedelta(days=1))[0] == 0
        assert db.get(Usuario, ids["Ana"])
    as_("07:00", SEG + timedelta(days=400))
    with SessionLocal() as db:
        n, linhas = desligamento.limpar_excluidos(db, ontem.replace(year=ontem.year + 5))
        assert n == 1 and linhas >= 3
        assert db.get(Usuario, ids["Ana"]) is None
        assert not db.scalar(select(Batida.id).where(Batida.usuario_id == ids["Ana"]))
        assert not db.scalar(select(Historico.id).where(Historico.usuario_id.in_([ids["Ana"], ids["Bruno"]])))
        assert db.scalar(select(Historico.id).where(Historico.usuario_id == ids["Caio"]))   # quem segue fica
    print("OK: desligamento e exclusão do cadastro passou")


if __name__ == "__main__":
    main()
