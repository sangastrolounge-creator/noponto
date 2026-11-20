"""Junta todos os textos a traduzir e mostra quais ainda não têm tradução.

- Telas: as chaves de _('...') nos templates.
- Código Python: textos e f-strings em português (avisos, erros, alertas). Uma f-string vira
  um modelo com {v1}, {v2}...; na hora de traduzir, o texto pronto é reconhecido pelo modelo.
- Scripts: as chaves de t("...") em <script> e em app/static/*.js.

Uso: .venv/Scripts/python tools/i18n_extrair.py            -> resumo do que falta
     .venv/Scripts/python tools/i18n_extrair.py --faltando en > faltando.json
"""
import ast
import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "app"
LETRA = re.compile(r"[A-Za-zÀ-ÿ]")
CHAVE_TPL = re.compile(r"""\b_\(\s*'((?:[^'\\]|\\.)*)'""")
CHAVE_JS = re.compile(r"""\bt\(\s*"((?:[^"\\]|\\.)*)"\s*\)""")
PY_FORA = {"db.py", "i18n.py"}


def _des(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in s else s


def do_template() -> list[str]:
    achados = []
    for p in sorted(RAIZ.joinpath("templates").rglob("*.html")):
        achados += [_des(m.group(1)) for m in CHAVE_TPL.finditer(p.read_text(encoding="utf-8"))]
    return achados


def do_js() -> list[str]:
    achados = []
    fontes = list(RAIZ.joinpath("templates").rglob("*.html")) + list(RAIZ.joinpath("static").glob("*.js"))
    for p in sorted(fontes):
        achados += [json.loads('"' + m.group(1) + '"') for m in CHAVE_JS.finditer(p.read_text(encoding="utf-8"))]
    return achados


def _parece_texto(s: str) -> bool:
    s2 = re.sub(r"\{v\d+\}", "", s).strip()
    if len(s2) < 2 or not LETRA.search(s2):
        return False
    if any(x in s for x in ("<", "\\", "SELECT", "INSERT", "/static", "http", "sqlite", "%(")):
        return False
    if re.fullmatch(r"[a-z0-9_\-./:]+", s2):        # identificadores, caminhos, códigos
        return False
    if re.fullmatch(r"[A-Z0-9_]+", s2) or re.fullmatch(r"[A-Z][a-zA-Z]*(-[A-Z][a-zA-Z]*)+", s2):
        return False                                 # constantes e nomes de cabeçalho HTTP
    if re.search(r"%[a-zA-Z]", s) or ("=" in s and ";" in s) or s.startswith(("text/", "image/", "application/")):
        return False                                 # formatos de data, cabeçalhos, tipos de arquivo
    return " " in s2 or bool(re.search(r"[À-ÿ]", s2)) or s2[:1].isupper()


def _modelo(no: ast.JoinedStr) -> str:
    partes, n = [], 0
    for v in no.values:
        if isinstance(v, ast.Constant):
            partes.append(str(v.value))
        else:
            n += 1
            partes.append("{v%d}" % n)
    return "".join(partes)


def do_python() -> list[str]:
    achados = []
    for p in sorted(RAIZ.glob("*.py")):
        if p.name in PY_FORA:
            continue
        arvore = ast.parse(p.read_text(encoding="utf-8"))
        pular = set()
        for no in ast.walk(arvore):  # docstrings e logs não são mostrados a ninguém
            if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and no.body \
                    and isinstance(no.body[0], ast.Expr) and isinstance(no.body[0].value, ast.Constant):
                pular.add(id(no.body[0].value))
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute) and \
                    isinstance(no.func.value, ast.Name) and no.func.value.id in ("log", "re"):
                for a in ast.walk(no):
                    pular.add(id(a))
            if isinstance(no, ast.JoinedStr):
                for v in no.values:
                    pular.add(id(v))
        for no in ast.walk(arvore):
            if id(no) in pular:
                continue
            if isinstance(no, ast.Constant) and isinstance(no.value, str):
                s = no.value
            elif isinstance(no, ast.JoinedStr):
                s = _modelo(no)
            else:
                continue
            if _parece_texto(s):
                achados.append(s)
    return achados


# Textos montados em tempo de execução que não aparecem literais no código.
EXTRAS = ["atestado médico", "férias", "licença", "afastamento (inss)", "falta abonada", "outro",
          "publicada", "rascunho", "não criada", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
# Nomes próprios e textos que ficam iguais em todos os idiomas.
IGUAIS = {"Turno+", "No Ponto", "WhatsApp", "Pix", "PIN", "PDF", "CSV", "QR Code", "Master", "R$", "DSR",
          "Tiradentes", "Natal", "Finados", "Confraternização Universal", "Independência do Brasil",
          "Nossa Senhora Aparecida", "Proclamação da República", "Consciência Negra", "Sexta-feira Santa",
          "Dia do Trabalho", "Asaas", "Google Maps"}


def todos() -> dict[str, list[str]]:
    return {"telas": do_template(), "python": do_python(), "js": do_js(), "extras": EXTRAS}


if __name__ == "__main__":
    grupos = todos()
    unicos = list(dict.fromkeys(k for g in grupos.values() for k in g))
    if len(sys.argv) > 2 and sys.argv[1] == "--faltando":
        cod = sys.argv[2]
        arq = RAIZ / "idiomas" / f"{cod}.json"
        tab = json.loads(arq.read_text(encoding="utf-8")) if arq.is_file() else {}
        print(json.dumps([k for k in unicos if k not in tab and k not in IGUAIS], ensure_ascii=False, indent=0))
    elif len(sys.argv) > 1 and sys.argv[1] == "--js":
        print(json.dumps(list(dict.fromkeys(grupos["js"])), ensure_ascii=False))
    else:
        for g, lst in grupos.items():
            print(g, len(set(lst)))
        print("total", len(unicos), "| caracteres", sum(len(k) for k in unicos))
