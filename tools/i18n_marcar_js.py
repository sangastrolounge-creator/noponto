"""Envolve com t("...") os textos em português dos scripts (<script> das telas e static/*.js)
e grava a lista deles em app/idiomas/_js.json (só esses vão para a página, em window.T).

Uso: .venv/Scripts/python tools/i18n_marcar_js.py
"""
import json
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "app"
SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", re.S | re.I)
LIT = re.compile(r'(?<![\w$.=])"((?:[^"\\\n]|\\.)*)"')  # depois de "=" é atributo HTML
JA = re.compile(r'\bt\(\s*$')
NAO_TEXTO = {"Content-Type", "X-CSRF", "POST", "GET", "Escape", "ArrowRight", "ArrowLeft", "Enter",
             "DOMContentLoaded", "PushManager", "Notification", "use strict", "CHAVENOVA",
             "dialog[open], .tour", "receber avisos", "NotAllowedError"}


def texto_de_gente(s: str) -> bool:
    if "{{" in s or "{%" in s or not re.search(r"[A-Za-zÀ-ÿ]", s):
        return False
    if re.match(r"^[.#\[/:]|^https?:", s) or "=" in s or s.startswith("data-"):
        return False
    if s in NAO_TEXTO or s.startswith("h2:") or "[" in s or s.endswith("Error"):
        return False
    return " " in s.strip() or bool(re.search(r"[À-ÿ]", s)) or (s[:1].isupper() and len(s) > 3)


def marcar_js(codigo: str, achados: list[str]) -> str:
    def troca(m):
        s = m.group(1)
        linha = codigo[codigo.rfind("\n", 0, m.start()) + 1:m.start()]
        if JA.search(codigo[:m.start()][-6:]) or not texto_de_gente(s) or "//" in linha:
            return m.group(0)
        # chave de objeto ({ "texto": ... }) não é texto mostrado
        depois = codigo[m.end():m.end() + 3]
        if depois.lstrip().startswith(":") and codigo[:m.start()].rstrip()[-1:] in "{,":
            return m.group(0)
        achados.append(json.loads('"' + s + '"'))
        return "t(" + m.group(0) + ")"
    return LIT.sub(troca, codigo)


def main():
    achados: list[str] = []
    for p in sorted(RAIZ.joinpath("templates").rglob("*.html")):
        src = p.read_text(encoding="utf-8")
        novo = SCRIPT.sub(lambda m: m.group(1) + marcar_js(m.group(2), achados) + m.group(3), src)
        if novo != src:
            p.write_text(novo, encoding="utf-8")
    for p in sorted(RAIZ.joinpath("static").glob("*.js")):
        if p.name == "sw.js":
            continue
        src = p.read_text(encoding="utf-8")
        novo = marcar_js(src, achados)
        if novo != src:
            p.write_text(novo, encoding="utf-8")
    # todas as chaves t("...") existentes (inclusive as marcadas antes)
    chave = re.compile(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
    todas = []
    for p in list(RAIZ.joinpath("templates").rglob("*.html")) + list(RAIZ.joinpath("static").glob("*.js")):
        todas += [json.loads('"' + m.group(1) + '"') for m in chave.finditer(p.read_text(encoding="utf-8"))]
    todas = list(dict.fromkeys(todas))
    (RAIZ / "idiomas").mkdir(exist_ok=True)
    (RAIZ / "idiomas" / "_js.json").write_text(json.dumps(todas, ensure_ascii=False, indent=0), encoding="utf-8")
    print(len(achados), "marcados agora;", len(todas), "textos de script no total")


if __name__ == "__main__":
    main()
