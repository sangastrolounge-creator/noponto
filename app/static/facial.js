/* Reconhecimento facial no celular (Data Worke Brasil).
   A foto do rosto não sai do aparelho: a biblioteca (face-api, MIT, servida pelo próprio site)
   transforma o rosto em 128 números, e só esses números vão para o servidor.
   Prova de vida simples: a pessoa precisa piscar diante da câmera. */
(function () {
  var t = function (s) { return window.t ? window.t(s) : s; };
  var api = null, carregando = null;
  var BASE = "/static/vendor/face";

  function erro(msg, codigo) { var e = new Error(msg); e.codigo = codigo || ""; return e; }

  function carregar() {
    if (carregando) return carregando;
    carregando = import(BASE + "/face-api.esm.js").then(function (m) {
      api = m;
      return Promise.all([
        m.nets.tinyFaceDetector.loadFromUri(BASE + "/model"),
        m.nets.faceLandmark68TinyNet.loadFromUri(BASE + "/model"),
        m.nets.faceRecognitionNet.loadFromUri(BASE + "/model")
      ]);
    }).catch(function () { carregando = null; throw erro(t("Este aparelho não conseguiu carregar o reconhecimento facial."), "suporte"); });
    return carregando;
  }

  // Abertura do olho (EAR): fica baixa quando a pessoa pisca.
  function abertura(olho) {
    function d(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
    return (d(olho[1], olho[5]) + d(olho[2], olho[4])) / (2 * d(olho[0], olho[3]) || 1);
  }

  function janela() {
    var dlg = document.createElement("dialog");
    dlg.className = "facial-dlg";
    dlg.innerHTML =
      '<div class="facial-quadro"><video playsinline muted autoplay></video><div class="facial-oval"></div></div>' +
      '<p class="facial-msg"></p><div class="facial-barra"><span></span></div>' +
      '<button type="button" class="botao claro facial-cancelar"></button>';
    dlg.querySelector(".facial-cancelar").textContent = t("Cancelar");
    document.body.appendChild(dlg);
    return dlg;
  }

  /* Abre a câmera, espera um rosto, pede uma piscada e devolve {descritor, foto}.
     opcoes.foto = true devolve também uma foto pequena (para quem já exige foto no ponto). */
  function capturar(opcoes) {
    opcoes = opcoes || {};
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.HTMLDialogElement)
      return Promise.reject(erro(t("Este navegador não permite usar a câmera."), "suporte"));
    var dlg = janela(), video = dlg.querySelector("video"), msg = dlg.querySelector(".facial-msg"),
        barra = dlg.querySelector(".facial-barra span"), fluxo = null, parar = false;
    function dizer(s) { msg.textContent = s; }
    function fechar() {
      parar = true;
      if (fluxo) fluxo.getTracks().forEach(function (t) { t.stop(); });
      if (dlg.open) dlg.close();
      dlg.remove();
    }
    dlg.showModal();
    dizer(t("Preparando a câmera…"));
    return new Promise(function (ok, falha) {
      dlg.querySelector(".facial-cancelar").onclick = function () { fechar(); falha(erro("", "cancelado")); };
      dlg.oncancel = function (ev) { ev.preventDefault(); fechar(); falha(erro("", "cancelado")); };
      Promise.all([
        navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 640 } }, audio: false }),
        carregar()
      ]).then(function (r) {
        fluxo = r[0];
        video.muted = true; video.setAttribute("muted", ""); video.setAttribute("playsinline", "");
        video.srcObject = fluxo;
        var tocar = video.play && video.play();
        if (tocar && tocar.catch) tocar.catch(function () {});
        return new Promise(function (pronto, falhou) {
          var feito = false;
          function ok() { if (!feito && video.videoWidth) { feito = true; pronto(); } }
          video.onloadeddata = ok; video.onplaying = ok;
          var vezes = 0, relogio = setInterval(function () {
            ok(); if (feito || parar) clearInterval(relogio);
            else if (++vezes > 50) { clearInterval(relogio); falhou(erro(t("A câmera não começou a mostrar a imagem. Feche outros apps que usam a câmera e tente de novo."))); }
          }, 100);
        });
      }).then(function () {
        var opc = new api.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.4 });
        var inicio = Date.now(), maior = 0, fechou = false, piscou = false, descritores = [], foto = null;
        var errosSeguidos = 0, ultimoErro = "", tela = document.createElement("canvas");
        dizer(t("Olhe para a câmera e pisque devagar."));
        // O iPhone lê melhor um quadro desenhado num canvas do que o vídeo direto.
        function capturarQuadro() {
          var esc = Math.min(1, 360 / Math.max(video.videoWidth, video.videoHeight));
          tela.width = Math.round(video.videoWidth * esc); tela.height = Math.round(video.videoHeight * esc);
          tela.getContext("2d").drawImage(video, 0, 0, tela.width, tela.height);
          return tela;
        }
        function comPrazo(p, ms) {
          return Promise.race([p, new Promise(function (_, n) { setTimeout(function () { n(new Error("tempo esgotado")); }, ms); })]);
        }
        function falhouLeitura(e) {
          errosSeguidos++; ultimoErro = (e && e.message) || String(e);
          if (errosSeguidos === 2 && api.tf && api.tf.getBackend && api.tf.getBackend() !== "cpu") {
            dizer(t("Ajustando o reconhecimento para este aparelho…"));
            return api.tf.setBackend("cpu").then(function () { return api.tf.ready(); }).catch(function () {});
          }
          if (errosSeguidos >= 6) {
            fechar();
            falha(erro(t("O reconhecimento facial não funcionou neste aparelho.") + " (" + ultimoErro.slice(0, 80) + ")", "suporte"));
            parar = true;
          }
        }
        function passo() {
          if (parar) return;
          if (Date.now() - inicio > 25000) {
            fechar();
            falha(erro(piscou ? t("Não consegui ler o seu rosto. Tente num lugar mais claro.")
                              : t("Não percebi a piscada. Tente de novo, num lugar claro.")));
            return;
          }
          var img = capturarQuadro();
          var precisa = descritores.length < 3;
          var tarefa = api.detectAllFaces(img, opc).withFaceLandmarks(true);
          if (precisa) tarefa = tarefa.withFaceDescriptors();
          comPrazo(tarefa, 8000).then(function (rostos) {
            errosSeguidos = 0;
            if (parar) return;
            if (rostos.length > 1) { dizer(t("Só uma pessoa na frente da câmera, por favor.")); return; }
            if (!rostos.length) { dizer(t("Não estou vendo o rosto. Fique de frente para a câmera.")); return; }
            var r = rostos[0], pts = r.landmarks;
            var ear = (abertura(pts.getLeftEye()) + abertura(pts.getRightEye())) / 2;
            if (ear > maior) maior = ear;
            if (!piscou) {
              dizer(fechou ? t("Isso! Agora abra os olhos.") : t("Agora pisque devagar."));
              if (maior > 0 && ear < maior * 0.75) fechou = true;
              else if (fechou && ear > maior * 0.85) piscou = true;
            }
            if (precisa && r.descriptor && ear > maior * 0.85) {     // olhos abertos: bom quadro para o modelo
              descritores.push(Array.from(r.descriptor));
              if (opcoes.foto && !foto) foto = quadro(video);
            }
            barra.style.width = Math.min(100, (piscou ? 50 : 0) + Math.min(descritores.length, 3) * 17) + "%";
            if (piscou && descritores.length >= 3) {
              var media = descritores.slice(-3).reduce(function (s, d) { return s.map(function (x, i) { return x + d[i] / 3; }); },
                                                       new Array(128).fill(0));
              dizer(t("Rosto conferido."));
              fechar();
              ok({ descritor: media, foto: foto });
            }
          }, falhouLeitura).catch(falhouLeitura).then(function () { if (!parar) setTimeout(passo, 60); });
        }
        passo();
      }).catch(function (e) {
        fechar();
        if (e && e.name === "NotAllowedError")
          falha(erro(t("Você precisa permitir o uso da câmera. Libere nas configurações do navegador e tente de novo.")));
        else falha(e && e.codigo ? e : erro(t("Não consegui abrir a câmera."), "suporte"));
      });
    });
  }

  function quadro(video) {
    var escala = Math.min(1, 480 / Math.max(video.videoWidth, video.videoHeight)), c = document.createElement("canvas");
    c.width = Math.round(video.videoWidth * escala); c.height = Math.round(video.videoHeight * escala);
    c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
    return c.toDataURL("image/jpeg", 0.7);
  }

  window.Facial = { capturar: capturar, carregar: carregar };
})();
