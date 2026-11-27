"""Planilha modelo da equipe (CSV e Excel) com todos os campos da ficha, e a importação dela.
Rodar da raiz: python tests/test_planilha_modelo.py"""
import html
import io
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})
os.environ.pop("ADMIN_EMAIL", None)

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import DadosRH, Empresa, Local, SessionLocal, Setor, Usuario  # noqa: E402
from app.main import app  # noqa: E402


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    m = re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text)
    assert m, f"sem csrf em {caminho}"
    return m.group(1)


def cadastrar(cli, empresa, email):
    cli.post("/cadastro", data={"csrf": csrf(cli, "/cadastro"), "aceito": "1", "empresa": empresa,
                                "nome": "Gestor", "email": email, "senha": "segredo1"})
    with SessionLocal() as db:
        return db.scalar(select(Usuario).where(Usuario.email == email)).empresa_id


def pis_valido():
    base = [1, 2, 0, 5, 4, 3, 2, 1, 0, 9]
    resto = sum(d * p for d, p in zip(base, (3, 2, 9, 8, 7, 6, 5, 4, 3, 2))) % 11
    return "".join(map(str, base)) + str(0 if resto < 2 else 11 - resto)


def enviar(cli, nome, conteudo, tipo):
    r = cli.post("/painel/importar-equipe", data={"csrf": csrf(cli, "/painel/importar-equipe")},
                 files={"arquivo": (nome, conteudo, tipo)})
    return r, r.text


def confirmar(cli, texto):
    conteudo = html.unescape(re.search(r'<textarea name="conteudo" hidden>(.*?)</textarea>', texto, re.S).group(1))
    cli.post("/painel/importar-equipe/confirmar", data={"csrf": csrf(cli, "/painel/importar-equipe"),
                                                        "conteudo": conteudo})


def main():
    g = novo()
    eid = cadastrar(g, "Bistrô Sol", "gestor@bistrosol.com")
    with SessionLocal() as db:
        db.add_all([Local(empresa_id=eid, nome="Filial Centro", lat=-2.53, lng=-44.30, raio_m=150),
                    Setor(empresa_id=eid, nome="Cozinha")])
        db.commit()
        local_id = db.scalar(select(Local.id).where(Local.nome == "Filial Centro"))
        setor_id = db.scalar(select(Setor.id).where(Setor.nome == "Cozinha"))

    # ---------- modelos
    t = g.get("/painel/importar-equipe").text
    assert "modelo.xlsx" in t and "modelo.csv" in t and ".xlsx" in t
    c = g.get("/painel/importar-equipe/modelo.csv")
    assert c.headers["content-type"].startswith("text/csv")
    cab = c.text.lstrip("﻿").splitlines()[0].split(";")
    for x in ("Nome", "Sobrenome", "Celular", "Local", "Setor", "Tipo do salário", "Salário", "Vale-transporte",
              "Contrato", "CPF", "PIS/PASEP/NIS", "Prazo do contrato", "Insalubridade", "Chave PIX",
              "Contato de emergência (nome)", "Telefone de emergência"):
        assert x in cab, x
    assert len(cab) == len(set(cab)) == 70
    x = g.get("/painel/importar-equipe/modelo.xlsx")
    assert x.headers["content-type"].startswith("application/vnd.openxmlformats")
    wb = load_workbook(io.BytesIO(x.content))
    assert wb.sheetnames == ["Colaboradores", "Listas", "Instruções"]
    ws = wb["Colaboradores"]
    assert [c.value for c in ws[1]] == cab
    assert ws.data_validations.dataValidation, "listas de opções"
    assert "Filial Centro" in [c.value for c in wb["Listas"]["A"]]   # os locais da empresa viram lista
    assert ws.cell(row=2, column=cab.index("CPF") + 1).number_format == "@"

    # ---------- preenche o próprio modelo no Excel e importa
    col = {n: i + 1 for i, n in enumerate(cab)}
    for linha in (2, 3):
        for n in range(1, len(cab) + 1):
            ws.cell(row=linha, column=n).value = None

    def poe(linha, **v):
        for k, valor in v.items():
            ws.cell(row=linha, column=col[k]).value = valor
    poe(2, **{"Nome": "Ana", "Sobrenome": "Souza", "Celular": "98 99999-0301", "Local": "Filial Centro",
              "Setor": "cozinha", "Admissão": date(2025, 3, 15), "Função": "Cozinheira", "Tipo do salário": "mensal",
              "Salário": 2100, "Jornada": 220, "Dependentes": 2, "Vale-transporte": "180,00", "Gorjeta": "sim",
              "Contrato": "CLT", "CPF": "529.982.247-25", "PIS/PASEP/NIS": pis_valido(), "RG: UF": "MA",
              "RG: data de emissão": date(2015, 5, 10), "Sexo": "Feminino", "Estado civil": "Casado(a)",
              "Insalubridade": "20%", "Categoria eSocial": "101", "Prazo do contrato": "Contrato de experiência",
              "CEP": 65000000, "Cidade": "São Luís", "UF": "MA", "Banco (nome ou código)": "Itaú",
              "Tipo de conta": "Conta corrente", "Contato de emergência (nome)": "José Souza"})
    poe(3, **{"Nome": "Léo", "Sobrenome": "Lima", "Celular": "98 99999-0302", "Setor": "Bar",
              "CPF": "111.111.111-11", "Sexo": "Talvez"})
    saida = io.BytesIO()
    wb.save(saida)
    r, t = enviar(g, "equipe.xlsx", saida.getvalue(),
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.status_code == 200, t[:500]
    assert "Prévia: 1 pessoa(s) prontas, 1 com erro" in t, t[t.find("Prévia"):][:300]
    assert "setor não encontrado: Bar" in t and "Sexo" in t
    confirmar(g, t)
    with SessionLocal() as db:
        ana = db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza"))
        assert (ana.local_id, ana.setor_id, ana.cargo, ana.salario_centavos, ana.vt_centavos, ana.dependentes,
                ana.gorjeta, ana.classificacao, ana.data_admissao) == \
            (local_id, setor_id, "Cozinheira", 210000, 18000, 2, True, "clt", date(2025, 3, 15))
        d = db.scalar(select(DadosRH).where(DadosRH.usuario_id == ana.id))
        assert (d.cpf, d.pis, d.rg_uf, d.rg_emissao, d.sexo, d.estado_civil, d.insalubridade,
                d.categoria_esocial, d.tipo_contrato, d.cep, d.banco, d.tipo_conta, d.emergencia_nome) == \
            ("52998224725", pis_valido(), "MA", "2015-05-10", "F", "casado", "20", "101", "experiencia",
             "65000000", "Itaú", "corrente", "José Souza"), d.__dict__
        assert db.scalar(select(Usuario).where(Usuario.nome == "Léo Lima")) is None

    # mesmo CPF de novo: barrado; salário por hora pelo CSV vira salário de referência do mês
    csv_txt = ("Nome;Sobrenome;Celular;Função;Tipo do salário;Salário;Jornada;CPF\r\n"
               "Bia;Reis;98 99999-0303;Garçom;hora;8,50;120;\r\n"
               "Caio;Dias;98 99999-0304;;;;;529.982.247-25\r\n")
    r, t = enviar(g, "equipe.csv", csv_txt.encode("utf-8"), "text/csv")
    assert "Prévia: 1 pessoa(s) prontas, 1 com erro" in t and "CPF já cadastrado" in t
    confirmar(g, t)
    with SessionLocal() as db:
        bia = db.scalar(select(Usuario).where(Usuario.nome == "Bia Reis"))
        assert bia.salario_centavos == 850 * 120 and bia.jornada_mensal == 120

    # ---------- EUA: modelo em inglês e pagamento por hora ou anual
    u = novo()
    eid_us = cadastrar(u, "Sunny Diner", "manager@sunnydiner.com")
    with SessionLocal() as db:
        db.get(Empresa, eid_us).pais = "US"
        db.commit()
    c = u.get("/painel/importar-equipe/modelo.csv").text.lstrip("﻿")
    assert c.splitlines()[0].startswith("First name,Last name,Phone,Email,Location,Department")
    assert "CPF" not in c
    assert load_workbook(io.BytesIO(u.get("/painel/importar-equipe/modelo.xlsx").content)).sheetnames == \
        ["Team", "Lists", "Instructions"]
    csv_us = ("First name,Last name,Phone,Hire date,Role,Pay type,Pay rate,Tips,Classification\r\n"
              "Maria,Lopez,(305) 555-0123,03/15/2025,Manager,annual,52000,no,W-2\r\n"
              "James,Carter,(305) 555-0188,,Server,hourly,15.50,yes,1099\r\n")
    r, t = enviar(u, "team.csv", csv_us.encode(), "text/csv")
    assert "Prévia: 2 pessoa(s) prontas, 0 com erro" in t, t[t.find("Prévia"):][:300]
    confirmar(u, t)
    with SessionLocal() as db:
        maria = db.scalar(select(Usuario).where(Usuario.nome == "Maria Lopez"))
        james = db.scalar(select(Usuario).where(Usuario.nome == "James Carter"))
        assert maria.isento and maria.data_admissao == date(2025, 3, 15) and maria.classificacao == "w2"
        assert (james.taxa_hora_centavos, james.gorjeta, james.classificacao) == (1550, True, "1099_ind")
    print("ok: planilha modelo da equipe")


if __name__ == "__main__":
    main()
