/* Recursos nativos do app do iPhone (Capacitor). Só roda dentro do app; no navegador não faz nada.
   - Face ID: depois de entrar com a senha, a pessoa pode guardar o acesso no Keychain do iPhone e,
     nas próximas vezes, entrar com o rosto.
   - Vibração (háptico) quando o ponto é registrado.
   - Ao excluir a conta ou sair, o acesso guardado é apagado. */
(function () {
  var C = window.Capacitor;
  if (!C || !C.isNativePlatform || !C.isNativePlatform()) return;
  var P = C.Plugins || {};
  var Bio = P.NativeBiometric, Hap = P.Haptics;
  var SERVIDOR = "dataworke.com.br";

  function vibrar(tipo) {
    try { if (Hap) Hap.notification({ type: tipo || "SUCCESS" }); } catch (e) {}
  }

  // ---- vibração ao registrar o ponto (app e totem)
  var fetchOriginal = window.fetch;
  window.fetch = function (url, opcoes) {
    return fetchOriginal.apply(this, arguments).then(function (r) {
      var u = typeof url === "string" ? url : (url && url.url) || "";
      if (/\/(app|totem)\/ponto$/.test(u.split("?")[0])) vibrar(r.ok ? "SUCCESS" : "ERROR");
      return r;
    });
  };

  if (!Bio) return;

  // ---- apagar o acesso guardado ao sair ou excluir a conta
  document.addEventListener("submit", function (ev) {
    var a = (ev.target.getAttribute("action") || "");
    if (a === "/conta/excluir") Bio.deleteCredentials({ server: SERVIDOR }).catch(function () {});
  }, true);

  // ---- tela de entrada
  var form = document.querySelector('form[action="/entrar"]');
  if (!form) return;
  var login = form.querySelector('[name="login"]'), senha = form.querySelector('[name="senha"]');
  if (!login || !senha) return;

  Bio.isAvailable().then(function (r) {
    if (!r || !r.isAvailable) return;
    var nome = r.biometryType === 2 ? "Touch ID" : "Face ID";

    // oferecer guardar o acesso
    var caixa = document.createElement("label");
    caixa.className = "marcar campo";
    caixa.innerHTML = '<input type="checkbox" id="usar-bio" checked> Entrar com ' + nome + " nas próximas vezes";
    var botao = form.querySelector('button[type="submit"]');
    form.insertBefore(caixa, botao);
    form.addEventListener("submit", function () {
      if (!document.getElementById("usar-bio").checked || !login.value || !senha.value) return;
      Bio.setCredentials({ username: login.value, password: senha.value, server: SERVIDOR }).catch(function () {});
    });

    // se já guardou, mostrar o botão de entrar com o rosto
    Bio.getCredentials({ server: SERVIDOR }).then(function (cred) {
      if (!cred || !cred.username) return;
      var b = document.createElement("button");
      b.type = "button"; b.className = "botao"; b.style.width = "100%"; b.style.marginBottom = "12px";
      b.textContent = "Entrar com " + nome;
      form.insertBefore(b, form.firstChild.nextSibling);
      function entrar() {
        Bio.verifyIdentity({ reason: "Entrar no Data Worke", title: "Data Worke" }).then(function () {
          login.value = cred.username; senha.value = cred.password;
          vibrar("SUCCESS");
          form.submit();
        }).catch(function () {});
      }
      b.addEventListener("click", entrar);
      if (!document.querySelector(".aviso.erro, .aviso-erro")) entrar();   // abre o Face ID direto
    }).catch(function () {});
  }).catch(function () {});
})();
