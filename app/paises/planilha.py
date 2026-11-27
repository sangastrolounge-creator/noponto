"""Planilha modelo do cadastro de colaboradores (CSV e Excel), com todos os campos da ficha.

Brasil: contato, trabalho, função e salário, contrato e os dados de RH do eSocial (documentos,
dados pessoais, contrato e adicionais, endereço, banco, emergência). EUA: os campos da ficha de lá.
O importador (app/importar.py) lê estas mesmas colunas, pelo nome do cabeçalho.
"""
import csv
import io
import re
import unicodedata

from .. import rh


def norm(txt: str) -> str:
    """Cabeçalho ou valor comparável: sem acento, minúsculo, sem pontuação e espaços extras."""
    t = unicodedata.normalize("NFKD", str(txt or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9%]+", " ", t).split())


# ------------------------------------------------------------------ dados de RH (Brasil)
CABECALHO_RH = {"tipo_contrato": "Prazo do contrato"}   # "Tipo de contrato" já é o CLT/Aprendiz/PJ...


def cabecalho_rh(campo: str, rotulo: str) -> str:
    return CABECALHO_RH.get(campo, rotulo)


def mapa_rh() -> dict[str, str]:
    """Cabeçalho normalizado -> campo de RH (pelo rótulo da ficha, pelo nome do campo e pelo do modelo)."""
    m = {}
    for campo, rotulo, _tipo, _extra, _grupo in rh.CAMPOS:
        for nome in (rotulo, campo, campo.replace("_", " "), cabecalho_rh(campo, rotulo)):
            m.setdefault(norm(nome), campo)
    return m


def _opcoes(extra) -> dict[str, str]:
    """Valor escrito -> valor guardado, aceitando o código e o nome da opção."""
    pares = extra.items() if isinstance(extra, dict) else ((x, x) for x in extra)
    m = {}
    for chave, nome in pares:
        for t in (chave, nome, re.split(r"\s+-\s+|\s*\(", str(nome))[0]):
            m.setdefault(norm(t), chave)
    return m


def valor_rh(campo: str, tipo: str, extra, txt: str) -> str | None:
    """Converte o texto da planilha para o formato da ficha. None = valor não reconhecido."""
    txt = " ".join(str(txt or "").split())
    if not txt:
        return ""
    if tipo in ("cpf", "pis", "cep"):
        d = re.sub(r"\D", "", txt)
        tamanho = 8 if tipo == "cep" else 11
        if d and len(d) < tamanho and len(d) >= tamanho - 3:   # o Excel tira os zeros da frente
            d = d.zfill(tamanho)
        return d
    if tipo in ("uf", "opcao"):
        m, k = _opcoes(extra), norm(txt)
        return m.get(k) or m.get(k.replace("%", "").strip())
    return txt


def opcoes_rh(extra) -> list[str]:
    return list(extra.values()) if isinstance(extra, dict) else list(extra)


# ------------------------------------------------------------------ colunas do modelo
def colunas(pais: str, locais: list[str] | None = None, setores: list[str] | None = None) -> list[dict]:
    """Cada coluna: cab (cabeçalho), ex (dois exemplos), ajuda, opcoes (lista ou None), texto (célula de texto)."""
    locais, setores = locais or [], setores or []

    def c(cab, ex1="", ex2="", ajuda="", opcoes=None, texto=False, grupo="Cadastro"):
        return {"cab": cab, "ex": (ex1, ex2), "ajuda": ajuda, "opcoes": opcoes, "texto": texto, "grupo": grupo}

    if pais == "US":
        return [
            c("First name", "Maria", "James", "Required."),
            c("Last name", "Lopez", "Carter"),
            c("Phone", "(305) 555-0123", "(305) 555-0188", "Required. With area code: the employee signs in with it.",
              texto=True),
            c("Email", "maria@example.com", "", "Optional."),
            c("Location", "", "", "Name of a clock-in location already set up. Blank = main location.",
              locais or None),
            c("Department", "", "", "Name of a department already set up.", setores or None),
            c("Hire date", "2025-03-15", "", "YYYY-MM-DD."),
            c("Birth date", "", "2008-06-01", "YYYY-MM-DD."),
            c("Role", "Server", "Line cook", "Main role."),
            c("Pay type", "hourly", "annual", "hourly or annual (annual = salaried, exempt from overtime).",
              ["hourly", "annual"]),
            c("Pay rate", "15.50", "52000", "Dollars: per hour, or per year if annual."),
            c("Tips", "yes", "no", "Receives tips?", ["yes", "no"]),
            c("Classification", "W-2", "W-2", "W-2, 1099 or 1099 business.", ["W-2", "1099", "1099 business"]),
        ]

    base = [
        c("Nome", "Ana", "Léo", "Obrigatório. Primeiro nome (ou o nome completo)."),
        c("Sobrenome", "Souza", "Lima"),
        c("Celular", "98 99999-0101", "98 99999-0102", "Obrigatório. Com DDD: o funcionário entra no app por ele.",
          texto=True),
        c("E-mail", "ana@exemplo.com.br", "", "Opcional."),
        c("Local", "", "", "Nome de um local de ponto já cadastrado em Locais. Vazio = endereço principal.",
          locais or None),
        c("Setor", "", "", "Nome de um setor já cadastrado em Equipe -> Setores.", setores or None),
        c("Admissão", "15/03/2025", "", "DD/MM/AAAA."),
        c("Vigência do ponto", "", "", "DD/MM/AAAA. A partir de quando o ponto vale. Vazio = desde sempre."),
        c("Nascimento", "", "01/06/2009", "DD/MM/AAAA."),
        c("Função", "Garçom", "Auxiliar de cozinha", "Função principal."),
        c("Tipo do salário", "mensal", "hora", "mensal ou hora.", ["mensal", "hora"]),
        c("Salário", "1.750,00", "8,50", "Em reais: por mês ou por hora, conforme o tipo."),
        c("Jornada", "220", "120", "Horas por mês: 220 (44 h por semana), 180 (36 h), 150 (30 h)."),
        c("Dependentes", "1", "0", "Dependentes para o IRRF."),
        c("Vale-transporte", "220,00", "", "Custo das passagens no mês, em reais."),
        c("Gorjeta", "sim", "não", "Recebe gorjeta?", ["sim", "não"]),
        c("Contrato", "CLT", "Aprendiz", "CLT, Aprendiz, Intermitente, Estágio ou PJ.",
          ["CLT", "Aprendiz", "Intermitente", "Estágio", "PJ"]),
    ]
    exemplos = {"sexo": ("Feminino", "Masculino"), "estado_civil": ("Solteiro(a)", "Solteiro(a)"),
                "nacionalidade": ("Brasileira", "Brasileira"), "naturalidade": ("São Luís", "São Luís"),
                "naturalidade_uf": ("MA", "MA"), "categoria_esocial": ("101 - Empregado geral (CLT)", "103 - Aprendiz"),
                "tipo_contrato": ("Contrato de experiência", "Prazo determinado"), "cidade": ("São Luís", "São Luís"),
                "uf": ("MA", "MA"), "camisa": ("M", "P")}
    formatos = {"data": "DD/MM/AAAA.", "cpf": "Só os números ou 000.000.000-00.", "pis": "11 números.",
                "cep": "8 números."}
    for campo, rotulo, tipo, extra, grupo in rh.CAMPOS:
        opcoes = opcoes_rh(extra) if tipo in ("uf", "opcao") else None
        ajuda = formatos.get(tipo, "Uma das opções da lista." if opcoes else "")
        if grupo in rh.SO_PAGAMENTO:
            ajuda = (ajuda + " Só entra se quem importa vê a folha de pagamento.").strip()
        ex = exemplos.get(campo, ("", ""))
        base.append(c(cabecalho_rh(campo, rotulo), ex[0], ex[1], ajuda, opcoes,
                      texto=tipo in ("cpf", "pis", "cep", "texto"), grupo=rh.GRUPOS[grupo]))
    return base


# ------------------------------------------------------------------ arquivos
def gerar_csv(pais: str, locais=None, setores=None) -> str:
    cols = colunas(pais, locais, setores)
    sep = "," if pais == "US" else ";"
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=sep, lineterminator="\r\n")
    w.writerow([x["cab"] for x in cols])
    for i in (0, 1):
        w.writerow([x["ex"][i] for x in cols])
    return "﻿" + saida.getvalue()


def gerar_xlsx(pais: str, locais=None, setores=None) -> bytes:
    """Excel com a aba da equipe (cabeçalho fixo, exemplos, listas de opções) e a aba de instruções."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    eua = pais == "US"
    cols = colunas(pais, locais, setores)
    wb = Workbook()
    ws = wb.active
    ws.title = "Team" if eua else "Colaboradores"
    listas = wb.create_sheet("Lists" if eua else "Listas")
    listas.sheet_state = "hidden"
    instr = wb.create_sheet("Instructions" if eua else "Instruções")

    cab_fonte, cab_fundo = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E79")
    obrig_fundo = PatternFill("solid", fgColor="C00000")
    ex_fonte = Font(italic=True, color="7F7F7F")
    ws.append([x["cab"] for x in cols])
    for i in (0, 1):
        ws.append([x["ex"][i] for x in cols])
    ws.freeze_panes = "B2"
    ws.row_dimensions[1].height = 32
    col_lista = 0
    for n, x in enumerate(cols, start=1):
        letra = get_column_letter(n)
        cel = ws.cell(row=1, column=n)
        cel.font = cab_fonte
        cel.fill = obrig_fundo if x["ajuda"].startswith(("Obrigatório", "Required")) else cab_fundo
        cel.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[letra].width = max(12, min(34, len(x["cab"]) + 4))
        for linha in (2, 3):
            ws.cell(row=linha, column=n).font = ex_fonte
        if x["texto"]:   # CPF, PIS, CEP e celular como texto: o Excel não tira os zeros da frente
            for linha in range(2, 1003):
                ws.cell(row=linha, column=n).number_format = "@"
        if x["opcoes"]:
            col_lista += 1
            lc = get_column_letter(col_lista)
            for k, op in enumerate(x["opcoes"], start=1):
                listas.cell(row=k, column=col_lista, value=op)
            dv = DataValidation(type="list", formula1=f"='{listas.title}'!${lc}$1:${lc}${len(x['opcoes'])}",
                                allow_blank=True, showErrorMessage=False)
            ws.add_data_validation(dv)
            dv.add(f"{letra}2:{letra}1002")

    instr.append(["Column", "Section", "How to fill in", "Options"] if eua
                 else ["Coluna", "Grupo", "Como preencher", "Opções"])
    for c in instr[1]:
        c.font, c.fill = cab_fonte, cab_fundo
    for x in cols:
        instr.append([x["cab"], x["grupo"], x["ajuda"], ", ".join(x["opcoes"] or [])[:1000]])
    for letra, larg in (("A", 34), ("B", 22), ("C", 70), ("D", 70)):
        instr.column_dimensions[letra].width = larg
    for linha in instr.iter_rows(min_row=2):
        for c in linha:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    nota = ("One person per row. Replace or delete the two example rows. Only first name and phone are required."
            if eua else "Uma pessoa por linha. Troque ou apague as duas linhas de exemplo. Só o nome e o celular "
            "são obrigatórios; as colunas em vermelho são as obrigatórias.")
    instr.insert_rows(1)
    instr["A1"] = nota
    instr["A1"].font = Font(bold=True)
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def xlsx_para_csv(bruto: bytes, max_linhas: int) -> str:
    """Primeira aba do Excel enviado -> texto CSV com ponto e vírgula (datas AAAA-MM-DD, sem ambiguidade)."""
    from datetime import date, datetime

    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(bruto), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\r\n")
    for n, linha in enumerate(ws.iter_rows(values_only=True)):
        if n > max_linhas + 1:
            break
        valores = []
        for v in linha:
            if v is None:
                valores.append("")
            elif isinstance(v, (datetime, date)):
                valores.append(v.strftime("%Y-%m-%d"))
            elif isinstance(v, bool):
                valores.append("sim" if v else "não")
            elif isinstance(v, float):
                valores.append(str(int(v)) if v.is_integer() else str(round(v, 2)))
            else:
                valores.append(str(v).strip())
        while valores and not valores[-1]:
            valores.pop()
        w.writerow(valores)
    wb.close()
    return saida.getvalue()
