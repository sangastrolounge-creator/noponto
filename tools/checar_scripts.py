"""Confere a sintaxe dos <script> escritos dentro dos templates (o node --check só vê os .js).

Troca as marcações do Jinja por valores neutros e roda `node --check` em cada bloco.
Uso (da raiz): python tools/checar_scripts.py   — sai com erro se algum bloco não compila.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "app" / "templates"
erros = 0
for arq in sorted(RAIZ.rglob("*.html")):
    texto = arq.read_text(encoding="utf-8")
    for n, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", texto, re.S)):
        corpo = re.sub(r"\{#.*?#\}", "", m.group(1), flags=re.S)
        corpo = re.sub(r"\{%.*?%\}", "", corpo, flags=re.S)
        corpo = re.sub(r"\{\{.*?\}\}", "1", corpo, flags=re.S)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(corpo)
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        if r.returncode:
            erros += 1
            linha = texto[:m.start()].count("\n") + 1
            print(f"ERRO {arq.relative_to(RAIZ.parent.parent)} (script que começa na linha {linha}):")
            print("\n".join(r.stderr.splitlines()[:5]))
print("scripts dos templates:", "com erro" if erros else "ok")
sys.exit(1 if erros else 0)
