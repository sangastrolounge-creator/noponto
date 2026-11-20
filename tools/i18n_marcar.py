"""Marca os textos das telas (templates Jinja) para tradução.

Troca cada trecho de texto visível por {{ _('texto') }}; partes com {{ valor }} no meio viram
{{ _('Olá, {v1}!', v1=(valor)) }}. Também marca os atributos placeholder, title, aria-label,
alt e data-confirma. Não mexe em <script>, <style>, nem em textos dentro de {% %} ou {{ }}.

Uso (da raiz do projeto):
  .venv/Scripts/python tools/i18n_marcar.py --listar  app/templates/entrar.html ...
  .venv/Scripts/python tools/i18n_marcar.py --aplicar app/templates/entrar.html ...
Rodar duas vezes no mesmo arquivo não marca de novo o que já foi marcado.
"""
import json
import re
import sys
from pathlib import Path

JINJA = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.S)
CRU = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
ATRIB = re.compile(r'(\s(?:placeholder|title|aria-label|alt|data-confirma)=")([^"]*)(")')
LETRA = re.compile(r"[A-Za-zÀ-ÿ]")
# Expressões que devolvem HTML pronto (macros, |safe): não podem virar parte de um texto traduzido.
NAO_JUNTA = re.compile(r"\|\s*safe|csrf|\b(etiqueta|cartoes|estrelas|item|botao|video|midia|campos|linha|"
                       r"campo_imagem|campo_video|quadro|seletor|_)\s*\(")


def _literal(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _converter(partes: list[tuple[str, str]], achados: list[str]) -> str:
    """partes = [('txt', texto) | ('expr', '{{ ... }}')] -> trecho marcado."""
    texto = "".join(s for k, s in partes if k == "txt")
    if not LETRA.search(texto) or "&" in texto:
        return "".join(s for _, s in partes)
    bruto = "".join(s if k == "txt" else "\0" for k, s in partes)
    ini = len(bruto) - len(bruto.lstrip())
    fim = len(bruto.rstrip())
    # separa o espaço das pontas, que fica fora da marcação
    saida_ini, saida_fim, meio, pos = "", "", [], 0
    for k, s in partes:
        tam = len(s) if k == "txt" else 1
        a, b = pos, pos + tam
        if k == "txt":
            antes = s[:max(0, min(tam, ini - a))]
            depois = s[max(0, fim - a):] if b > fim else ""
            centro = s[len(antes):tam - len(depois)] if tam - len(depois) >= len(antes) else ""
            saida_ini += antes
            if centro:
                meio.append(("txt", centro))
            saida_fim += depois
        else:
            meio.append((k, s))
        pos = b
    chave, args, n = "", [], 0
    for k, s in meio:
        if k == "txt":
            chave += re.sub(r"\s+", " ", s)
        else:
            n += 1
            chave += "{v%d}" % n
            args.append("v%d=(%s)" % (n, s[2:-2].strip()))
    achados.append(chave)
    chamada = "{{ _(%s%s) }}" % (_literal(chave), ", " + ", ".join(args) if args else "")
    return saida_ini + chamada + saida_fim


def _marcar_texto(segs: list[tuple[str, str]], achados: list[str]) -> str:
    """segs fora de tags: ('txt', s) | ('expr', s) | ('outro', s). Agrupa em frases."""
    saida, frase = [], []

    def fecha():
        if frase:
            saida.append(_converter(frase, achados))
            frase.clear()
    for k, s in segs:
        if k == "txt" or (k == "expr" and not NAO_JUNTA.search(s)):
            frase.append((k, s))
        else:
            fecha()
            saida.append(s)
    fecha()
    return "".join(saida)


def _marcar_atributos(tag: str, achados: list[str]) -> str:
    def troca(m):
        valor = m.group(2)
        if "{%" in valor or "_(" in valor or not LETRA.search(JINJA.sub("", valor)):
            return m.group(0)
        partes = [("expr" if JINJA.fullmatch(p) else "txt", p) for p in JINJA.split(valor) if p]
        if any(k == "expr" and NAO_JUNTA.search(p) for k, p in partes):
            return m.group(0)
        return m.group(1) + _converter(partes, achados) + m.group(3)
    return ATRIB.sub(troca, tag)


def marcar(fonte: str) -> tuple[str, list[str]]:
    guardados: list[str] = []

    def guarda(m):
        guardados.append(m.group(0))
        return f"\x01{len(guardados) - 1}\x01"
    texto = CRU.sub(guarda, fonte)
    achados: list[str] = []
    saida, segs, tag, em_tag = [], [], "", False
    for i, peca in enumerate(JINJA.split(texto)):
        if i % 2:  # trecho Jinja
            if em_tag:
                tag += peca
            else:
                segs.append(("expr" if peca.startswith("{{") else "outro", peca))
            continue
        j = 0
        while j < len(peca):
            c = peca[j]
            if em_tag:
                tag += c
                if c == ">":
                    em_tag = False
                    segs.append(("outro", _marcar_atributos(tag, achados)))
                    tag = ""
            elif c == "<" and j + 1 < len(peca) and (peca[j + 1].isalpha() or peca[j + 1] in "/!"):
                em_tag, tag = True, "<"
            elif c == "\x01":
                fim = peca.index("\x01", j + 1)
                segs.append(("outro", peca[j:fim + 1]))
                j = fim
            else:
                if segs and segs[-1][0] == "txt":
                    segs[-1] = ("txt", segs[-1][1] + c)
                else:
                    segs.append(("txt", c))
            j += 1
    if tag:
        segs.append(("outro", tag))
    resultado = _marcar_texto(segs, achados)
    resultado = re.sub(r"\x01(\d+)\x01", lambda m: guardados[int(m.group(1))], resultado)
    return resultado, achados


if __name__ == "__main__":
    modo, arquivos = sys.argv[1], sys.argv[2:]
    todos = []
    for a in arquivos:
        p = Path(a)
        novo, achados = marcar(p.read_text(encoding="utf-8"))
        todos += achados
        if modo == "--aplicar":
            p.write_text(novo, encoding="utf-8")
    unicos = list(dict.fromkeys(todos))
    if modo == "--listar":
        print(json.dumps(unicos, ensure_ascii=False, indent=0))
    print(f"{len(unicos)} textos em {len(arquivos)} arquivo(s)", file=sys.stderr)
