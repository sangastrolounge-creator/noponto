"""Fotografa as telas principais (computador e celular, claro e escuro) para revisar o visual.

Roda no GitHub (workflow "Capturas de tela") com a empresa de demonstração já criada
e o servidor ligado em http://127.0.0.1:8100. As imagens vão para a pasta capturas/.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8100"
SAIDA = Path("capturas")
PUBLICAS = [("inicio", "/"), ("entrar", "/entrar"), ("cadastro", "/cadastro")]
PAINEL = [("hoje", "/painel"), ("escala", "/painel/escala"), ("ponto", "/painel/ponto"),
          ("ajuste", "/painel/ponto/ajuste"), ("equipe", "/painel/equipe"),
          ("config", "/painel/config"), ("relatorios", "/painel/relatorios"), ("vendas", "/master/pagina/previa"),
          ("master", "/master"), ("app", "/app")]
TELAS = {"pc": dict(viewport={"width": 1440, "height": 1000}),
         "cel": dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                     device_scale_factor=2)}
so = set(sys.argv[1:])


def fotografar(pag, nome, url):
    pag.goto(BASE + url, wait_until="networkidle")
    pag.wait_for_timeout(700)
    pag.screenshot(path=str(SAIDA / f"{nome}.png"), full_page=True)


def main():
    SAIDA.mkdir(exist_ok=True)
    with sync_playwright() as p:
        nav = p.chromium.launch()
        for tela, opc in TELAS.items():
            for tema in ("light", "dark"):
                ctx = nav.new_context(color_scheme=tema, locale="pt-BR", **opc)
                pag = ctx.new_page()
                sufixo = f"{tela}-{'claro' if tema == 'light' else 'escuro'}"
                for nome, url in PUBLICAS:
                    if not so or nome in so:
                        fotografar(pag, f"{nome}-{sufixo}", url)
                pag.goto(BASE + "/entrar")
                pag.fill("input[name=login], input[type=email], input[type=text]", "demo@noponto.test")
                pag.fill("input[type=password]", "demo1234")
                pag.click("form button[type=submit]")
                pag.wait_for_load_state("networkidle")
                for nome, url in PAINEL:
                    if not so or nome in so:
                        fotografar(pag, f"{nome}-{sufixo}", url)
                if not so or "desligamento" in so:
                    pag.goto(BASE + "/painel/equipe", wait_until="networkidle")
                    pag.click("[data-desligamento]")
                    pag.wait_for_timeout(600)
                    pag.screenshot(path=str(SAIDA / f"desligamento-{sufixo}.png"))
                    pag.click("dialog[open] [data-passo=excluir]")
                    pag.wait_for_timeout(300)
                    pag.screenshot(path=str(SAIDA / f"desligamento-excluir-{sufixo}.png"))
                ctx.close()
        nav.close()
    print("capturas:", len(list(SAIDA.glob("*.png"))))


if __name__ == "__main__":
    main()
