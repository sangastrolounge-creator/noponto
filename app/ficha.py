"""Ficha da pessoa: cadastro completo numa página própria.

Contato, local e data de admissão, funções com salário (mensal ou por hora; pode ter mais de
uma), jornada mensal, dependentes para o IRRF, vale-transporte, gorjeta, nível de acesso
(Funcionário, Gerente, Gerente Geral) e o tipo de contrato (CLT, aprendiz, intermitente,
estágio ou PJ). A primeira função é a principal: define o cargo e o salário."""
import secrets
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import master, regras, salario_minimo
from .core import (PERMISSOES, avisar, celular_limpo, celular_valido, da_empresa, data_iso,
                   email_valido, encaixar_no_alcance, exigir_csrf, gestor_logado, ir, ler_centavos, pode, render,
                   alcance_locais, alcance_setores, restrito)
from . import rh
from .db import FuncaoUsuario, Local, Setor, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
NIVEIS = {"funcionario": "Funcionário", "gerente": "Gerente", "gerente_geral": "Gerente Geral"}
CLASSIFICACOES = {"clt": ("Empregado CLT", "Carteira assinada"),
                  "aprendiz": ("Aprendiz", "Contrato de aprendizagem (14 a 24 anos)"),
                  "intermitente": ("Intermitente", "CLT, convocado por período e pago por hora"),
                  "estagio": ("Estagiário", "Bolsa de estágio, sem carteira (Lei 11.788)"),
                  "pj": ("Prestador PJ", "Empresa ou MEI que emite nota: não entra na folha")}
CLASSIFICACOES_US = {"w2": ("Empregado", "Recebe W-2"),
                     "1099_ind": ("Prestador autônomo (pessoa física)", "Recebe 1099"),
                     "1099_emp": ("Prestador (empresa)", "Recebe 1099")}
PERIODOS_ANO = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}
PERM_GERENTE = "escala,ponto,mural,tarefas,ausencias"


def classificacoes(e) -> dict:
    return CLASSIFICACOES_US if getattr(e, "pais", "BR") == "US" else CLASSIFICACOES
MAX_FUNCOES = 3


def nivel_de(u: Usuario) -> str:
    if not u.gestor:
        return "funcionario"
    return "gerente_geral" if set(PERMISSOES) <= set((u.permissoes or "").split(",")) else "gerente"


def contratado(u: Usuario) -> bool:
    """Prestador (PJ no Brasil, 1099 nos EUA): não é empregado, não entra na folha nem nas regras
    de hora extra."""
    c = u.classificacao or ""
    return c == "pj" or c.startswith("1099")


def funcoes(db: Session, u: Usuario) -> list[FuncaoUsuario]:
    return list(db.scalars(select(FuncaoUsuario).where(FuncaoUsuario.usuario_id == u.id)
                           .order_by(FuncaoUsuario.ordem, FuncaoUsuario.id)))


def nomes_de_funcao(db: Session, u: Usuario) -> set[str]:
    """Todas as funções da pessoa (para turnos abertos de um cargo), em minúsculas."""
    nomes = {f.nome.strip().lower() for f in funcoes(db, u) if f.nome.strip()}
    if u.cargo:
        nomes.add(u.cargo.strip().lower())
    return nomes


def _ler(form, e) -> tuple[dict, str | None]:
    """Lê e confere o formulário. Devolve (dados, erro)."""
    nome = " ".join(p for p in (str(form.get("nome", "")).strip(), str(form.get("sobrenome", "")).strip()) if p)[:120]
    email = str(form.get("email", "")).strip().lower()
    cel = celular_limpo(str(form.get("celular", "")))
    nivel = str(form.get("nivel", "funcionario"))
    eua = getattr(e, "pais", "BR") == "US"
    classe = str(form.get("classificacao", "w2" if eua else "clt"))
    inicio = data_iso(str(form.get("data_admissao", "")), None) if form.get("data_admissao") else None
    nasc = data_iso(str(form.get("nascimento", "")), None) if form.get("nascimento") else None
    vigencia = data_iso(str(form.get("ponto_desde", "")), None) if form.get("ponto_desde") else None
    funcs = []
    for i in range(MAX_FUNCOES):
        fn = str(form.get(f"funcao_{i}", "")).strip()[:80]
        valor_txt = str(form.get(f"valor_{i}", "")).strip()
        tipo = str(form.get(f"tipo_{i}", "hora" if eua else "mensal"))
        if not fn and not valor_txt:
            continue
        valor = ler_centavos(valor_txt) if valor_txt else 0
        if valor is None or tipo not in (("hora", "anual") if eua else ("hora", "mensal")) or (tipo == "hora" and valor > 100000):
            if eua:
                return {}, f"Confira o valor da função {fn or i + 1}: em dólares, como 18.50 por hora ou 52000 por ano."
            return {}, f"Confira o valor da função {fn or i + 1}: em reais, como 1.850,00 por mês ou 12,50 por hora."
        funcs.append({"nome": fn, "valor": valor, "tipo": tipo})
    def inteiro(campo, padrao, minimo, maximo):
        txt = str(form.get(campo, "")).strip()
        if not txt:
            return padrao
        return int(txt) if txt.isdigit() and minimo <= int(txt) <= maximo else None
    jornada = inteiro("jornada_mensal", 220, 1, 300)
    dependentes = inteiro("dependentes", 0, 0, 30)
    vt = ler_centavos(str(form.get("vt", "")).strip() or "0")
    d = {"nome": nome, "jornada_mensal": jornada, "dependentes": dependentes, "vt": vt, "email": email, "celular": cel, "nivel": nivel, "classificacao": classe,
         "data_admissao": inicio, "ponto_desde": vigencia, "nascimento": nasc, "funcoes": funcs, "gorjeta": bool(form.get("gorjeta")),
         "local_id": int(form["local_id"]) if str(form.get("local_id", "")).isdigit() else None,
         "setor_id": int(form["setor_id"]) if str(form.get("setor_id", "")).isdigit() else None,
         "na_escala": bool(form.get("na_escala")), "convidar": bool(form.get("convidar"))}
    if not nome:
        return d, "Informe o nome."
    if nivel not in NIVEIS or classe not in classificacoes(e):
        return d, "Escolha o nível de acesso e a classificação."
    if nivel == "funcionario" and not celular_valido(cel):
        if eua:
            return d, "Funcionário entra pelo celular: informe o celular com o código de área (ex.: 305 555-0123)."
        return d, "Funcionário entra pelo celular: informe o celular com DDD (ex.: 98 99999-1234)."
    if nivel != "funcionario" and not email_valido(email):
        return d, "Gerente entra com o e-mail: informe um e-mail válido."
    if email and not email_valido(email):
        return d, "E-mail inválido."
    if cel and not celular_valido(cel):
        if eua:
            return d, "Celular inválido: use o código de área (ex.: 305 555-0123)."
        return d, "Celular inválido: use o DDD (ex.: 98 99999-1234)."
    if form.get("data_admissao") and not inicio:
        return d, "Data de admissão inválida."
    if form.get("ponto_desde") and not vigencia:
        return d, "Data de vigência inválida."
    if jornada is None:
        return d, "Jornada mensal: use as horas por mês (220 para 44 h por semana, 180 para 36 h, 150 para 30 h)."
    if dependentes is None:
        return d, "Dependentes: use um número de 0 a 30."
    if vt is None:
        return d, "Vale-transporte: informe o custo do mês em reais (ou deixe em branco)."
    if form.get("nascimento") and (not nasc or nasc > date.today() or nasc.year < 1900):
        return d, "Data de nascimento inválida."
    return d, None


def _aplicar(db: Session, e, alvo: Usuario, d: dict) -> None:
    alvo.nome, alvo.email, alvo.celular = d["nome"], d["email"] or None, d["celular"] or None
    alvo.classificacao, alvo.data_admissao, alvo.local_id = d["classificacao"], d["data_admissao"], d["local_id"]
    alvo.gorjeta = d["gorjeta"]
    s = db.get(Setor, d["setor_id"]) if d.get("setor_id") else None
    alvo.setor_id = s.id if s and s.empresa_id == e.id else None
    alvo.nascimento = d["nascimento"]
    alvo.ponto_desde = d.get("ponto_desde")
    alvo.jornada_mensal, alvo.dependentes, alvo.vt_centavos = d["jornada_mensal"], d["dependentes"], d["vt"]
    if not alvo.dono:
        if d["nivel"] == "funcionario":
            alvo.papel, alvo.permissoes, alvo.na_escala = "funcionario", "", True
        else:
            alvo.papel = "gestor"
            alvo.permissoes = ",".join(PERMISSOES) if d["nivel"] == "gerente_geral" else (
                alvo.permissoes if nivel_de(alvo) == "gerente" and alvo.permissoes else PERM_GERENTE)
            alvo.na_escala = d["na_escala"]
    for f in funcoes(db, alvo):
        db.delete(f)
    for i, f in enumerate(d["funcoes"]):
        db.add(FuncaoUsuario(empresa_id=e.id, usuario_id=alvo.id, nome=f["nome"], ordem=i,
                             valor_centavos=f["valor"], tipo=f["tipo"]))
    principal = d["funcoes"][0] if d["funcoes"] else None
    alvo.cargo = principal["nome"] if principal else ""
    if getattr(e, "pais", "BR") == "US":   # EUA: por hora, ou salário anual (isento de hora extra)
        if principal and principal["tipo"] == "anual":
            alvo.isento, alvo.taxa_hora_centavos = True, 0
            alvo.salario_periodo_centavos = round(principal["valor"] / PERIODOS_ANO.get(e.frequencia, 26))
        elif principal:
            alvo.isento, alvo.salario_periodo_centavos = False, 0
            alvo.taxa_hora_centavos = principal["valor"]
        return
    if principal and principal["tipo"] == "mensal":
        alvo.salario_centavos = principal["valor"]
    elif principal:   # por hora: o salário de referência é o valor da hora x a jornada do mês
        alvo.salario_centavos = principal["valor"] * (alvo.jornada_mensal or 220)


def _contexto(db: Session, e, alvo: Usuario | None, quem: Usuario | None = None) -> dict:
    fs = funcoes(db, alvo) if alvo else []
    if getattr(e, "pais", "BR") == "US":
        if alvo and not fs and (alvo.cargo or alvo.taxa_hora_centavos or alvo.isento):
            anual = alvo.salario_periodo_centavos * PERIODOS_ANO.get(e.frequencia, 26)
            fs = [FuncaoUsuario(nome=alvo.cargo, valor_centavos=anual if alvo.isento else alvo.taxa_hora_centavos,
                                tipo="anual" if alvo.isento else "hora")]
    elif alvo and not fs and (alvo.cargo or alvo.salario_centavos):
        fs = [FuncaoUsuario(nome=alvo.cargo, valor_centavos=alvo.salario_centavos, tipo="mensal")]
    return {"alvo": alvo, "funcs": fs, "NIVEIS": NIVEIS, "CLASSIFICACOES": classificacoes(e),
            "setores": list(db.scalars(select(Setor).where(Setor.empresa_id == e.id).order_by(Setor.nome))),
            "alc_s": alcance_setores(quem) if quem else set(), "alc_l": alcance_locais(quem) if quem else set(),
            "locais": list(db.scalars(select(Local).where(Local.empresa_id == e.id, Local.ativo.is_(True))
                                      .order_by(Local.nome))),
            "nivel_atual": nivel_de(alvo) if alvo else "funcionario", "MAX_FUNCOES": MAX_FUNCOES,
            "periodos": PERIODOS_ANO.get(getattr(e, "frequencia", "") or "", 26)}


def _rh_ctx(db: Session, alvo: Usuario | None, u: Usuario) -> dict:
    from .core import recurso_liberado
    if not recurso_liberado(u.empresa, "com_rh"):   # o plano não inclui os dados de RH
        return {"RH": rh, "rh_dados": None, "ve_banco": False, "rh_grupos": [], "rh_bloqueado": True}
    ve_banco = pode(u, "pagamento")
    return {"RH": rh, "rh_dados": rh.da_pessoa(db, alvo) if alvo else None, "ve_banco": ve_banco,
            "rh_grupos": rh.grupos_visiveis(ve_banco)}


def _ler_rh(db: Session, e, form, u: Usuario, alvo_id: int | None) -> tuple[dict, str | None]:
    from .core import recurso_liberado
    if not recurso_liberado(e, "com_rh") or getattr(e, "pais", "BR") != "BR":
        return {}, None
    dr, erro = rh.ler(form, pode(u, "pagamento"))
    if not erro and rh.cpf_repetido(db, e.id, dr.get("cpf", ""), alvo_id):
        erro = "Já existe outra pessoa com este CPF na empresa."
    return dr, erro


def _registrar_rh(db: Session, e, u: Usuario, alvo: Usuario, dr: dict) -> None:
    if not dr:
        return
    mudou = rh.salvar(db, alvo, dr)
    if mudou:   # só os nomes dos campos, nunca os números (LGPD)
        regras.registrar(db, e.id, u.nome, "documentos alterados", f"{alvo.nome}: {', '.join(mudou)}.")


# ------------------------------------------------------------------ rotas
@rota.get("/painel/equipe/novo")
def novo_form(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    return render(request, "painel_ficha.html", u=u, e=u.empresa, aba="equipe",
                  pode_gerente=u.dono, v=None, **_contexto(db, u.empresa, None, u if restrito(u) else None), **_rh_ctx(db, None, u))


@rota.post("/painel/equipe/novo", dependencies=CSRF)
async def novo(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    form = await request.form()
    d, erro = _ler(form, e)
    dr, erro_rh = _ler_rh(db, e, form, u, None)
    erro = erro or erro_rh
    if not erro and d["nivel"] != "funcionario" and not u.dono:
        erro = "Só o dono da conta cadastra gerentes."
    if not erro and d["email"] and db.scalar(select(Usuario.id).where(Usuario.email == d["email"]).execution_options(alcance_livre=True)):
        erro = "Já existe uma conta com este e-mail."
    if not erro and d["celular"] and db.scalar(select(Usuario.id).where(Usuario.celular == d["celular"]).execution_options(alcance_livre=True)):
        erro = "Este celular já está cadastrado."
    if not erro and d["nivel"] == "funcionario" and master.sem_vaga(db, e):
        erro = master.sem_vaga(db, e)
    if erro:
        return render(request, "painel_ficha.html", 400, u=u, e=e, aba="equipe", pode_gerente=u.dono,
                      erro=erro, v=form, **_contexto(db, e, None, u if restrito(u) else None), **_rh_ctx(db, None, u))
    alvo = Usuario(empresa_id=e.id, nome=d["nome"], papel="funcionario",
                   convite_token=secrets.token_urlsafe(18))
    db.add(alvo)
    db.flush()
    _aplicar(db, e, alvo, d)
    encaixar_no_alcance(u, alvo)
    _registrar_rh(db, e, u, alvo, dr)
    regras.registrar(db, e.id, u.nome, "pessoa cadastrada",
                     f"{alvo.nome}: {NIVEIS[d['nivel']]}, {classificacoes(e)[d['classificacao']][1]}"
                     + (f", {alvo.cargo}" if alvo.cargo else "") + ".")
    db.commit()
    avisar(request, f"{alvo.nome} cadastrado(a). Envie o link de acesso para criar a senha.")
    return ir(f"/painel/equipe#p{alvo.id}")


@rota.get("/painel/equipe/{fid}/ficha")
def ficha_form(request: Request, fid: int, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    alvo = da_empresa(db, Usuario, fid, u)
    if alvo.gestor and alvo.id != u.id and not u.dono:
        avisar(request, "Só o dono da conta muda os dados de outro gestor.", "erro")
        return ir("/painel/equipe")
    return render(request, "painel_ficha.html", u=u, e=u.empresa, aba="equipe",
                  pode_gerente=u.dono and not alvo.dono, v=None, **_contexto(db, u.empresa, alvo, u if restrito(u) else None), **_rh_ctx(db, alvo, u))


@rota.post("/painel/equipe/{fid}/ficha", dependencies=CSRF)
async def ficha(request: Request, fid: int, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    alvo = da_empresa(db, Usuario, fid, u)
    if alvo.gestor and alvo.id != u.id and not u.dono:
        avisar(request, "Só o dono da conta muda os dados de outro gestor.", "erro")
        return ir("/painel/equipe")
    form = await request.form()
    d, erro = _ler(form, e)
    dr, erro_rh = _ler_rh(db, e, form, u, alvo.id)
    erro = erro or erro_rh
    if alvo.dono:
        d["nivel"] = "gerente_geral"
    if not erro and d["nivel"] != nivel_de(alvo) and not u.dono:
        erro = "Só o dono da conta muda o nível de acesso."
    if not erro and d["email"] and db.scalar(select(Usuario.id).where(Usuario.email == d["email"], Usuario.id != alvo.id).execution_options(alcance_livre=True)):
        erro = "Já existe uma conta com este e-mail."
    if not erro and d["celular"] and db.scalar(select(Usuario.id).where(Usuario.celular == d["celular"], Usuario.id != alvo.id).execution_options(alcance_livre=True)):
        erro = "Este celular já está cadastrado."
    if erro:
        return render(request, "painel_ficha.html", 400, u=u, e=e, aba="equipe",
                      pode_gerente=u.dono and not alvo.dono, erro=erro, v=form, **_contexto(db, e, alvo, u if restrito(u) else None), **_rh_ctx(db, alvo, u))
    antes = nivel_de(alvo)
    _aplicar(db, e, alvo, d)
    encaixar_no_alcance(u, alvo)
    _registrar_rh(db, e, u, alvo, dr)
    regras.registrar(db, e.id, u.nome, "ficha alterada",
                     f"{alvo.nome}: {NIVEIS[antes]} → {NIVEIS[nivel_de(alvo)]}." if antes != nivel_de(alvo)
                     else f"{alvo.nome}: dados atualizados.")
    db.commit()
    baixos = [x for x in salario_minimo.abaixo(db, e, regras.hoje(e)) if x["u"].id == alvo.id]
    if baixos:
        avisar(request, salario_minimo.aviso(baixos), "erro")
    else:
        avisar(request, f"Ficha de {alvo.primeiro_nome} salva.")
    return ir(f"/painel/equipe#p{alvo.id}")
