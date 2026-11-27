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
PUBLICAS = [("inicio", "/"), ("entrar", "/entrar"), ("cadastro", "/cadastro"), ("esqueci", "/esqueci"), ("portaria671", "/portaria-671")]
PAINEL = [("hoje", "/painel"), ("escala", "/painel/escala"), ("ponto", "/painel/ponto"),
          ("ajuste", "/painel/ponto/ajuste"), ("equipe", "/painel/equipe"), ("historico", "/painel/historico"), ("setores", "/painel/setores"), ("gerentes", "/painel/gerentes"), ("ficha", "/painel/equipe/5/ficha#documentos"),
          ("config", "/painel/config#logo"), ("relatorios", "/painel/relatorios"), ("pagamento", "/painel/pagamento"), ("folha", "/painel/folha?func=5"), ("fechamento", "/painel/fechamento"), ("horas", "/painel/fechamento/horas"), ("vendas", "/master/pagina/previa"),
          ("master", "/master"), ("app", "/app"), ("p671-painel", "/painel/portaria671"),
          ("p671-master", "/master/portaria671"), ("comprovantes", "/app/comprovantes"),
          ("conta", "/conta"), ("lojas", "/master/lojas"), ("crm", "/master/crm"), ("crm-lead", "/master/crm/2"),
          ("crm-equipe", "/master/equipe"), ("crm-aovivo", "/master/crm/ao-vivo"),
          ("caixa", "/master/caixa"), ("caixa-ler", "/master/caixa/2"), ("caixa-novo", "/master/caixa/novo?responder=1"),
          ("rastreamento", "/master/rastreamento"), ("canais", "/master/crm/canais")]
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
                      ("relatorios", "/painel/relatorios"), ("pagamento", "/painel/pagamento"), ("ficha", "/painel/equipe/2/ficha"),
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
        pag.evaluate("(document.querySelector('input[type=date]') || document.body).scrollIntoView({block: 'center'})")
        pag.wait_for_timeout(300)
        pag.screenshot(path=str(SAIDA / f"datas-iphone-{nome}.png"))
    (SAIDA / "datas-iphone.txt").write_text("\n".join(linhas))
    nav.close()


def teste_gratis(p):
    """Barra do teste grátis no menu lateral, nos dois temas."""
    import sqlite3
    from datetime import date, timedelta
    c = sqlite3.connect("dados/visualizar.db")
    c.execute("update empresas set isenta=0, pago_ate=null, teste_ate=?", ((date.today() + timedelta(days=5)).isoformat(),))
    c.commit()
    nav = p.chromium.launch()
    for tema in ("light", "dark"):
        ctx = nav.new_context(color_scheme=tema, locale="pt-BR", viewport={"width": 1440, "height": 1000})
        pag = ctx.new_page()
        pag.goto(BASE + "/entrar")
        pag.fill("input[name=login], input[type=email], input[type=text]", "demo@noponto.test")
        pag.fill("input[type=password]", "demo1234")
        pag.click("form button[type=submit]")
        pag.wait_for_load_state("networkidle")
        pag.goto(BASE + "/painel", wait_until="networkidle")
        pag.screenshot(path=str(SAIDA / f"teste-gratis-{'claro' if tema == 'light' else 'escuro'}.png"))
        ctx.close()
    nav.close()


def crescimento(p):
    """Planos novos, cupom, indique e ganhe e o programa de parceiros (muda a demonstração)."""
    import sqlite3
    from datetime import date, timedelta
    nav = p.chromium.launch()
    for tela, opc in TELAS.items():
        ctx = nav.new_context(color_scheme="light", locale="pt-BR", **opc)
        pag = ctx.new_page()
        pag.goto(BASE + "/entrar")
        pag.fill("input[name=login], input[type=email], input[type=text]", "demo@noponto.test")
        pag.fill("input[type=password]", "demo1234")
        pag.click("form button[type=submit]")
        pag.wait_for_load_state("networkidle")
        if tela == "pc":
            pag.goto(BASE + "/master/planos", wait_until="networkidle")
            pag.once("dialog", lambda d: d.accept())
            pag.click("form[action='/master/pacote-planos'] button")
            pag.wait_for_load_state("networkidle")
            c = sqlite3.connect("dados/visualizar.db")
            c.execute("update empresas set isenta=0, pago_ate=null, teste_ate=?", ((date.today() + timedelta(days=9)).isoformat(),))
            c.commit()
            pag.goto(BASE + "/painel/assinatura", wait_until="networkidle")
            pag.fill("#codigo", "FUNDADORES")
            pag.click("form[action='/painel/assinatura/cupom'] button")
            pag.wait_for_load_state("networkidle")
        for nome, url in (("assinatura", "/painel/assinatura"), ("master-planos", "/master/planos"),
                          ("master-cupons", "/master/cupons"), ("inicio-planos", "/#planos")):
            fotografar(pag, f"cresc-{nome}-{tela}", url)
        ctx.close()
        ctx = nav.new_context(color_scheme="light", locale="pt-BR", **opc)
        pag = ctx.new_page()
        fotografar(pag, f"cresc-parceiros-{tela}", "/parceiros")
        if tela == "pc":
            pag.fill("#nome", "Carla Souza")
            pag.fill("#escritorio", "Contábil Norte")
            pag.fill("#email", "carla@contabilnorte.test")
            pag.fill("#celular", "98999990000")
            pag.fill("#senha", "parceira123")
            pag.check("input[name=aceito]")
            pag.click("form[action='/parceiros'] button[type=submit]")
            pag.wait_for_load_state("networkidle")
        else:
            pag.goto(BASE + "/parceiros/entrar")
            pag.fill("#email", "carla@contabilnorte.test")
            pag.fill("#senha", "parceira123")
            pag.click("form button[type=submit]")
            pag.wait_for_load_state("networkidle")
        fotografar(pag, f"cresc-parceiro-painel-{tela}", "/parceiro")
        ctx.close()
    nav.close()


def totem(p):
    """Tela do totem num tablet deitado (claro e escuro): nomes, teclado do PIN e resultado."""
    nav = p.chromium.launch()
    ctx = nav.new_context(locale="pt-BR", viewport={"width": 1440, "height": 1000})
    pag = ctx.new_page()
    pag.goto(BASE + "/entrar")
    pag.fill("input[name=login], input[type=email], input[type=text]", "demo@noponto.test")
    pag.fill("input[type=password]", "demo1234")
    pag.click("form button[type=submit]")
    pag.wait_for_load_state("networkidle")
    pag.goto(BASE + "/painel/locais", wait_until="networkidle")
    if pag.query_selector("form[action='/painel/totens']"):
        pag.fill("form[action='/painel/totens'] input[name=nome]", "Tablet da entrada")
        pag.click("form[action='/painel/totens'] button[type=submit]")
    else:   # já tem totem: gera um endereço novo para ativar este navegador
        pag.once("dialog", lambda d: d.accept())
        pag.click("form[action^='/painel/totens/'] button[value=novo_link]")
    pag.wait_for_load_state("networkidle")
    link = pag.eval_on_selector("input[value*='/totem/ativar/']", "e => e.value")
    ctx.close()
    for tema in ("light", "dark"):
        suf = "claro" if tema == "light" else "escuro"
        ctx = nav.new_context(color_scheme=tema, locale="pt-BR", viewport={"width": 1280, "height": 800},
                              has_touch=True)
        pag = ctx.new_page()
        pag.goto(BASE + link[link.index("/totem/ativar/"):], wait_until="networkidle")
        pag.goto(BASE + "/totem", wait_until="networkidle")
        pag.wait_for_timeout(600)
        pag.screenshot(path=str(SAIDA / f"totem-{suf}.png"))
        pag.click(".totem-pessoa")
        for n in "48":
            pag.click(f".totem-teclado [data-n='{n}']")
        pag.wait_for_timeout(300)
        pag.screenshot(path=str(SAIDA / f"totem-pin-{suf}.png"))
        for n in "21":
            pag.click(f".totem-teclado [data-n='{n}']")
        pag.click("#pin-ok")
        pag.wait_for_timeout(1500)
        pag.screenshot(path=str(SAIDA / f"totem-resultado-{suf}.png"))
        ctx.close()
    cel = nav.new_context(locale="pt-BR", viewport={"width": 820, "height": 1180}, has_touch=True)
    pag = cel.new_page()
    pag.goto(BASE + link[link.index("/totem/ativar/"):], wait_until="networkidle")
    pag.goto(BASE + "/totem", wait_until="networkidle")
    pag.wait_for_timeout(600)
    pag.screenshot(path=str(SAIDA / "totem-em-pe.png"))
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
                if not so or "foto" in so:   # foto da pessoa: janela de enquadrar, depois salva
                    pag.goto(BASE + "/painel/equipe/5/ficha", wait_until="networkidle")
                    pag.set_input_files("[data-foto-arquivo]", "app/static/logo-nome.png")
                    pag.wait_for_selector("[data-foto-modal][open]")
                    pag.fill("[data-foto-zoom]", "1.6")
                    pag.dispatch_event("[data-foto-zoom]", "input")
                    caixa = pag.locator("[data-foto-quadro]").bounding_box()
                    pag.mouse.move(caixa["x"] + 100, caixa["y"] + 100)
                    pag.mouse.down()
                    pag.mouse.move(caixa["x"] + 60, caixa["y"] + 120, steps=5)
                    pag.mouse.up()
                    pag.wait_for_timeout(300)
                    pag.screenshot(path=str(SAIDA / f"foto-modal-{sufixo}.png"))
                    pag.click("[data-foto-salvar]")
                    pag.wait_for_load_state("networkidle")
                    pag.screenshot(path=str(SAIDA / f"foto-salva-{sufixo}.png"))
                for nome, url in PAINEL:
                    if not so or nome in so:
                        fotografar(pag, f"{nome}-{sufixo}", url)
                if tela == "cel" and tema == "light" and (not so or "swipe" in so):   # arrastar entre páginas
                    pag.goto(BASE + "/painel", wait_until="networkidle")
                    pag.evaluate("""() => {
                        const el = document.querySelector('h1');
                        const toque = (x, y) => new Touch({identifier: 1, target: el, clientX: x, clientY: y});
                        const ev = (tipo, x, y) => el.dispatchEvent(new TouchEvent(tipo, {bubbles: true, cancelable: true,
                            touches: tipo === 'touchend' ? [] : [toque(x, y)], changedTouches: [toque(x, y)]}));
                        ev('touchstart', 300, 300); ev('touchmove', 250, 302); ev('touchmove', 190, 304);
                        window.__fimSwipe = () => ev('touchend', 190, 304);
                    }""")
                    pag.wait_for_timeout(200)
                    pag.screenshot(path=str(SAIDA / "swipe-painel-meio.png"))
                    with pag.expect_navigation():
                        pag.evaluate("window.__fimSwipe()")
                    (SAIDA / "swipe.txt").write_text("depois do swipe para a esquerda em /painel: " + pag.url)
                if tela == "cel" and (not so or "gaveta" in so):   # menu em gaveta aberto
                    for nome, url in (("gaveta-painel", "/painel/ponto"), ("gaveta-master", "/master/crm")):
                        pag.goto(BASE + url, wait_until="networkidle")
                        pag.click("#abrir-gaveta")
                        pag.wait_for_timeout(500)
                        pag.screenshot(path=str(SAIDA / f"{nome}-{sufixo}.png"))
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
        if not so or "crescimento" in so:
            crescimento(p)
        if not so or "totem" in so:
            totem(p)
        if not so or "teste" in so:
            teste_gratis(p)  # por último: muda a empresa de demonstração
    print("capturas:", len(list(SAIDA.glob("*.png"))))


if __name__ == "__main__":
    main()
