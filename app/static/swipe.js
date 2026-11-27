/* Swipe entre as páginas no celular e no tablet, no estilo do Instagram: a página acompanha o
   dedo. Arrastar para a direita volta para a página vista antes (como o "voltar" do navegador);
   arrastar para a esquerda avança de novo, se a pessoa tiver voltado. A página que vai aparecer
   é mostrada como esqueleto (título e linhas cinzas) até carregar.
   Cada aba guarda a sua lista de páginas visitadas (sessionStorage), na mesma ordem do
   histórico do navegador. O gesto só vale no painel e no app (onde existe [data-swipe]).
   Não atrapalha tabelas e escalas que rolam para o lado, campos, janelas abertas nem os gestos
   da borda da tela (voltar do iPhone, gaveta do menu). */
(function () {
  "use strict";
  var CHAVE = "dw-paginas", MAX = 40;

  // ---- lista das páginas visitadas nesta aba (acompanha o voltar/avançar do navegador)
  function ler() {
    try { var v = JSON.parse(sessionStorage.getItem(CHAVE)); if (v && v.lista) return v; } catch (e) {}
    return { lista: [], pos: -1 };
  }
  function gravar(v) { try { sessionStorage.setItem(CHAVE, JSON.stringify(v)); } catch (e) {} }
  function titulo() {
    var h = document.querySelector("main h1");
    var t = (h && h.textContent) || document.title.split(" — ")[0];
    return t.replace(/\s+/g, " ").trim().slice(0, 60);
  }
  function aqui() { return location.pathname + location.search; }
  function registrar(tipo) {
    var v = ler(), u = aqui(), t = titulo();
    if (tipo === "back_forward") {          // achar a entrada mais perto da posição anterior
      if (v.lista[v.pos - 1] && v.lista[v.pos - 1].u === u) v.pos -= 1;
      else if (v.lista[v.pos + 1] && v.lista[v.pos + 1].u === u) v.pos += 1;
      else if (!(v.lista[v.pos] && v.lista[v.pos].u === u)) {
        var achou = -1;
        for (var k = v.lista.length - 1; k >= 0; k--) if (v.lista[k].u === u) { achou = k; break; }
        if (achou >= 0) v.pos = achou; else { v.lista = v.lista.slice(0, v.pos + 1); v.lista.push({ u: u, t: t }); v.pos = v.lista.length - 1; }
      }
    } else if (tipo === "reload" && v.lista[v.pos] && v.lista[v.pos].u === u) {
      // recarregou: continua no mesmo lugar
    } else {                                // página nova: corta o "avançar" e entra no fim
      if (!(v.lista[v.pos] && v.lista[v.pos].u === u)) {
        v.lista = v.lista.slice(0, v.pos + 1);
        v.lista.push({ u: u, t: t });
        if (v.lista.length > MAX) v.lista = v.lista.slice(-MAX);
        v.pos = v.lista.length - 1;
      }
    }
    if (v.lista[v.pos]) v.lista[v.pos].t = t;
    gravar(v);
    return v;
  }
  var nav = (performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || {};
  var hist = registrar(nav.type || "navigate");
  window.addEventListener("pageshow", function (ev) {   // voltou pelo histórico sem recarregar a página
    if (ev.persisted) hist = registrar("back_forward");
  });

  if (!window.matchMedia || !matchMedia("(pointer: coarse)").matches) return;
  var conteudo = document.querySelector("main");
  if (!document.querySelector("[data-swipe]") || !conteudo) return;
  function vizinha(frente) {   // {t, ir} da página para onde o gesto leva, ou null
    var e = hist.lista[hist.pos + (frente ? 1 : -1)];
    if (!e) return null;
    return { t: e.t, href: e.u, ir: function () { frente ? history.forward() : history.back(); } };
  }

  var BORDA = 28, MINIMO = 0.28, RAPIDO = 0.45;   // 28% da largura, ou um peteleco a 0,45 px/ms
  var calmo = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ini = null, painel = null, carregadas = {};

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
  function adiantar(a) {   // baixa a página vizinha enquanto o dedo ainda está na tela
    if (!a || carregadas[a.href]) return;
    carregadas[a.href] = 1;
    var l = document.createElement("link");
    l.rel = "prefetch"; l.href = a.href;
    document.head.appendChild(l);
  }
  function montar(prox, frente) {
    var topo = document.querySelector(".topo");
    var alto = topo ? Math.max(0, topo.getBoundingClientRect().bottom) : 0;
    painel = document.createElement("div");
    painel.className = "swipe-pagina " + (frente ? "frente" : "tras");
    painel.setAttribute("aria-hidden", "true");
    painel.style.top = alto + "px";
    painel.innerHTML = '<div class="swipe-esqueleto"><h1></h1><i></i><i></i><b></b><b></b><b></b><b></b><b></b></div>';
    painel.querySelector("h1").textContent = prox.t;
    document.body.appendChild(painel);
    document.documentElement.classList.add("swipe-ativo", frente ? "swipe-frente" : "swipe-tras");
    ini.prox = prox; ini.frente = frente;
  }
  function posicionar(p, animar) {   // p: 0 (parado) a 1 (página nova inteira na tela)
    var L = innerWidth;
    var t = animar ? "transform .26s cubic-bezier(.2,.8,.2,1), opacity .26s" : "none";
    conteudo.style.transition = painel.style.transition = t;
    if (ini.frente) {   // a nova vem por cima, da direita; a atual recua 30%
      painel.style.transform = "translate3d(" + (L * (1 - p)) + "px,0,0)";
      conteudo.style.transform = "translate3d(" + (-L * 0.3 * p) + "px,0,0)";
      conteudo.style.opacity = 1 - 0.35 * p;
    } else {            // a atual sai para a direita e revela a anterior por baixo
      conteudo.style.transform = "translate3d(" + (L * p) + "px,0,0)";
      painel.style.transform = "translate3d(" + (-L * 0.3 * (1 - p)) + "px,0,0)";
      painel.style.opacity = 0.65 + 0.35 * p;
    }
  }
  function limpar() {
    document.documentElement.classList.remove("swipe-ativo", "swipe-frente", "swipe-tras");
    conteudo.style.transition = conteudo.style.transform = conteudo.style.opacity = "";
    if (painel) painel.remove();
    painel = null; ini = null;
  }

  document.addEventListener("touchstart", function (ev) {
    if (painel || ev.touches.length !== 1) { ini = null; return; }
    var t = ev.touches[0];
    if (t.clientX < BORDA || t.clientX > innerWidth - BORDA || bloqueado(ev.target)) { ini = null; return; }
    ini = { x: t.clientX, y: t.clientY, ultX: t.clientX, ultQ: Date.now(), vel: 0 };
    adiantar(vizinha(true)); adiantar(vizinha(false));
  }, { passive: true });

  document.addEventListener("touchmove", function (ev) {
    if (!ini) return;
    var t = ev.touches[0], dx = t.clientX - ini.x, dy = t.clientY - ini.y, agora = Date.now();
    if (!ini.prox) {
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { ini = null; return; }   // está rolando a página
      if (Math.abs(dx) < 14) return;
      var prox = vizinha(dx < 0);
      if (!prox) { ini = null; return; }
      montar(prox, dx < 0);
    }
    if (ev.cancelable) ev.preventDefault();   // daqui em diante o gesto é nosso, não da rolagem
    ini.vel = (t.clientX - ini.ultX) / Math.max(1, agora - ini.ultQ);
    ini.ultX = t.clientX; ini.ultQ = agora;
    var p = ini.frente ? -dx / innerWidth : dx / innerWidth;
    posicionar(Math.max(0, Math.min(1, p)), false);
  }, { passive: false });

  function soltar(ev) {
    if (!ini || !painel) { ini = null; return; }
    var t = ev.changedTouches[0], dx = t.clientX - ini.x;
    var p = ini.frente ? -dx / innerWidth : dx / innerWidth;
    var peteleco = (ini.frente ? -ini.vel : ini.vel) > RAPIDO;
    var vai = ev.type === "touchend" && (p > MINIMO || (peteleco && p > 0.08));
    var destino = ini.prox;
    posicionar(vai ? 1 : 0, !calmo);
    if (vai) {
      setTimeout(function () { destino.ir(); }, calmo ? 0 : 230);
    } else {
      setTimeout(limpar, calmo ? 0 : 270);
    }
  }
  document.addEventListener("touchend", soltar, { passive: true });
  document.addEventListener("touchcancel", soltar, { passive: true });
  window.addEventListener("pageshow", function (ev) { if (ev.persisted) limpar(); });
})();
