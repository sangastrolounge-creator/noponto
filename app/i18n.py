"""Idiomas: português (original), inglês e espanhol.

O texto em português é a própria chave: _("Entrar") devolve "Sign in" em inglês. O que ainda
não tiver tradução aparece em português. As traduções ficam em app/idiomas/<cod>.json.
O idioma vem do cookie "idioma" (escolhido no seletor do topo) ou, na primeira visita, do
idioma do navegador.
"""
import contextvars
import json
import re
from pathlib import Path

IDIOMAS = {  # código -> (nome, sigla, atributo lang do HTML)
    "pt": ("Português", "PT", "pt-BR"),
    "en": ("English", "EN", "en"),
    "es": ("Español", "ES", "es"),
}
COOKIE = "idioma"
_PASTA = Path(__file__).parent / "idiomas"
_atual: contextvars.ContextVar[str] = contextvars.ContextVar("idioma", default="pt")
_caminho: contextvars.ContextVar[str] = contextvars.ContextVar("caminho", default="/")
_tabelas: dict[str, dict[str, str]] = {}
# Modelos com {v1}, {v2}...: reconhecem textos já montados ("Ana cadastrado." -> "{v1} cadastrado.").
_modelos: dict[str, list[tuple[re.Pattern, str]]] = {}
_JS: list[str] = []  # textos usados pelos scripts das telas (vão para a página como window.T)


def _regex(chave: str) -> re.Pattern:
    vistos, saida = set(), []
    for p in re.split(r"(\{v\d+\})", chave):
        if re.fullmatch(r"\{v\d+\}", p):
            nome = p[1:-1]
            saida.append("(?P=%s)" % nome if nome in vistos else "(?P<%s>.+?)" % nome)
            vistos.add(nome)
        else:
            saida.append(re.escape(p))
    return re.compile("".join(saida), re.S)


def carregar():
    global _JS
    for cod in IDIOMAS:
        arq = _PASTA / f"{cod}.json"
        _tabelas[cod] = json.loads(arq.read_text(encoding="utf-8")) if arq.is_file() else {}
        _modelos[cod] = [(_regex(k), v) for k, v in _tabelas[cod].items()
                         if "{v" in k and len(re.sub(r"\{v\d+\}", "", k).strip()) >= 4]
    arq = _PASTA / "_js.json"
    _JS = json.loads(arq.read_text(encoding="utf-8")) if arq.is_file() else []


carregar()


def escolher(cookie: str | None, aceita: str | None) -> str:
    """Idioma da pessoa: o que ela escolheu ou, se nunca escolheu, o do navegador."""
    if cookie in IDIOMAS:
        return cookie
    for parte in (aceita or "").split(","):
        cod = parte.split(";")[0].strip().lower()[:2]
        if cod in IDIOMAS:
            return cod
    return "pt"


def definir(cod: str, caminho: str = "/"):
    _atual.set(cod if cod in IDIOMAS else "pt")
    _caminho.set(caminho or "/")


def idioma() -> str:
    return _atual.get()


def caminho_atual() -> str:
    return _caminho.get()


def _(texto, **valores) -> str:
    """Traduz para o idioma atual. Valores entram nos {nomes} do texto. Um texto já montado
    (ex.: um aviso com o nome da pessoa) é reconhecido pelo modelo dele."""
    if not isinstance(texto, str):
        return texto
    cod = _atual.get()
    if cod != "pt":
        traduzido = _tabelas.get(cod, {}).get(texto)
        if traduzido:
            texto = traduzido
        elif not valores and texto:
            for rx, alvo in _modelos.get(cod, ()):
                m = rx.fullmatch(texto)
                if m:
                    return alvo.format(**{k: _(v) for k, v in m.groupdict().items()})
    return texto.format(**valores) if valores else texto


class no_idioma:
    """with no_idioma("en"): ...  -> traduz para esse idioma dentro do bloco."""

    def __init__(self, cod: str):
        self.cod, self.marca = cod, None

    def __enter__(self):
        self.marca = _atual.set(self.cod if self.cod in IDIOMAS else "pt")

    def __exit__(self, *erro):
        _atual.reset(self.marca)


def traduzir_tudo(o, pular=("imagem", "video", "foto", "nota")):
    """Traduz todos os textos de um conteúdo (dicionários e listas)."""
    if isinstance(o, str):
        return _(o)
    if isinstance(o, list):
        return [traduzir_tudo(v, pular) for v in o]
    if isinstance(o, dict):
        return {k: (v if k in pular else traduzir_tudo(v, pular)) for k, v in o.items()}
    return o


def textos_js() -> dict[str, str]:
    """Traduções dos textos dos scripts, para o idioma atual."""
    cod = _atual.get()
    tab = _tabelas.get(cod, {})
    return {k: tab[k] for k in _JS if k in tab} if cod != "pt" else {}


class Traduzido(dict):
    """Dicionário de rótulos (ex.: tipos de ausência) que devolve o rótulo já traduzido."""

    def __getitem__(self, k):
        return _(super().__getitem__(k))

    def get(self, k, padrao=None):
        return _(super().get(k, padrao))

    def items(self):
        return [(k, _(v)) for k, v in super().items()]

    def values(self):
        return [_(v) for v in super().values()]


def resposta_erro(dados: dict, status: int = 400):
    """JSONResponse que traduz a mensagem de erro mostrada ao usuário."""
    from fastapi.responses import JSONResponse
    if isinstance(dados.get("erro"), str):
        dados = {**dados, "erro": _(dados["erro"])}
    return JSONResponse(dados, status)


class _Dias:
    """DIAS[0] -> "Seg" / "Mon" / "Lun", conforme o idioma."""
    NOMES = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

    def __getitem__(self, i):
        return _(self.NOMES[i])

    def __iter__(self):
        return (self[i] for i in range(7))

    def __len__(self):
        return 7


DIAS = _Dias()
