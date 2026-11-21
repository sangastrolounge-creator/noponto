"""Dados de RH na ficha do colaborador: documentos (CPF, RG, CTPS, PIS...), dados pessoais,
endereço, dados bancários e contato de emergência. Os campos seguem o cadastro do trabalhador
do eSocial (S-2200). Os dados bancários só aparecem para quem vê a folha de pagamento."""
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import DadosRH, Usuario

UFS = ("AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO").split()
SEXOS = {"F": "Feminino", "M": "Masculino"}
ESTADOS_CIVIS = {"solteiro": "Solteiro(a)", "casado": "Casado(a)", "uniao": "União estável",
                 "divorciado": "Divorciado(a)", "separado": "Separado(a)", "viuvo": "Viúvo(a)"}
INSTRUCAO = {"01": "Analfabeto", "02": "Até o 5º ano incompleto do fundamental",
             "03": "5º ano completo do fundamental", "04": "Do 6º ao 9º ano do fundamental incompleto",
             "05": "Fundamental completo", "06": "Médio incompleto", "07": "Médio completo",
             "08": "Superior incompleto", "09": "Superior completo", "10": "Pós-graduação completa",
             "11": "Mestrado completo", "12": "Doutorado completo"}
RACAS = {"1": "Branca", "2": "Preta", "3": "Parda", "4": "Amarela", "5": "Indígena", "6": "Não informada"}
TIPOS_CONTA = {"corrente": "Conta corrente", "poupanca": "Poupança", "salario": "Conta salário"}
CNH_CATEGORIAS = ("A", "B", "C", "D", "E", "AB", "AC", "AD", "AE")
DEFICIENCIAS = {"nao": "Não possui", "fisica": "Física", "auditiva": "Auditiva", "visual": "Visual",
                "intelectual": "Intelectual", "mental": "Mental", "reabilitado": "Reabilitado"}
CATEGORIAS_ESOCIAL = {"101": "101 - Empregado geral (CLT)", "103": "103 - Aprendiz",
                      "105": "105 - Contrato a termo (Lei 9.601/1998)", "106": "106 - Trabalhador temporário",
                      "111": "111 - Contrato intermitente", "701": "701 - Contribuinte individual (autônomo)",
                      "901": "901 - Estagiário"}
TIPOS_CONTRATO = {"indeterminado": "Prazo indeterminado", "experiencia": "Contrato de experiência",
                  "determinado": "Prazo determinado", "intermitente": "Intermitente"}
INSALUBRIDADE = {"0": "Não", "10": "Grau mínimo (10%)", "20": "Grau médio (20%)", "40": "Grau máximo (40%)"}
SIM_NAO = {"nao": "Não", "sim": "Sim"}
CAMISAS = ("PP", "P", "M", "G", "GG", "XG", "XXG")

# (campo, rótulo, tipo, tamanho máximo ou opções, grupo). tipo: texto | cpf | pis | data | uf | opcao | cep
CAMPOS = [
    ("cpf", "CPF", "cpf", 11, "documentos"),
    ("pis", "PIS/PASEP/NIS", "pis", 11, "documentos"),
    ("rg_numero", "RG (número)", "texto", 20, "documentos"),
    ("rg_orgao", "RG: órgão emissor", "texto", 20, "documentos"),
    ("rg_uf", "RG: UF", "uf", UFS, "documentos"),
    ("rg_emissao", "RG: data de emissão", "data", 0, "documentos"),
    ("ctps_numero", "Carteira de Trabalho (número)", "texto", 11, "documentos"),
    ("ctps_serie", "Carteira de Trabalho (série)", "texto", 5, "documentos"),
    ("ctps_uf", "Carteira de Trabalho: UF", "uf", UFS, "documentos"),
    ("titulo_eleitor", "Título de eleitor", "texto", 14, "documentos"),
    ("titulo_zona", "Título: zona", "texto", 4, "documentos"),
    ("titulo_secao", "Título: seção", "texto", 5, "documentos"),
    ("cnh_numero", "CNH (número)", "texto", 12, "documentos"),
    ("cnh_categoria", "CNH: categoria", "opcao", CNH_CATEGORIAS, "documentos"),
    ("cnh_validade", "CNH: validade", "data", 0, "documentos"),
    ("reservista", "Certificado de reservista", "texto", 20, "documentos"),
    ("sexo", "Sexo", "opcao", SEXOS, "pessoais"),
    ("estado_civil", "Estado civil", "opcao", ESTADOS_CIVIS, "pessoais"),
    ("instrucao", "Grau de instrução", "opcao", INSTRUCAO, "pessoais"),
    ("raca_cor", "Raça/cor (eSocial)", "opcao", RACAS, "pessoais"),
    ("nome_social", "Nome social", "texto", 120, "pessoais"),
    ("deficiencia", "Pessoa com deficiência", "opcao", DEFICIENCIAS, "pessoais"),
    ("nacionalidade", "Nacionalidade", "texto", 40, "pessoais"),
    ("naturalidade", "Naturalidade (cidade)", "texto", 60, "pessoais"),
    ("naturalidade_uf", "Naturalidade: UF", "uf", UFS, "pessoais"),
    ("nome_mae", "Nome da mãe", "texto", 120, "pessoais"),
    ("nome_pai", "Nome do pai", "texto", 120, "pessoais"),
    ("email_pessoal", "E-mail pessoal", "texto", 120, "pessoais"),
    ("matricula", "Matrícula eSocial", "texto", 30, "contrato"),
    ("categoria_esocial", "Categoria eSocial", "opcao", CATEGORIAS_ESOCIAL, "contrato"),
    ("tipo_contrato", "Tipo de contrato", "opcao", TIPOS_CONTRATO, "contrato"),
    ("fim_experiencia", "Fim do período de experiência", "data", 0, "contrato"),
    ("vencimento_contrato", "Vencimento do contrato", "data", 0, "contrato"),
    ("centro_custo", "Centro de custo", "texto", 40, "contrato"),
    ("insalubridade", "Insalubridade", "opcao", INSALUBRIDADE, "contrato"),
    ("periculosidade", "Periculosidade (30%)", "opcao", SIM_NAO, "contrato"),
    ("filhos", "Quantidade de filhos", "texto", 2, "outros"),
    ("camisa", "Tamanho de camisa", "opcao", CAMISAS, "outros"),
    ("cep", "CEP", "cep", 8, "endereco"),
    ("logradouro", "Logradouro (rua, avenida)", "texto", 120, "endereco"),
    ("numero", "Número", "texto", 10, "endereco"),
    ("complemento", "Complemento", "texto", 60, "endereco"),
    ("bairro", "Bairro", "texto", 60, "endereco"),
    ("cidade", "Cidade", "texto", 60, "endereco"),
    ("uf", "UF", "uf", UFS, "endereco"),
    ("banco", "Banco (nome ou código)", "texto", 40, "banco"),
    ("agencia", "Agência", "texto", 10, "banco"),
    ("conta", "Conta com dígito", "texto", 20, "banco"),
    ("tipo_conta", "Tipo de conta", "opcao", TIPOS_CONTA, "banco"),
    ("pix", "Chave PIX", "texto", 120, "banco"),
    ("emergencia_nome", "Contato de emergência (nome)", "texto", 120, "emergencia"),
    ("emergencia_parentesco", "Parentesco", "texto", 40, "emergencia"),
    ("emergencia_telefone", "Telefone de emergência", "texto", 20, "emergencia"),
]
GRUPOS = {"documentos": "Documentos", "pessoais": "Dados pessoais", "contrato": "Contrato e adicionais",
          "endereco": "Endereço", "banco": "Dados bancários", "emergencia": "Contato de emergência",
          "outros": "Outros"}
SO_PAGAMENTO = {"banco"}   # grupos que só quem vê a folha de pagamento enxerga e altera


def so_digitos(txt: str) -> str:
    return re.sub(r"\D", "", txt or "")


def cpf_valido(cpf: str) -> bool:
    c = so_digitos(cpf)
    if len(c) != 11 or c == c[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(c[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(c[n]):
            return False
    return True


def pis_valido(pis: str) -> bool:
    p = so_digitos(pis)
    if len(p) != 11 or p == p[0] * 11:
        return False
    pesos = (3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    resto = sum(int(p[i]) * pesos[i] for i in range(10)) % 11
    dv = 0 if resto < 2 else 11 - resto
    return dv == int(p[10])


def fmt_cpf(c: str) -> str:
    c = so_digitos(c)
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}" if len(c) == 11 else c


def fmt_pis(p: str) -> str:
    p = so_digitos(p)
    return f"{p[:3]}.{p[3:8]}.{p[8:10]}-{p[10:]}" if len(p) == 11 else p


def fmt_cep(c: str) -> str:
    c = so_digitos(c)
    return f"{c[:5]}-{c[5:]}" if len(c) == 8 else c


def mostrar(campo: str, valor: str) -> str:
    """Valor como aparece no formulário (CPF, PIS e CEP formatados)."""
    if not valor:
        return ""
    return {"cpf": fmt_cpf, "pis": fmt_pis, "cep": fmt_cep}.get(campo, lambda x: x)(valor)


def grupos_visiveis(ve_banco: bool) -> list[str]:
    return [g for g in GRUPOS if ve_banco or g not in SO_PAGAMENTO]


def ler(form, ve_banco: bool) -> tuple[dict, str | None]:
    """Lê os campos de RH do formulário. Devolve (valores, erro). Campos vazios ficam ""."""
    d = {}
    for campo, rotulo, tipo, extra, grupo in CAMPOS:
        if grupo in SO_PAGAMENTO and not ve_banco:
            continue
        v = " ".join(str(form.get(f"rh_{campo}", "")).split())
        if tipo in ("cpf", "pis", "cep"):
            v = so_digitos(v)
            if v and tipo == "cpf" and not cpf_valido(v):
                return d, "CPF inválido: confira os números."
            if v and tipo == "pis" and not pis_valido(v):
                return d, "PIS/PASEP inválido: confira os números."
            if v and tipo == "cep" and len(v) != 8:
                return d, "CEP inválido: use 8 números (ex.: 65000-000)."
        elif tipo == "data" and v:
            try:
                dt = date.fromisoformat(v)
            except ValueError:
                return d, f"Data inválida em “{rotulo}”."
            if dt.year < 1900 or dt.year > 2100:
                return d, f"Data inválida em “{rotulo}”."
        elif tipo in ("uf", "opcao") and v and v not in extra:
            return d, f"Escolha uma opção válida em “{rotulo}”."
        elif tipo == "texto":
            v = v[:extra]
        if campo == "filhos" and v and not v.isdigit():
            return d, "Quantidade de filhos: use um número."
        if campo == "email_pessoal" and v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            return d, "E-mail pessoal inválido."
        d[campo] = v
    return d, None


def da_pessoa(db: Session, u: Usuario) -> DadosRH | None:
    return db.scalar(select(DadosRH).where(DadosRH.usuario_id == u.id))


def salvar(db: Session, u: Usuario, d: dict) -> list[str]:
    """Grava os valores lidos; devolve os rótulos dos campos que mudaram (sem os valores)."""
    r = da_pessoa(db, u)
    if not r:
        r = DadosRH(empresa_id=u.empresa_id, usuario_id=u.id)
        db.add(r)
    mudou = []
    rotulos = {c[0]: c[1] for c in CAMPOS}
    for campo, v in d.items():
        if (getattr(r, campo) or "") != v:
            setattr(r, campo, v)
            mudou.append(rotulos[campo])
    return mudou


def cpf_repetido(db: Session, empresa_id: int, cpf: str, outro_id: int | None) -> bool:
    if not cpf:
        return False
    q = select(DadosRH.id).where(DadosRH.empresa_id == empresa_id, DadosRH.cpf == cpf)
    if outro_id:
        q = q.where(DadosRH.usuario_id != outro_id)
    return bool(db.scalar(q.execution_options(alcance_livre=True)))
