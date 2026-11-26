"""Camada de regras por país (fusão Brasil + EUA, etapa 2). Rodar da raiz: python tests/test_paises.py"""
import os
import re
import sys
import tempfile
import types
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
for k in ("ANTHROPIC_API_KEY", "ADMIN_EMAIL", "ASAAS_API_KEY", "PUBLIC_URL"):
    os.environ.pop(k, None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import apuracao, calendario, paises  # noqa: E402
from app.db import Empresa, Plano, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.paises import br  # noqa: E402


def contrato():
    for pais in paises.PAISES:
        m = paises.regras_de(pais)
        faltando = [n for n in paises.CONTRATO if not hasattr(m, n)]
        assert not faltando, (pais, faltando)
        assert m.CODIGO == pais


def codigo():
    assert paises.codigo(None) == "BR" and paises.codigo("") == "BR" and paises.codigo("xx") == "BR"
    assert paises.codigo(" br ") == "BR"
    assert paises.codigo(types.SimpleNamespace(pais="BR")) == "BR"
    assert paises.codigo(types.SimpleNamespace(pais=None)) == "BR"
    assert paises.regras_de(Empresa(pais="BR")) is br
    assert paises.regras_de(None) is br


def brasil_igual_ao_antigo():
    """A versão brasileira só aponta para o código que já existia: o resultado é o mesmo."""
    for ano in (2025, 2026, 2027):
        assert br.feriados(ano) == apuracao.feriados_nacionais(ano)
        assert br.facultativos(ano) == calendario.facultativos(ano)
        assert br.comemorativas(ano) == calendario.comemorativas(ano)
    e = Empresa(pais="BR")
    d = date(2026, 11, 26)
    assert br.inicio_semana(e, d) == apuracao.inicio_semana(e, d)
    assert (br.MOEDA, br.IDIOMA, br.COBRANCA) == ("BRL", "pt", "asaas")


def cadastro_vira_brasil():
    cli = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="csrf" value="([^"]+)"', cli.get("/cadastro").text).group(1)
    cli.post("/cadastro", data={"csrf": tok, "aceito": "1", "empresa": "Bar do Teste", "nome": "Ana",
                                "email": "ana@teste.com", "senha": "segredo1"})
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Bar do Teste"))
        assert e is not None and e.pais == "BR"
        assert paises.regras_de(e) is br
        planos = db.scalars(select(Plano)).all()
        db.add(Plano(nome="Novo"))
        db.flush()
        planos = db.scalars(select(Plano)).all()
        assert all(p.pais == "BR" for p in planos)
        # o calendário da empresa continua com os feriados e facultativos do Brasil
        mapa = calendario.datas_do_ano(db, e, 2026)
        assert ("nacional", "Tiradentes") in mapa[date(2026, 4, 21)]
        assert any(t == "facultativo" for t, _n in mapa[date(2026, 2, 17)])  # Carnaval


def telas_usam_a_camada():
    """As telas pedem as regras ao país da empresa; só a camada chama os módulos de regra."""
    proibido = re.compile(r"apuracao\.(apurar|fechar|periodo_aberto|feriados_nacionais|inicio_semana)\(|"
                          r"from \.apuracao import [^\n]*feriados_nacionais")
    achados = []
    for p in sorted((RAIZ / "app").rglob("*.py")):
        if p.name == "apuracao.py" or "paises" in p.parts:
            continue
        for n, linha in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if proibido.search(linha):
                achados.append(f"{p.relative_to(RAIZ)}:{n}: {linha.strip()}")
    assert not achados, "\n".join(achados)


def main():
    contrato()
    codigo()
    brasil_igual_ao_antigo()
    cadastro_vira_brasil()
    telas_usam_a_camada()
    print("ok: camada de regras por país")


if __name__ == "__main__":
    main()
