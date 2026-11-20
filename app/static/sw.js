// Service worker: deixa o app instalável, mostra a tela "sem internet" e recebe avisos.
const CACHE = "turno-v22";
const FIXOS = ["/offline", "/static/style.css?v=3", "/static/icone.svg", "/static/icone-192.png"];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(FIXOS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(caches.keys()
    .then((nomes) => Promise.all(nomes.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (ev) => {
  const req = ev.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    ev.respondWith(caches.match(req).then((r) => r || fetch(req).then((resp) => {
      const copia = resp.clone();
      caches.open(CACHE).then((c) => c.put(req, copia));
      return resp;
    })));
    return;
  }
  // Páginas: sempre da rede (os dados mudam a toda hora); sem rede, aviso.
  if (req.mode === "navigate") {
    ev.respondWith(fetch(req).catch(() => caches.match("/offline")));
  }
});

self.addEventListener("push", (ev) => {
  let d = {};
  try { d = ev.data.json(); } catch (e) { d = { titulo: "Aviso", texto: ev.data ? ev.data.text() : "" }; }
  ev.waitUntil(self.registration.showNotification(d.titulo || "Aviso", {
    body: d.texto || "", icon: "/static/icone-192.png?v=3", badge: "/static/icone-192.png",
    data: { url: d.url || "/painel" },
  }));
});

self.addEventListener("notificationclick", (ev) => {
  ev.notification.close();
  const destino = ev.notification.data && ev.notification.data.url || "/painel";
  ev.waitUntil(self.clients.matchAll({ type: "window" }).then((abas) => {
    for (const a of abas) { if ("focus" in a) { a.navigate(destino); return a.focus(); } }
    return self.clients.openWindow(destino);
  }));
});
