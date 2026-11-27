/* Swipe entre as páginas no celular e no tablet: arrastar para a esquerda vai para a próxima
   página do menu, para a direita volta para a anterior. A ordem é a do menu (painel do gestor)
   ou a das abas de baixo (app do funcionário). Não atrapalha tabelas e escalas que rolam para
   o lado, campos de texto, janelas abertas nem os gestos da borda da tela (voltar, gaveta). */
(function () {
  "use strict";
  if (!window.matchMedia || !matchMedia("(pointer: coarse)").matches) return;
  var menu = document.querySelector("[data-swipe]");
  if (!menu) return;
  var links = [].slice.call(menu.querySelectorAll(menu.dataset.swipe || "a"))
    .filter(function (a) { return a.getAttribute("href") && !a.hasAttribute("data-swipe-fora"); });
  var atual = links.findIndex(function (a) { return a.classList.contains("ativa"); });
  if (atual < 0 || links.length < 2) return;

  var BORDA = 28, MINIMO = 80, LIMITE_MS = 700;
  var ini = null, dica = null;

  function rotulo(a) {
    var r = a.getAttribute("data-rotulo") || (a.querySelector("span") || a).textContent;
    return (r || "").replace(/\s+/g, " ").trim();
  }
  function rolaDeLado(el) {   // algo entre o dedo e a página que rola na horizontal
    for (; el && el !== document.body; el = el.parentElement) {
      if (el.scrollWidth > el.clientWidth + 4) {
        var ox = getComputedStyle(el).overflowX;
        if (ox === "auto" || ox === "scroll") return true;
      }
    }
    return false;
  }
  function bloqueado(alvo) {
    if (document.querySelector("dialog[open]") || document.documentElement.classList.contains("gaveta-aberta")) return true;
    if (alvo.closest("input, textarea, select, [contenteditable], .foto-quadro, canvas, [data-sem-swipe]")) return true;
    var sel = window.getSelection && window.getSelection();
    if (sel && String(sel).length) return true;
    return rolaDeLado(alvo);
  }
  function mostrar(lado, alvoA, forca) {
    if (!dica) {
      dica = document.createElement("div");
      dica.className = "swipe-dica";
      dica.setAttribute("aria-hidden", "true");
      document.body.appendChild(dica);
    }
    dica.className = "swipe-dica " + lado + (forca >= 1 ? " pronta" : "");
    dica.innerHTML = (lado === "dir" ? "" : "<b>‹</b>") + "<span></span>" + (lado === "dir" ? "<b>›</b>" : "");
    dica.querySelector("span").textContent = rotulo(alvoA);
    dica.style.opacity = Math.min(1, forca);
    dica.style.setProperty("--desloca", (Math.min(1, forca) * 18) + "px");
  }
  function esconder() { if (dica) dica.style.opacity = 0; }

  document.addEventListener("touchstart", function (ev) {
    if (ev.touches.length !== 1) { ini = null; return; }
    var t = ev.touches[0];
    if (t.clientX < BORDA || t.clientX > innerWidth - BORDA || bloqueado(ev.target)) { ini = null; return; }
    ini = { x: t.clientX, y: t.clientY, quando: Date.now(), lado: null };
  }, { passive: true });

  document.addEventListener("touchmove", function (ev) {
    if (!ini) return;
    var t = ev.touches[0], dx = t.clientX - ini.x, dy = t.clientY - ini.y;
    if (Math.abs(dy) > Math.abs(dx) * 0.8 && Math.abs(dy) > 12) { ini = null; esconder(); return; }   // rolando a página
    var prox = dx < 0 ? links[atual + 1] : links[atual - 1];
    if (!prox || Math.abs(dx) < 16) { esconder(); return; }
    mostrar(dx < 0 ? "dir" : "esq", prox, Math.abs(dx) / MINIMO);
  }, { passive: true });

  document.addEventListener("touchend", function (ev) {
    if (!ini) return;
    var t = ev.changedTouches[0], dx = t.clientX - ini.x, dy = t.clientY - ini.y;
    var rapido = Date.now() - ini.quando < LIMITE_MS;
    ini = null;
    var prox = dx < 0 ? links[atual + 1] : links[atual - 1];
    if (prox && Math.abs(dx) >= MINIMO && Math.abs(dx) > Math.abs(dy) * 2 && rapido) {
      if (dica) dica.classList.add("indo");
      location.href = prox.href;
    } else {
      esconder();
    }
  }, { passive: true });
  document.addEventListener("touchcancel", function () { ini = null; esconder(); }, { passive: true });
  window.addEventListener("pageshow", esconder);
})();
