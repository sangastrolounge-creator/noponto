"""Ficha da pessoa: cadastro numa página própria com funções e salários (hora ou anual), nível de
acesso (Funcionário, Gerente, Gerente Geral) e classificação tributária (W-2 / 1099).
Rodar da raiz: .venv/Scripts/python tests/test_ficha.py"""
import os
import re
import sys
import tempfile
from datetime import date, datetime
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
from app import pagamento, trocas  # noqa: E402
from app.core import PERMISSOES  # noqa: E402
from app.db import FuncaoUsuario, Oferta, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 18, 15, 0)}
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def pessoa(email=None, celular=None):
    with SessionLocal() as db:
        q = Usuario.email == email if email else Usuario.celular == celular
        return db.scalar(select(Usuario).where(q))


def main():
    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Padaria", nome="Dora", email="dora@d.com", senha="segredo1")
    tg = csrf(g, "/painel/equipe/novo")
    assert 'href="/painel/equipe/novo"' in g.get("/painel/equipe").text
    tela = g.get("/painel/equipe/novo").text
    for txt in ("Nível de acesso", "Gerente Geral", "Tipo de contrato", "Empregado CLT", "Prestador PJ",
                "Jornada mensal (horas)", "Dependentes (IRRF)", "Data de admissão"):
        assert txt in tela, txt

    # ---- funcionário: celular obrigatório
    r = posta(g, "/painel/equipe/novo", tg, nome="Ana", sobrenome="Souza", nivel="funcionario", classificacao="clt")
    assert r.status_code == 400 and "informe o celular" in r.text and 'value="Ana"' in r.text
    # ---- funcionária com duas funções: garçom (mensal, com gorjeta) e caixa por hora
    r = posta(g, "/painel/equipe/novo", tg, nome="Ana", sobrenome="Souza", celular="98 99999-0001",
              email="", nivel="funcionario", classificacao="clt", data_admissao="2026-11-02",
              funcao_0="Garçom", valor_0="1.800,00", tipo_0="mensal", funcao_1="Caixa", valor_1="12,50",
              tipo_1="hora", gorjeta="1", jornada_mensal="220", dependentes="1", vt="180,00")
    assert r.status_code == 303, r.text[-1500:]
    ana = pessoa(celular="98999990001")
    assert (ana.nome, ana.papel, ana.cargo, ana.salario_centavos, ana.gorjeta) == ("Ana Souza", "funcionario", "Garçom", 180000, True)
    assert (ana.jornada_mensal, ana.dependentes, ana.vt_centavos) == (220, 1, 18000)
    assert ana.data_admissao == date(2026, 11, 2) and ana.classificacao == "clt" and ana.convite_token
    with SessionLocal() as db:
        assert [(f.nome, f.valor_centavos, f.tipo) for f in db.scalars(select(FuncaoUsuario).order_by(FuncaoUsuario.ordem))] == \
            [("Garçom", 180000, "mensal"), ("Caixa", 1250, "hora")]
        a = db.get(Usuario, ana.id)
        assert trocas.cargo_serve(Oferta(cargo="caixa"), a) and not trocas.cargo_serve(Oferta(cargo="Cozinha"), a)
    # ---- valor inválido, jornada inválida e celular repetido
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", celular="98999990002", nivel="funcionario",
              classificacao="clt", funcao_0="Cozinha", valor_0="abc", tipo_0="mensal")
    assert r.status_code == 400 and "Confira o valor" in r.text
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", celular="98999990002", nivel="funcionario",
              classificacao="clt", jornada_mensal="999")
    assert r.status_code == 400 and "Jornada mensal" in r.text
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", celular="98999990001", nivel="funcionario", classificacao="clt")
    assert r.status_code == 400 and "já está cadastrado" in r.text
    # ---- por hora: o salário de referência é o valor da hora x a jornada do mês
    posta(g, "/painel/equipe/novo", tg, nome="Bia", celular="98999990002", nivel="funcionario",
          classificacao="intermitente", funcao_0="Cozinha", valor_0="10,00", tipo_0="hora", jornada_mensal="100")
    assert pessoa(celular="98999990002").salario_centavos == 100000

    # ---- gerente geral: todas as permissões, entra com e-mail
    r = posta(g, "/painel/equipe/novo", tg, nome="Lia", sobrenome="Lima", nivel="gerente_geral",
              classificacao="clt", funcao_0="Gerente", valor_0="4.500,00", tipo_0="mensal")
    assert r.status_code == 400 and "informe um e-mail" in r.text
    posta(g, "/painel/equipe/novo", tg, nome="Lia", sobrenome="Lima", email="lia@d.com", nivel="gerente_geral",
          classificacao="clt", funcao_0="Gerente", valor_0="4.500,00", tipo_0="mensal", na_escala="")
    lia = pessoa(email="lia@d.com")
    assert lia.papel == "gestor" and not lia.dono and set(lia.permissoes.split(",")) == set(PERMISSOES)
    assert lia.salario_centavos == 450000 and not lia.na_escala
    assert "4500,00" in g.get(f"/painel/equipe/{lia.id}/ficha").text
    # ---- gerente comum: permissões padrão
    posta(g, "/painel/equipe/novo", tg, nome="Rui", email="rui@d.com", nivel="gerente", classificacao="clt",
          na_escala="1")
    rui = pessoa(email="rui@d.com")
    assert rui.permissoes == "escala,ponto,mural,tarefas,ausencias" and rui.na_escala

    # ---- Lia (gerente geral) entra; pode cadastrar funcionário, não gerente
    lc = novo()
    posta(lc, f"/convite/{lia.convite_token}", csrf(lc, f"/convite/{lia.convite_token}"), senha="senha123")
    tl = csrf(lc, "/painel/equipe/novo")
    r = posta(lc, "/painel/equipe/novo", tl, nome="X", email="x@d.com", nivel="gerente", classificacao="clt")
    assert r.status_code == 400 and "Só o dono" in r.text
    r = posta(lc, "/painel/equipe/novo", tl, nome="Caio", celular="98999990003", nivel="funcionario",
              classificacao="pj", funcao_0="Entregas", valor_0="2.000,00", tipo_0="mensal")
    assert r.status_code == 303
    caio = pessoa(celular="98999990003")
    assert caio.classificacao == "pj"
    with SessionLocal() as db:   # PJ fica fora da folha de pagamento
        assert caio.id not in {p.id for p in pagamento._equipe(db, db.get(Usuario, caio.id).empresa)}
    # ela não muda o nível de ninguém
    r = posta(lc, f"/painel/equipe/{caio.id}/ficha", tl, nome="Caio", celular="98999990003", nivel="gerente",
              email="c@d.com", classificacao="pj")
    assert r.status_code == 400 and "nível de acesso" in r.text

    # ---- dono promove Ana a gerente (precisa de e-mail) e depois volta a funcionária
    base = dict(nome="Ana", sobrenome="Souza", celular="98999990001", classificacao="clt",
                funcao_0="Garçom", valor_0="1.800,00", tipo_0="mensal")
    r = posta(g, f"/painel/equipe/{ana.id}/ficha", tg, nivel="gerente", gorjeta="1", **base)
    assert r.status_code == 400
    posta(g, f"/painel/equipe/{ana.id}/ficha", tg, email="ana@d.com", nivel="gerente", gorjeta="1",
          na_escala="1", **base)
    assert pessoa(email="ana@d.com").papel == "gestor"
    posta(g, f"/painel/equipe/{ana.id}/ficha", tg, email="ana@d.com", nivel="funcionario", **base)
    a = pessoa(email="ana@d.com")
    assert a.papel == "funcionario" and a.permissoes == "" and a.na_escala and not a.gorjeta
    print("OK: ficha da pessoa passou")


if __name__ == "__main__":
    main()
