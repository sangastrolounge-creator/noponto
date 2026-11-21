"""Ficha de RH: documentos (CPF, PIS, RG, CTPS...), dados pessoais, endereço e dados bancários.
Rodar da raiz: .venv/Scripts/python tests/test_rh.py"""
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/teste.db".replace("\\", "/")
os.environ["SECRET_KEY"] = "teste"
os.environ["IDIOMA_PADRAO"] = "pt"
os.environ["SEM_AGENDADOR"] = "1"
for k in ("PUBLIC_URL", "ADMIN_EMAIL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import rh  # noqa: E402
from app.db import DadosRH, Historico, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 18, 14, 0)}
banco.agora = lambda: relogio["agora"]
CPF, CPF2, PIS = "52998224725", "11144477735", "12056412545"


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def posta(cli, caminho, token, **dados):
    return cli.post(caminho, data={"csrf": token, **dados})


def pessoa(nome):
    with SessionLocal() as db:
        return db.scalar(select(Usuario).where(Usuario.nome == nome))


def dados(nome):
    with SessionLocal() as db:
        return db.scalar(select(DadosRH).where(DadosRH.usuario_id == pessoa(nome).id))


BASE = dict(nivel="funcionario", classificacao="clt", funcao_0="Garçom")


def main():
    # validações puras
    assert rh.cpf_valido(CPF) and rh.cpf_valido("529.982.247-25") and not rh.cpf_valido("52998224724")
    assert not rh.cpf_valido("11111111111") and rh.pis_valido(PIS) and not rh.pis_valido("12056412546")
    assert rh.fmt_cpf(CPF) == "529.982.247-25" and rh.fmt_pis(PIS) == "120.56412.54-5"

    g = novo()
    posta(g, "/cadastro", csrf(g, "/cadastro"), aceito="1", empresa="Diner", nome="Dora Dona",
          email="dora@diner.com", senha="segredo1")
    tg = csrf(g, "/painel")
    form = g.get("/painel/equipe/novo").text
    assert "Documentos e dados pessoais" in form and 'name="rh_cpf"' in form and 'name="rh_pix"' in form

    # ---- CPF inválido não salva nada
    r = posta(g, "/painel/equipe/novo", tg, nome="Ana", sobrenome="Souza", celular="3055550001", **BASE,
              rh_cpf="529.982.247-24")
    assert r.status_code == 400 and "CPF inválido" in r.text and 'value="529.982.247-24"' in r.text
    assert not pessoa("Ana Souza")

    # ---- cadastro completo
    r = posta(g, "/painel/equipe/novo", tg, nome="Ana", sobrenome="Souza", celular="3055550001", **BASE,
              rh_cpf="529.982.247-25", rh_pis=PIS, rh_rg_numero="123456 ", rh_rg_orgao="SSP", rh_rg_uf="MA",
              rh_ctps_numero="7654321", rh_ctps_serie="0012", rh_ctps_uf="MA", rh_sexo="F",
              rh_estado_civil="casado", rh_instrucao="07", rh_raca_cor="3", rh_nome_mae="Maria Souza",
              rh_cep="65000-000", rh_cidade="São Luís", rh_uf="MA", rh_banco="Caixa", rh_agencia="1234",
              rh_conta="5678-9", rh_tipo_conta="corrente", rh_pix=CPF, rh_emergencia_nome="João",
              rh_emergencia_telefone="98 99999-0000", rh_rg_emissao="2010-05-04", rh_nome_social="Aninha",
              rh_categoria_esocial="101", rh_tipo_contrato="experiencia", rh_fim_experiencia="2027-02-15",
              rh_insalubridade="20", rh_filhos="2", rh_camisa="M")
    assert r.status_code == 303, r.text[:400]
    d = dados("Ana Souza")
    assert d.cpf == CPF and d.pis == PIS and d.rg_numero == "123456" and d.cep == "65000000"
    assert d.banco == "Caixa" and d.conta == "5678-9" and d.raca_cor == "3" and d.rg_emissao == "2010-05-04"
    assert d.tipo_contrato == "experiencia" and d.insalubridade == "20" and d.filhos == "2" and d.nome_social == "Aninha"
    ficha = g.get(f"/painel/equipe/{pessoa('Ana Souza').id}/ficha").text
    assert 'value="529.982.247-25"' in ficha and 'value="65000-000"' in ficha and 'value="120.56412.54-5"' in ficha
    with SessionLocal() as db:
        h = db.scalar(select(Historico).where(Historico.acao == "documentos alterados"))
        assert h and "CPF" in h.detalhe and CPF not in h.detalhe and "1234" not in h.detalhe

    # ---- CPF repetido, opção inválida, CEP curto
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", sobrenome="Reis", celular="3055550002", **BASE, rh_cpf=CPF)
    assert r.status_code == 400 and "com este CPF" in r.text
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", sobrenome="Reis", celular="3055550002", **BASE, rh_uf="XX")
    assert r.status_code == 400 and "opção válida" in r.text
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", sobrenome="Reis", celular="3055550002", **BASE, rh_cep="650")
    assert r.status_code == 400 and "CEP inválido" in r.text
    r = posta(g, "/painel/equipe/novo", tg, nome="Bia", sobrenome="Reis", celular="3055550002", **BASE, rh_cpf=CPF2)
    assert r.status_code == 303 and dados("Bia Reis").cpf == CPF2

    # ---- gerente sem acesso à folha não vê nem apaga os dados bancários
    posta(g, "/painel/gerentes", tg, nome="Lia Lima", email="lia@diner.com", p_equipe="1")
    token = pessoa("Lia Lima").convite_token
    lc = novo()
    posta(lc, f"/convite/{token}", csrf(lc, f"/convite/{token}"), senha="senha123")
    lc = novo()
    posta(lc, "/entrar", csrf(lc, "/entrar"), login="lia@diner.com", senha="senha123")
    tl = csrf(lc, "/painel")
    ana_id = pessoa("Ana Souza").id
    ficha = lc.get(f"/painel/equipe/{ana_id}/ficha").text
    assert 'name="rh_cpf"' in ficha and 'name="rh_pix"' not in ficha and "5678-9" not in ficha
    r = posta(lc, f"/painel/equipe/{ana_id}/ficha", tl, nome="Ana", sobrenome="Souza", celular="3055550001",
              **BASE, rh_cpf=CPF, rh_pis=PIS, rh_bairro="Centro")
    assert r.status_code == 303
    d = dados("Ana Souza")
    assert d.bairro == "Centro" and d.banco == "Caixa" and d.conta == "5678-9"   # banco intacto
    assert d.rg_numero == ""                                                     # o que veio vazio, limpou
    print("OK: ficha de RH passou")


if __name__ == "__main__":
    main()
