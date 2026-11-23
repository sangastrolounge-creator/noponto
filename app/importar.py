"""Importar a equipe de uma planilha (CSV): o gestor envia o arquivo, confere a prévia (o que vai
entrar e o que tem erro) e confirma. Aceita os nomes de coluna em português (ou inglês), separador
ponto e vírgula ou vírgula, e datas DD/MM/AAAA ou AAAA-MM-DD. Respeita o limite do plano."""
import csv
import io
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import regras
from .core import (avisar, celular_limpo, celular_valido, email_valido, exigir_csrf, funcionarios_ativos,
                   gestor_logado, ir, ler_centavos, limite_funcionarios, render)
from .db import FuncaoUsuario, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MAX_LINHAS = 500
MAX_BYTES = 1_000_000
COLUNAS = {
    "nome": ("name", "full name", "employee", "employee name", "nome", "nome completo", "funcionário", "funcionario", "nombre"),
    "primeiro": ("first name", "first", "primeiro nome", "nombre de pila"),
    "sobrenome": ("last name", "last", "surname", "sobrenome", "apellido"),
    "celular": ("phone", "mobile", "cell", "cell phone", "phone number", "mobile phone", "celular", "telefone", "teléfono", "telefono"),
    "email": ("email", "e-mail", "email address", "correo"),
    "cargo": ("role", "job title", "title", "position", "job", "cargo", "função", "funcao", "puesto"),
    "valor": ("salário", "salario", "salário mensal", "salario mensal", "salário base", "salario base", "remuneração",
              "valor", "salary", "wage", "sueldo"),
    "valor_hora": ("valor da hora", "salário por hora", "salario por hora", "hourly rate", "pay rate"),
    "jornada": ("jornada", "jornada mensal", "horas por mês", "horas por mes", "carga horária", "carga horaria"),
    "dependentes": ("dependentes", "dependentes irrf"),
    "cpf": ("cpf",),
    "gorjeta": ("tips", "tipped", "receives tips", "gorjeta", "recebe gorjeta", "propinas"),
    "inicio": ("admissão", "admissao", "data de admissão", "data de admissao", "data de início", "data de inicio",
               "hire date", "start date", "fecha de ingreso"),
    "vigencia": ("vigência", "vigencia", "vigência do ponto", "vigencia do ponto", "início do ponto", "inicio do ponto",
                 "ponto a partir de"),
    "nascimento": ("birth date", "date of birth", "dob", "birthday", "data de nascimento", "nascimento", "fecha de nacimiento"),
    "classificacao": ("contrato", "tipo de contrato", "vínculo", "vinculo", "classificação", "classificacao", "tipo",
                      "classification", "employment type"),
}
SIM = {"y", "yes", "sim", "s", "si", "sí", "true", "1", "x"}
MODELO = ("Nome;Celular;E-mail;Função;Salário;Jornada;Dependentes;Gorjeta;Admissão;Nascimento;Contrato\r\n"
          "Ana Souza;98 99999-0101;ana@exemplo.com.br;Garçom;1.750,00;220;1;sim;15/03/2025;;CLT\r\n"
          "Léo Lima;98 99999-0102;;Cozinha;2.100,00;220;0;não;;01/06/2009;Aprendiz\r\n")
CONTRATOS = {"clt": "clt", "celetista": "clt", "empregado": "clt", "aprendiz": "aprendiz",
             "jovemaprendiz": "aprendiz", "intermitente": "intermitente", "estagio": "estagio",
             "estágio": "estagio", "estagiario": "estagio", "estagiário": "estagio", "pj": "pj",
             "mei": "pj", "prestador": "pj", "autonomo": "pj", "autônomo": "pj"}


def _data(s: str) -> date | None:
    s = s.strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(s)


def ler(conteudo: str, db: Session, empresa_id: int) -> tuple[list[dict], str | None]:
    """Linhas lidas, cada uma com 'dados' e 'erros'. Devolve também um erro geral (ou None)."""
    conteudo = conteudo.lstrip("﻿")
    if not conteudo.strip():
        return [], "O arquivo está vazio."
    try:
        dialeto = csv.Sniffer().sniff(conteudo.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialeto = csv.excel
    leitor = csv.reader(io.StringIO(conteudo), dialeto)
    try:
        cab = [c.strip().lower() for c in next(leitor)]
    except StopIteration:
        return [], "O arquivo está vazio."
    mapa = {}
    for chave, nomes in COLUNAS.items():
        for i, c in enumerate(cab):
            if c in nomes and chave not in mapa:
                mapa[chave] = i
    if "nome" not in mapa and "primeiro" not in mapa:
        return [], "Não achei a coluna do nome. Use o modelo: Nome, Celular, Função, Salário..."
    if "celular" not in mapa:
        return [], "Não achei a coluna do celular. O funcionário entra no app pelo celular."
    existentes = set(db.scalars(select(Usuario.celular).where(Usuario.celular.is_not(None)).execution_options(alcance_livre=True)))
    emails = {x for x in db.scalars(select(Usuario.email).where(Usuario.email.is_not(None)))}
    vistos, vistos_email, linhas = set(), set(), []
    for n, row in enumerate(leitor, start=2):
        if not any(c.strip() for c in row):
            continue
        if len(linhas) >= MAX_LINHAS:
            return linhas, f"Só dá para importar {MAX_LINHAS} pessoas por vez."

        def col(k):
            i = mapa.get(k)
            return row[i].strip() if i is not None and i < len(row) else ""
        nome = col("nome") or " ".join(x for x in (col("primeiro"), col("sobrenome")) if x)
        cel = celular_limpo(col("celular"))
        email = col("email").lower()
        erros = []
        if not nome:
            erros.append("sem nome")
        if not celular_valido(cel):
            erros.append("celular inválido")
        elif cel in existentes:
            erros.append("celular já cadastrado")
        elif cel in vistos:
            erros.append("celular repetido na planilha")
        if email and not email_valido(email):
            erros.append("e-mail inválido")
        elif email and (email in emails or email in vistos_email):
            email = ""   # e-mail já usado: importa sem ele
        valor, tipo_valor = 0, "mensal"
        if col("valor"):
            v = ler_centavos(col("valor"))
            if v is None or v > 10_000_000:
                erros.append("salário inválido")
            else:
                valor = v
        elif col("valor_hora"):
            v = ler_centavos(col("valor_hora"))
            if v is None or v > 100_000:
                erros.append("valor da hora inválido")
            else:
                valor, tipo_valor = v, "hora"
        jornada = 220
        if col("jornada"):
            j = col("jornada").lower().replace("h", "").strip()
            if j.isdigit() and 1 <= int(j) <= 300:
                jornada = int(j)
            else:
                erros.append("jornada inválida (use as horas do mês, ex.: 220)")
        dep = col("dependentes")
        dependentes = int(dep) if dep.isdigit() and int(dep) <= 30 else 0
        if dep and not dep.isdigit():
            erros.append("dependentes inválido")
        datas = {}
        for k in ("inicio", "nascimento", "vigencia"):
            if col(k):
                try:
                    datas[k] = _data(col(k))
                except ValueError:
                    erros.append({"inicio": "data de admissão inválida", "vigencia": "data de vigência inválida"}.get(
                        k, "data de nascimento inválida"))
        classe = col("classificacao").lower().replace(" ", "").replace("-", "")
        if classe and classe not in CONTRATOS:
            erros.append("contrato não reconhecido (use CLT, Aprendiz, Intermitente, Estágio ou PJ)")
        classe = CONTRATOS.get(classe, "clt")
        vistos.add(cel)
        if email:
            vistos_email.add(email)
        linhas.append({"linha": n, "erros": erros, "dados": {
            "nome": nome[:120], "celular": cel, "email": email, "cargo": col("cargo")[:80], "valor": valor,
            "tipo_valor": tipo_valor, "jornada": jornada, "dependentes": dependentes,
            "gorjeta": col("gorjeta").lower() in SIM, "inicio": datas.get("inicio"), "vigencia": datas.get("vigencia"),
            "nascimento": datas.get("nascimento"), "classificacao": classe}})
    if not linhas:
        return [], "Nenhuma pessoa encontrada na planilha."
    return linhas, None


# ------------------------------------------------------------------ telas
@rota.get("/painel/importar-equipe")
def tela(request: Request, u: Usuario = Depends(gestor_logado)):
    return render(request, "painel_importar.html", u=u, e=u.empresa, aba="equipe", linhas=None, conteudo="", erro=None)


@rota.get("/painel/importar-equipe/modelo.csv")
def modelo(u: Usuario = Depends(gestor_logado)):
    return Response("﻿" + MODELO, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modelo-equipe.csv"'})


@rota.post("/painel/importar-equipe", dependencies=CSRF)
async def previa(request: Request, arquivo: UploadFile = File(None), u: Usuario = Depends(gestor_logado),
                 db: Session = Depends(get_db)):
    e = u.empresa
    erro, linhas, conteudo = None, None, ""
    if not arquivo or not arquivo.filename:
        erro = "Escolha o arquivo da planilha (CSV)."
    else:
        bruto = await arquivo.read(MAX_BYTES + 1)
        if len(bruto) > MAX_BYTES:
            erro = "O arquivo passa de 1 MB."
        else:
            for cod in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    conteudo = bruto.decode(cod)
                    break
                except UnicodeDecodeError:
                    continue
            linhas, erro = ler(conteudo, db, e.id)
    lim = limite_funcionarios(e)
    vagas = max(0, lim - funcionarios_ativos(db, e.id)) if lim else None
    return render(request, "painel_importar.html", 400 if erro and not linhas else 200, u=u, e=e, aba="equipe",
                  linhas=linhas, conteudo=conteudo, erro=erro, vagas=vagas)


@rota.post("/painel/importar-equipe/confirmar", dependencies=CSRF)
def confirmar(request: Request, conteudo: str = Form(""), u: Usuario = Depends(gestor_logado),
              db: Session = Depends(get_db)):
    e = u.empresa
    linhas, erro = ler(conteudo[:MAX_BYTES], db, e.id)
    boas = [x["dados"] for x in linhas if not x["erros"]]
    if erro and not boas:
        avisar(request, erro, "erro")
        return ir("/painel/importar-equipe")
    lim = limite_funcionarios(e)
    if lim:
        boas = boas[:max(0, lim - funcionarios_ativos(db, e.id))]
    for d in boas:
        p = Usuario(empresa_id=e.id, nome=d["nome"], celular=d["celular"], email=d["email"] or None,
                    cargo=d["cargo"], papel="funcionario", convite_token=secrets.token_urlsafe(18),
                    salario_centavos=d["valor"] * (d["jornada"] if d["tipo_valor"] == "hora" else 1),
                    jornada_mensal=d["jornada"], dependentes=d["dependentes"],
                    gorjeta=d["gorjeta"], data_admissao=d["inicio"], ponto_desde=d.get("vigencia"),
                    nascimento=d["nascimento"], classificacao=d["classificacao"])
        db.add(p)
        db.flush()
        if d["cargo"] or d["valor"]:
            db.add(FuncaoUsuario(empresa_id=e.id, usuario_id=p.id, nome=d["cargo"], valor_centavos=d["valor"],
                                 tipo=d["tipo_valor"], ordem=0))
    regras.registrar(db, e.id, u.nome, "equipe importada", f"{len(boas)} pessoa(s) importada(s) de planilha.")
    db.commit()
    pulados = len(linhas) - len(boas)
    avisar(request, f"{len(boas)} pessoa(s) importada(s). {pulados} linha(s) ficaram de fora. Envie o link de acesso para cada pessoa."
           if pulados else f"{len(boas)} pessoa(s) importada(s). Envie o link de acesso para cada pessoa.")
    return ir("/painel/equipe")
