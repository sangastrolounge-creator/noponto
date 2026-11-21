"""Fotografa as telas principais (computador e celular, claro e escuro) para revisar o visual.

Roda no GitHub (workflow "Capturas de tela") com a empresa de demonstração já criada
e o servidor ligado em http://127.0.0.1:8100. As imagens vão para a pasta capturas/.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8100"
SAIDA = Path("capturas")
PUBLICAS = [("inicio", "/"), ("entrar", "/entrar"), ("cadastro", "/cadastro")]
PAINEL = [("hoje", "/painel"), ("escala", "/painel/escala"), ("ponto", "/painel/ponto"),
          ("ajuste", "/painel/ponto/ajuste"), ("equipe", "/painel/equipe"), ("historico", "/painel/historico"),
          ("config", "/painel/config#logo"), ("relatorios", "/painel/relatorios"), ("folha", "/painel/folha?func=5"), ("fechamento", "/painel/fechamento"), ("horas", "/painel/fechamento/horas"), ("vendas", "/master/pagina/previa"),
          ("master", "/master"), ("app", "/app")]
TELAS = {"pc": dict(viewport={"width": 1440, "height": 1000}),
         "cel": dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                     device_scale_factor=2)}
so = set(sys.argv[1:])


def fotografar(pag, nome, url):
    pag.goto(BASE + url, wait_until="networkidle")
    pag.wait_for_timeout(700)
    pag.screenshot(path=str(SAIDA / f"{nome}.png"), full_page=True)


def camera_falsa(p):
    """Roda o reconhecimento com a câmera falsa do Chromium (sem rosto): confere que o laço
    de leitura funciona sem erro e mede o tempo de cada leitura."""
    nav = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
    ctx = nav.new_context(permissions=["camera"], viewport={"width": 390, "height": 844})
    pag = ctx.new_page()
    log = []
    pag.on("console", lambda m: log.append(m.text))
    pag.goto(BASE + "/entrar")
    pag.evaluate("""() => new Promise(ok => { const s = document.createElement('script');
                       s.src = '/static/facial.js'; s.onload = ok; document.head.appendChild(s); })""")
    res = pag.evaluate("""async () => {
        const msgs = []; const t0 = Date.now();
        const obs = new MutationObserver(() => { const m = document.querySelector('.facial-msg');
            if (m && msgs[msgs.length - 1] !== m.textContent) msgs.push(((Date.now() - t0) / 1000).toFixed(1) + 's ' + m.textContent); });
        obs.observe(document.body, { subtree: true, childList: true, characterData: true });
        const fim = window.Facial.capturar({}).then(r => 'ok', e => 'falhou: ' + e.message);
        const r = await Promise.race([fim, new Promise(ok => setTimeout(() => ok('ainda rodando'), 15000))]);
        return { resultado: r, mensagens: msgs };
    }""")
    (SAIDA / "facial-camera.txt").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n" + "\n".join(log[-20:]))
    pag.screenshot(path=str(SAIDA / "facial-camera.png"))
    nav.close()


def datas_iphone(p):
    """Safari (WebKit) num iPhone: nenhum campo de data pode passar da borda do bloco."""
    nav = p.webkit.launch()
    ctx = nav.new_context(**p.devices["iPhone 13"], locale="pt-BR")
    pag = ctx.new_page()
    pag.goto(BASE + "/entrar")
    pag.fill("input[name=login], input[type=email], input[type=text]", "demo@noponto.test")
    pag.fill("input[type=password]", "demo1234")
    pag.click("form button[type=submit]")
    pag.wait_for_load_state("networkidle")
    linhas = []
    for nome, url in (("ponto", "/painel/ponto"), ("ausencias", "/painel/ausencias"),
                      ("relatorios", "/painel/relatorios"), ("ficha", "/painel/equipe/2/ficha"),
                      ("apropriacao", "/painel/ponto/ajuste"), ("ferias", "/painel/ferias")):
        pag.goto(BASE + url, wait_until="networkidle")
        pag.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        vaza = pag.evaluate("""() => [...document.querySelectorAll('input[type=date],input[type=time]')]
            .filter(i => i.offsetParent).map(i => {
              const r = i.getBoundingClientRect();
              const c = (i.closest('.cartao, dialog, form') || document.body).getBoundingClientRect();
              return {id: i.id || i.name, sobra: Math.round(c.right - r.right), larg: Math.round(r.width),
                      tela: Math.round(window.innerWidth - r.right)};
            }).filter(x => x.sobra < 0 || x.tela < 0)""")
        linhas.append(f"{nome}: {'ok' if not vaza else vaza}")
        pag.screenshot(path=str(SAIDA / f"datas-iphone-{nome}.png"), full_page=True)
    (SAIDA / "datas-iphone.txt").write_text("\n".join(linhas))
    nav.close()


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
                pag.goto(BASE + "/painel/config", wait_until="networkidle")   # logo de exemplo nos impressos
                pag.set_input_files("#logo-arquivo", "app/static/icone-512.png")
                pag.click("#logo button[type=submit]:not([name])")
                pag.wait_for_load_state("networkidle")
                for nome, url in PAINEL:
                    if not so or nome in so:
                        fotografar(pag, f"{nome}-{sufixo}", url)
                if tela == "pc" and tema == "light" and (not so or "facial" in so):
                    pag.goto(BASE + "/painel/config", wait_until="networkidle")
                    pag.check("#facial input[name=ativo]")
                    pag.click("#facial button[type=submit]")
                    pag.wait_for_load_state("networkidle")
                    pag.goto(BASE + "/painel/config#facial", wait_until="networkidle")
                    pag.screenshot(path=str(SAIDA / "facial-config.png"))
                    pag.goto(BASE + "/painel/config/termo-facial", wait_until="networkidle")
                    pag.screenshot(path=str(SAIDA / "facial-termo.png"), full_page=True)
                    pag.add_script_tag(url="/static/facial.js")
                    res = pag.evaluate("""() => window.Facial.carregar().then(() => 'modelos carregados')
                                                 .catch(e => 'erro: ' + e.message)""")
                    (SAIDA / "facial.txt").write_text(res)
                if tela == "pc" and tema == "light" and (not so or "rolagem" in so):
                    pag.set_viewport_size({"width": 1440, "height": 900})
                    pag.goto(BASE + "/painel/ponto/ajuste", wait_until="networkidle")
                    pag.evaluate("document.body.scrollTop = 1500; window.scrollTo(0, 1500)")
                    antes = pag.evaluate("window.scrollY || document.body.scrollTop")
                    pag.once("dialog", lambda d: d.accept())
                    with pag.expect_navigation():
                        pag.click("button.esp-x")
                    pag.wait_for_timeout(800)
                    depois = pag.evaluate("window.scrollY || document.body.scrollTop")
                    (SAIDA / "rolagem.txt").write_text(f"antes={antes} depois={depois} url={pag.url}")
                    pag.set_viewport_size(opc["viewport"])
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
        if not so or "facial" in so:
            camera_falsa(p)
        if not so or "datas" in so:
            datas_iphone(p)
    print("capturas:", len(list(SAIDA.glob("*.png"))))


if __name__ == "__main__":
    main()
