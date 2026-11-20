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
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Dora", email="dora@d.com", senha="segredo1")
    tg = csrf(g, "/painel/equipe/novo")
    assert 'href="/painel/equipe/novo"' in g.get("/painel/equipe").text
    tela = g.get("/painel/equipe/novo").text
    for txt in ("Funções e salários", "Nível de acesso", "Gerente Geral", "Classificação tributária", "Recebe 1099"):
        assert txt in tela, txt

    # ---- funcionário: celular obrigatório
    r = posta(g, "/painel/equipe/novo", tg, nome="Ana", sobrenome="Souza", nivel="funcionario", classificacao="w2")
    assert r.status_code == 400 and "informe o celular" in r.text and 'value="Ana"' in r.text
    # ---- funcionário com duas funções: garçom por hora (com gorjeta) e hostess
    r = posta(g, "/painel/equipe/novo", tg, nome="Ana", sobrenome="Souza", celular="305 555 0001",
              email="", nivel="funcionario", classificacao="w2", data_admissao="2026-11-02",
              funcao_0="Server", valor_0="4.80", tipo_0="hora", funcao_1="Host", valor_1="15.00", tipo_1="hora",
              gorjeta="1")
    assert r.status_code == 303
    ana = pessoa(celular="3055550001")
    assert (ana.nome, ana.papel, ana.cargo, ana.taxa_hora_centavos, ana.gorjeta) == ("Ana Souza", "funcionario", "Server", 480, True)
    assert ana.data_admissao == date(2026, 11, 2) and ana.classificacao == "w2" and ana.convite_token
    with SessionLocal() as db:
        assert [(f.nome, f.valor_centavos) for f in db.scalars(select(FuncaoUsuario).order_by(FuncaoUsuario.ordem))] == \
            [("Server", 480), ("Host", 1500)]
        a = db.get(Usuario, ana.id)
        assert trocas.cargo_serve(Oferta(cargo="host"), a) and not trocas.cargo_serve(Oferta(cargo="Cook"), a)
    # ---- valor inválido e celular repetido
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", celular="3055550002", nivel="funcionario",
              classificacao="w2", funcao_0="Cook", valor_0="abc", tipo_0="hora")
    assert r.status_code == 400 and "Confira o valor" in r.text
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", celular="3055550001", nivel="funcionario", classificacao="w2")
    assert r.status_code == 400 and "já está cadastrado" in r.text

    # ---- gerente geral com salário anual: exempt, todas as permissões, entra com e-mail
    r = posta(g, "/painel/equipe/novo", tg, nome="Lia", sobrenome="Lima", nivel="gerente_geral",
              classificacao="w2", funcao_0="General manager", valor_0="52000", tipo_0="anual")
    assert r.status_code == 400 and "informe um e-mail" in r.text
    posta(g, "/painel/equipe/novo", tg, nome="Lia", sobrenome="Lima", email="lia@d.com", nivel="gerente_geral",
          classificacao="w2", funcao_0="General manager", valor_0="52000", tipo_0="anual", na_escala="")
    lia = pessoa(email="lia@d.com")
    assert lia.papel == "gestor" and not lia.dono and set(lia.permissoes.split(",")) == set(PERMISSOES)
    assert lia.isento and lia.salario_periodo_centavos == 5200000 // 26 and not lia.na_escala
    # a ficha mostra o salário anual de volta
    assert 'value="52000.00"' in g.get(f"/painel/equipe/{lia.id}/ficha").text
    # ---- gerente comum: permissões padrão
    posta(g, "/painel/equipe/novo", tg, nome="Rui", email="rui@d.com", nivel="gerente", classificacao="w2",
          na_escala="1")
    rui = pessoa(email="rui@d.com")
    assert rui.permissoes == "escala,ponto,mural,tarefas,ausencias" and rui.na_escala

    # ---- Lia (gerente geral) entra; pode cadastrar funcionário, não gerente
    lc = novo()
    posta(lc, f"/convite/{lia.convite_token}", csrf(lc, f"/convite/{lia.convite_token}"), senha="senha123")
    tl = csrf(lc, "/painel/equipe/novo")
    assert 'value="gerente" disabled' in lc.get("/painel/equipe/novo").text.replace("  ", " ") or "desligada" in lc.get("/painel/equipe/novo").text
    r = posta(lc, "/painel/equipe/novo", tl, nome="X", email="x@d.com", nivel="gerente", classificacao="w2")
    assert r.status_code == 400 and "Só o dono" in r.text
    r = posta(lc, "/painel/equipe/novo", tl, nome="Caio", celular="3055550003", nivel="funcionario", classificacao="1099_ind",
              funcao_0="Dishwasher", valor_0="16", tipo_0="hora")
    assert r.status_code == 303
    caio = pessoa(celular="3055550003")
    assert caio.classificacao == "1099_ind"
    # ela não muda o nível de ninguém
    r = posta(lc, f"/painel/equipe/{caio.id}/ficha", tl, nome="Caio", celular="3055550003", nivel="gerente",
              email="c@d.com", classificacao="1099_ind")
    assert r.status_code == 400 and "nível de acesso" in r.text

    # ---- dono promove Ana a gerente (precisa de e-mail) e depois volta a funcionária
    r = posta(g, f"/painel/equipe/{ana.id}/ficha", tg, nome="Ana", sobrenome="Souza", celular="3055550001",
              nivel="gerente", classificacao="w2", funcao_0="Server", valor_0="4.80", tipo_0="hora", gorjeta="1")
    assert r.status_code == 400
    posta(g, f"/painel/equipe/{ana.id}/ficha", tg, nome="Ana", sobrenome="Souza", celular="3055550001",
          email="ana@d.com", nivel="gerente", classificacao="w2", funcao_0="Server", valor_0="4.80", tipo_0="hora",
          gorjeta="1", na_escala="1")
    assert pessoa(email="ana@d.com").papel == "gestor"
    posta(g, f"/painel/equipe/{ana.id}/ficha", tg, nome="Ana", sobrenome="Souza", celular="3055550001",
          email="ana@d.com", nivel="funcionario", classificacao="w2", funcao_0="Server", valor_0="4.80", tipo_0="hora")
    a = pessoa(email="ana@d.com")
    assert a.papel == "funcionario" and a.permissoes == "" and a.na_escala and not a.gorjeta

    # ---- prestador 1099: horas extras viram horas normais no cálculo do pagamento
    it = SimpleNamespace(usuario=SimpleNamespace(classificacao="1099_ind"))
    linha = SimpleNamespace(**{k: 0 for k in pagamento.apuracao.CHAVES})
    linha.regular, linha.ot, linha.dt, linha.meal_premios = 2400, 300, 60, 60
    h = pagamento._horas_para_calculo(it, linha)
    assert (h["regular"], h["ot"], h["dt"], h["meal_premios"]) == (2760, 0, 0, 0)
    it.usuario.classificacao = "w2"
    assert pagamento._horas_para_calculo(it, linha)["ot"] == 300
    print("OK: ficha da pessoa passou")


if __name__ == "__main__":
    main()
