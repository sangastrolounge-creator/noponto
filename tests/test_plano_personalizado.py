"""Plano personalizado: o master combina limite de funcionários, de unidades e valor com uma só empresa.
Rodar da raiz: python tests/test_plano_personalizado.py"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core import limite_funcionarios, limite_locais  # noqa: E402
from app.db import Empresa, Plano, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def cadastrar(cli, empresa, email):
    cli.post("/cadastro", data={"csrf": csrf(cli, "/cadastro"), "aceito": "1", "empresa": empresa,
                                "nome": "Gestor " + empresa, "email": email, "senha": "segredo1"})
    with SessionLocal() as db:
        return db.scalar(select(Usuario).where(Usuario.email == email)).empresa_id


def main():
    m = novo()
    cadastrar(m, "Plataforma", "dono@plataforma.com")
    with SessionLocal() as db:
        db.add(Plano(nome="Essencial", preco_centavos=5990, limite_funcionarios=10, limite_locais=2, ativo=True,
                     com_gps=True, com_foto=False, com_facial=False, com_cerca=True, com_alertas=True,
                     com_pagamento=False, descricao="Até 10 funcionários"))
        db.commit()
        essencial = db.scalar(select(Plano.id).where(Plano.nome == "Essencial"))

    cli = novo()
    eid = cadastrar(cli, "Rede Sabor", "gestor@redesabor.com")
    outra = cadastrar(novo(), "Padaria Lua", "gestor@padarialua.com")

    t = m.get(f"/master/empresas/{eid}").text
    assert "Plano personalizado" in t and "Recursos iguais aos do plano" in t
    url = f"/master/empresas/{eid}/plano-personalizado"

    # valor inválido não cria nada
    m.post(url, data={"csrf": csrf(m, f"/master/empresas/{eid}"), "funcionarios": "25", "locais": "8",
                      "valor": "abc", "base": str(essencial)})
    with SessionLocal() as db:
        assert db.scalar(select(Plano).where(Plano.empresa_id == eid)) is None

    # 25 funcionários, 8 unidades, R$ 349,90, recursos do Essencial
    r = m.post(url, data={"csrf": csrf(m, f"/master/empresas/{eid}"), "funcionarios": "25", "locais": "8",
                          "valor": "349,90", "base": str(essencial)})
    assert r.status_code == 303
    with SessionLocal() as db:
        e = db.get(Empresa, eid)
        p = e.plano
        assert p.empresa_id == eid and not p.ativo and p.nome == "Personalizado"
        assert (p.limite_funcionarios, p.limite_locais, p.preco_centavos) == (25, 8, 34990)
        assert (p.com_gps, p.com_foto, p.com_facial, p.com_pagamento) == (True, False, False, False)
        assert limite_funcionarios(e) == 25 and limite_locais(e) == 8
        pid = p.id

    # ajustar de novo muda o mesmo plano (não cria outro) e mantém os recursos
    m.post(url, data={"csrf": csrf(m, f"/master/empresas/{eid}"), "funcionarios": "30", "locais": "2",
                      "valor": "399,00", "base": "0"})
    with SessionLocal() as db:
        lista = list(db.scalars(select(Plano).where(Plano.empresa_id == eid)))
        assert len(lista) == 1 and lista[0].id == pid
        assert (lista[0].limite_funcionarios, lista[0].limite_locais, lista[0].preco_centavos) == (30, 2, 39900)
        assert lista[0].com_gps and not lista[0].com_foto

    # o limite de unidades vale: com 2, cabe só mais um local além do principal
    tk = csrf(cli, "/painel/locais")
    cli.post("/painel/locais", data={"csrf": tk, "nome": "Filial Centro", "lat": "-2.53", "lng": "-44.30",
                                     "raio_m": "150"})
    cli.post("/painel/locais", data={"csrf": tk, "nome": "Filial Praia", "lat": "-2.49", "lng": "-44.25",
                                     "raio_m": "150"})
    t = cli.get("/painel/locais").text
    assert "Filial Centro" in t and "Filial Praia" not in t

    # a empresa vê o plano dela para pagar; ninguém mais vê
    from app.cobranca import _planos
    with SessionLocal() as db:
        assert pid in [x.id for x in _planos(db, db.get(Empresa, eid))]
        assert pid not in [x.id for x in _planos(db, db.get(Empresa, outra))]
        assert pid not in [x.id for x in _planos(db)]
    assert cli.get("/painel/assinatura").status_code == 200
    assert "Personalizado" not in m.get("/master/planos").text
    assert "399,00" not in novo().get("/").text
    assert "Personalizado" not in m.get(f"/master/empresas/{outra}").text

    # o plano de uma empresa não pode ser posto em outra
    m.post(f"/master/empresas/{outra}/dados", data={"csrf": csrf(m, f"/master/empresas/{outra}"),
                                                     "plano": str(pid), "obs": ""})
    with SessionLocal() as db:
        assert db.get(Empresa, outra).plano_id is None

    # voltar a empresa para um plano normal continua possível; o personalizado fica guardado
    m.post(f"/master/empresas/{eid}/dados", data={"csrf": csrf(m, f"/master/empresas/{eid}"),
                                                   "plano": str(essencial), "obs": ""})
    with SessionLocal() as db:
        assert db.get(Empresa, eid).plano_id == essencial and db.get(Plano, pid) is not None
    assert "Salvo, mas a empresa está em outro plano" in m.get(f"/master/empresas/{eid}").text
    print("ok: plano personalizado")


if __name__ == "__main__":
    main()
