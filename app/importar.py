"""Importar a equipe de uma planilha (CSV ou Excel): o gestor envia o arquivo, confere a prévia (o que vai
entrar e o que tem erro) e confirma. Aceita os nomes de coluna em português (ou inglês), separador
ponto e vírgula ou vírgula, e datas DD/MM/AAAA ou AAAA-MM-DD. Respeita o limite do plano.
Lê todos os campos da ficha: contato, local, setor, função e salário, contrato e, no Brasil, os dados de
RH do eSocial (paises/planilha.py tem o modelo, em CSV e em Excel)."""
import csv
import io
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import regras, rh
from .core import (avisar, celular_limpo, celular_valido, email_valido, exigir_csrf, funcionarios_ativos,
                   gestor_logado, ir, ler_centavos, limite_funcionarios, pode, recurso_liberado, render)
from .db import DadosRH, Empresa, Local, Setor, Usuario, get_db
from .paises import planilha

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
    "dependentes": ("dependentes", "dependentes irrf", "dependentes (irrf)"),
    "tipo_valor": ("tipo do salário", "tipo do salario", "tipo de salário", "tipo de salario", "salário por",
                   "pay type", "tipo de pago"),
    "vt": ("vale-transporte", "vale transporte", "vt", "vale-transporte no mês", "vale-transporte no mês (r$)"),
    "local": ("local", "local de ponto", "unidade", "location", "clock-in location", "ubicación", "ubicacion"),
    "setor": ("setor", "departamento", "department", "sector"),
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
CONTRATOS_US = {"w2": "w2", "employee": "w2", "empregado": "w2", "1099": "1099_ind", "1099ind": "1099_ind",
                "1099individual": "1099_ind", "contractor": "1099_ind", "1099emp": "1099_emp",
                "1099business": "1099_emp", "1099company": "1099_emp", "1099empresa": "1099_emp"}
TIPOS_VALOR = {"mensal": "mensal", "mes": "mensal", "mês": "mensal", "por mes": "mensal", "por mês": "mensal",
               "monthly": "mensal", "hora": "hora", "por hora": "hora", "hourly": "hora", "hour": "hora",
               "anual": "anual", "annual": "anual", "salary": "anual", "salaried": "anual", "yearly": "anual"}
CONTRATOS = {"clt": "clt", "celetista": "clt", "empregado": "clt", "aprendiz": "aprendiz",
             "jovemaprendiz": "aprendiz", "intermitente": "intermitente", "estagio": "estagio",
             "estágio": "estagio", "estagiario": "estagio", "estagiário": "estagio", "pj": "pj",
             "mei": "pj", "prestador": "pj", "autonomo": "pj", "autônomo": "pj"}


def _data(s: str, eua: bool = False) -> date | None:
    s = s.strip()
    formatos = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y") if eua else \
        ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y")
    for f in formatos:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(s)


def _nomes(db: Session, modelo, empresa_id: int) -> dict[str, int]:
    q = select(modelo).where(modelo.empresa_id == empresa_id)
    if modelo is Local:
        q = q.where(Local.ativo.is_(True))
    return {planilha.norm(x.nome): x.id for x in db.scalars(q)}


def ler(conteudo: str, db: Session, empresa_id: int, u: Usuario | None = None) -> tuple[list[dict], str | None]:
    """Linhas lidas, cada uma com 'dados' e 'erros'. Devolve também um erro geral (ou None).
    `u` é quem importa: decide se os dados bancários entram (só quem vê a folha de pagamento)."""
    e = db.get(Empresa, empresa_id)
    eua = getattr(e, "pais", "BR") == "US"
    conteudo = conteudo.lstrip("\ufeff")
    if not conteudo.strip():
        return [], "O arquivo está vazio."
    primeira = conteudo.splitlines()[0]
    sep = max(";\t,", key=primeira.count)   # o separador que mais aparece no cabeçalho
    leitor = csv.reader(io.StringIO(conteudo), delimiter=sep)
    try:
        cab = [c.strip().lower() for c in next(leitor)]
    except StopIteration:
        return [], "O arquivo está vazio."
    mapa = {}
    for chave, nomes in COLUNAS.items():
        for i, c in enumerate(cab):
            if c in nomes and chave not in mapa and i not in mapa.values():
                mapa[chave] = i
    # dados de RH (Brasil): as colunas que sobraram, pelo rótulo da ficha
    rh_ok = bool(e) and not eua and recurso_liberado(e, "com_rh")
    ve_banco = bool(u) and pode(u, "pagamento")
    mapa_rh, usados = {}, set(mapa.values())
    if not eua:
        nomes_rh = planilha.mapa_rh()
        for i, c in enumerate(cab):
            campo = nomes_rh.get(planilha.norm(c))
            if i not in usados and campo and campo not in mapa_rh:
                mapa_rh[campo] = i
    defs_rh = {c[0]: c for c in rh.CAMPOS}
    if "nome" not in mapa and "primeiro" not in mapa:
        return [], "Não achei a coluna do nome. Use o modelo: Nome, Celular, Função, Salário..."
    if "celular" not in mapa:
        return [], "Não achei a coluna do celular. O funcionário entra no app pelo celular."
    existentes = set(db.scalars(select(Usuario.celular).where(Usuario.celular.is_not(None)).execution_options(alcance_livre=True)))
    emails = {x for x in db.scalars(select(Usuario.email).where(Usuario.email.is_not(None)))}
    cpfs = set(db.scalars(select(DadosRH.cpf).where(DadosRH.empresa_id == empresa_id, DadosRH.cpf != "")
                          .execution_options(alcance_livre=True))) if mapa_rh else set()
    locais, setores = _nomes(db, Local, empresa_id), _nomes(db, Setor, empresa_id)
    vistos, vistos_email, vistos_cpf, linhas = set(), set(), set(), []
    for n, row in enumerate(leitor, start=2):
        if not any(c.strip() for c in row):
            continue
        if len(linhas) >= MAX_LINHAS:
            return linhas, f"Só dá para importar {MAX_LINHAS} pessoas por vez."

        def col(k, m=mapa):
            i = m.get(k)
            return row[i].strip() if i is not None and i < len(row) else ""
        nome = " ".join(x for x in (col("nome") or col("primeiro"), col("sobrenome")) if x)
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
        tipo_valor = "hora" if eua or (col("valor_hora") and not col("valor")) else "mensal"
        if col("tipo_valor"):
            t = TIPOS_VALOR.get(col("tipo_valor").lower())
            if t not in (("hora", "anual") if eua else ("mensal", "hora")):
                erros.append("tipo do salário não reconhecido (use mensal ou hora)" if not eua
                             else "tipo do salário não reconhecido (use hourly ou annual)")
            else:
                tipo_valor = t
        valor, bruto = 0, col("valor") or col("valor_hora")
        if bruto:
            v = ler_centavos(bruto)
            if tipo_valor == "hora" and (v is None or v > 100_000):
                erros.append("valor da hora inválido")
            elif v is None or v > 10_000_000:
                erros.append("salário inválido")
            else:
                valor = v
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
        vt = 0
        if col("vt"):
            vt = ler_centavos(col("vt"))
            if vt is None or vt > 1_000_000:
                erros.append("vale-transporte inválido")
                vt = 0
        local_id = setor_id = None
        if col("local"):
            local_id = locais.get(planilha.norm(col("local")))
            if not local_id:
                erros.append(f"local não encontrado: {col('local')}")
        if col("setor"):
            setor_id = setores.get(planilha.norm(col("setor")))
            if not setor_id:
                erros.append(f"setor não encontrado: {col('setor')}")
        datas = {}
        for k in ("inicio", "nascimento", "vigencia"):
            if col(k):
                try:
                    datas[k] = _data(col(k), eua)
                except ValueError:
                    erros.append({"inicio": "data de admissão inválida", "vigencia": "data de vigência inválida"}.get(
                        k, "data de nascimento inválida"))
        classe = col("classificacao").lower().replace(" ", "").replace("-", "")
        tabela = CONTRATOS_US if eua else CONTRATOS
        if classe and classe not in tabela:
            erros.append("classificação não reconhecida (use W-2, 1099 ou 1099 business)" if eua else
                         "contrato não reconhecido (use CLT, Aprendiz, Intermitente, Estágio ou PJ)")
        classe = tabela.get(classe, "w2" if eua else "clt")
        dados_rh, rh_ignorado = {}, False
        if mapa_rh:
            form = {}
            for campo in mapa_rh:
                txt = col(campo, mapa_rh)
                if not txt:
                    continue
                _, rotulo, tipo, extra, grupo = defs_rh[campo]
                if not rh_ok or (grupo in rh.SO_PAGAMENTO and not ve_banco):
                    rh_ignorado = True
                    continue
                if tipo == "data":
                    try:
                        txt = _data(txt).isoformat()
                    except ValueError:
                        erros.append(f"data inválida em {planilha.cabecalho_rh(campo, rotulo)}")
                        continue
                v = planilha.valor_rh(campo, tipo, extra, txt)
                if v is None:
                    erros.append(f"valor não reconhecido em {planilha.cabecalho_rh(campo, rotulo)}: {txt}")
                    continue
                form[f"rh_{campo}"] = v
            if form:
                lidos, erro_rh = rh.ler(form, ve_banco)
                if erro_rh:
                    erros.append(erro_rh)
                dados_rh = {k: v for k, v in lidos.items() if v}
                cpf = dados_rh.get("cpf", "")
                if cpf and (cpf in cpfs or cpf in vistos_cpf):
                    erros.append("CPF já cadastrado ou repetido na planilha")
                elif cpf:
                    vistos_cpf.add(cpf)
        vistos.add(cel)
        if email:
            vistos_email.add(email)
        linhas.append({"linha": n, "erros": erros, "rh_ignorado": rh_ignorado, "dados": {
            "nome": nome[:120], "celular": cel, "email": email, "cargo": col("cargo")[:80], "valor": valor,
            "tipo_valor": tipo_valor, "jornada": jornada, "dependentes": dependentes, "vt": vt,
            "local_id": local_id, "setor_id": setor_id, "rh": dados_rh,
            "gorjeta": col("gorjeta").lower() in SIM, "inicio": datas.get("inicio"), "vigencia": datas.get("vigencia"),
            "nascimento": datas.get("nascimento"), "classificacao": classe}})
    if not linhas:
        return [], "Nenhuma pessoa encontrada na planilha."
    return linhas, None


# ------------------------------------------------------------------ telas
@rota.get("/painel/importar-equipe")
def tela(request: Request, u: Usuario = Depends(gestor_logado)):
    return render(request, "painel_importar.html", u=u, e=u.empresa, aba="equipe", linhas=None, conteudo="", erro=None)


def _listas(db: Session, e) -> tuple[list[str], list[str]]:
    locais = [x.nome for x in db.scalars(select(Local).where(Local.empresa_id == e.id, Local.ativo.is_(True))
                                         .order_by(Local.nome))]
    setores = [x.nome for x in db.scalars(select(Setor).where(Setor.empresa_id == e.id).order_by(Setor.nome))]
    return locais, setores


@rota.get("/painel/importar-equipe/modelo.csv")
def modelo(u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    return Response(planilha.gerar_csv(getattr(e, "pais", "BR"), *_listas(db, e)), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modelo-equipe.csv"'})


@rota.get("/painel/importar-equipe/modelo.xlsx")
def modelo_xlsx(u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    return Response(planilha.gerar_xlsx(getattr(e, "pais", "BR"), *_listas(db, e)),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="modelo-equipe.xlsx"'})


@rota.post("/painel/importar-equipe", dependencies=CSRF)
async def previa(request: Request, arquivo: UploadFile = File(None), u: Usuario = Depends(gestor_logado),
                 db: Session = Depends(get_db)):
    e = u.empresa
    erro, linhas, conteudo = None, None, ""
    if not arquivo or not arquivo.filename:
        erro = "Escolha o arquivo da planilha (CSV ou Excel)."
    else:
        bruto = await arquivo.read(MAX_BYTES + 1)
        if len(bruto) > MAX_BYTES:
            erro = "O arquivo passa de 1 MB."
        elif bruto[:2] == b"PK" or arquivo.filename.lower().endswith((".xlsx", ".xlsm")):
            try:
                conteudo = planilha.xlsx_para_csv(bruto, MAX_LINHAS)
            except Exception:   # arquivo corrompido, .xls antigo, sem openpyxl...
                erro = "Não consegui abrir o Excel. Salve como .xlsx ou como CSV e envie de novo."
        else:
            for cod in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    conteudo = bruto.decode(cod)
                    break
                except UnicodeDecodeError:
                    continue
        if not erro:
            linhas, erro = ler(conteudo, db, e.id, u)
    lim = limite_funcionarios(e)
    vagas = max(0, lim - funcionarios_ativos(db, e.id)) if lim else None
    return render(request, "painel_importar.html", 400 if erro and not linhas else 200, u=u, e=e, aba="equipe",
                  linhas=linhas, conteudo=conteudo, erro=erro, vagas=vagas,
                  rh_ignorado=any(x.get("rh_ignorado") for x in linhas or []))


@rota.post("/painel/importar-equipe/confirmar", dependencies=CSRF)
def confirmar(request: Request, conteudo: str = Form(""), u: Usuario = Depends(gestor_logado),
              db: Session = Depends(get_db)):
    from .ficha import _aplicar
    e = u.empresa
    linhas, erro = ler(conteudo[:MAX_BYTES], db, e.id, u)
    boas = [x["dados"] for x in linhas if not x["erros"]]
    if erro and not boas:
        avisar(request, erro, "erro")
        return ir("/painel/importar-equipe")
    lim = limite_funcionarios(e)
    if lim:
        boas = boas[:max(0, lim - funcionarios_ativos(db, e.id))]
    com_documentos = 0
    for d in boas:
        p = Usuario(empresa_id=e.id, nome=d["nome"], papel="funcionario", convite_token=secrets.token_urlsafe(18))
        db.add(p)
        db.flush()
        # mesmas regras da ficha (salário mensal ou por hora; nos EUA, por hora ou anual)
        _aplicar(db, e, p, {
            "nome": d["nome"], "email": d["email"], "celular": d["celular"], "nivel": "funcionario",
            "classificacao": d["classificacao"], "data_admissao": d["inicio"], "ponto_desde": d.get("vigencia"),
            "nascimento": d["nascimento"], "gorjeta": d["gorjeta"], "local_id": d.get("local_id"),
            "setor_id": d.get("setor_id"), "na_escala": True, "jornada_mensal": d["jornada"],
            "dependentes": d["dependentes"], "vt": d.get("vt", 0),
            "funcoes": [{"nome": d["cargo"], "valor": d["valor"], "tipo": d["tipo_valor"]}]
            if d["cargo"] or d["valor"] else []})
        if d.get("rh"):
            rh.salvar(db, p, d["rh"])
            com_documentos += 1
    regras.registrar(db, e.id, u.nome, "equipe importada", f"{len(boas)} pessoa(s) importada(s) de planilha."
                     + (f" Documentos e dados pessoais de {com_documentos}." if com_documentos else ""))
    db.commit()
    pulados = len(linhas) - len(boas)
    avisar(request, f"{len(boas)} pessoa(s) importada(s). {pulados} linha(s) ficaram de fora. Envie o link de acesso para cada pessoa."
           if pulados else f"{len(boas)} pessoa(s) importada(s). Envie o link de acesso para cada pessoa.")
    return ir("/painel/equipe")
