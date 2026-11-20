/* Guia do gestor: destaca um pedaço da tela por vez e explica para que serve.
   Cada tela do painel tem o seu guia (botão t("Guia")); o guia de primeiros passos
   percorre as telas principais em sequência. Passo cujo alvo não está na tela é pulado.
   Alvo: seletor CSS, ou "h2:Texto" para o cartão cujo título começa com esse texto. */
(function () {
  "use strict";
  var CFG = window.TOUR || {};
  var CHAVE = "turno_guia";  // sessionStorage: "1" enquanto o guia de primeiros passos está em andamento
  var SEQ = ["hoje", "config", "equipe", "escala", "ponto", "fechamento", "pagamento"];
  var TELAS = {
    hoje: { url: "/painel", nome: t("Hoje") }, config: { url: "/painel/config", nome: t("Configurações") },
    equipe: { url: "/painel/equipe", nome: t("Equipe") }, escala: { url: "/painel/escala", nome: t("Escala") },
    ponto: { url: "/painel/ponto", nome: t("Ponto") }, fechamento: { url: "/painel/fechamento", nome: t("Fechamento") },
    pagamento: { url: "/painel/pagamento", nome: t("Pagamento") }
  };

  var PASSOS = {
    hoje: [
      { t: t("Bem-vindo ao painel"), x: t("Esta é a tela Hoje: mostra, ao vivo, quem está trabalhando, quem atrasou e quem não apareceu. Ela se atualiza sozinha a cada minuto.") },
      { a: ".abas", t: t("O menu"), x: t("O menu é dividido em Dia a dia, Pagamento e Empresa. Onde há uma setinha, ela abre um submenu com as páginas daquela parte; ao abrir um, o outro se fecha. No celular, o menu fica na barra de baixo, só com os ícones.") },
      { a: ".menu-selo", t: t("Contadores"), x: t("O número laranja ao lado de um item mostra o que espera por você: problemas de ponto, pedidos de ajuste, pedidos de folga ou de turno.") },
      { a: "#recolher-lateral", t: t("Recolher o menu"), x: t("Este botão deixa o menu estreito, só com os ícones, para sobrar mais espaço na tela. Toque de novo para abrir.") },
      { a: ".contagem-teste", t: t("Seu teste grátis"), x: t("A barra mostra quantos dias de teste ainda restam. Ela fica amarela nos últimos dias. Toque nela para ver os planos e assinar.") },
      { a: ".grade", t: t("Os números do dia"), x: t("Quantos estão escalados, quantos estão trabalhando agora, os atrasos, quem ainda não bateu o ponto e, se você vê os valores, o custo da equipe hoje e quanto ele representa das vendas.") },
      { a: "h2:Equipe de hoje", t: t("Equipe de hoje"), x: t("Cada pessoa escalada, com a hora em que entrou e saiu e a situação. Para quem não bateu o ponto aparece um atalho para chamar no WhatsApp.") },
      { a: "h2:Avisos", t: t("Avisos"), x: t("Atraso, ponto não batido, tentativa de bater fora da empresa, intervalo não feito e pedidos da equipe aparecem aqui.") },
      { a: "#ativar-avisos", t: t("Avisos no seu celular"), x: t("Toque aqui para receber os avisos como notificação neste aparelho, mesmo com o sistema fechado.") },
      { a: "h2:Checklist de hoje", t: t("Checklist do dia"), x: t("As tarefas diárias da equipe e quem já fez cada uma. Você cria as tarefas na tela Tarefas.") },
      { a: "#vendas", t: t("Vendas e custo da equipe"), x: t("No fim do dia, digite o total do caixa. O sistema mostra quanto a equipe custou e a porcentagem das vendas. Em restaurantes, o normal fica entre 25% e 35%.") },
      { a: ".conta-pessoa", t: t("Sua conta"), x: t("Seu nome e a empresa. Logo abaixo ficam o tema claro ou escuro, o idioma e os ícones de Master, Guia e Sair.") }
    ],
    config: [
      { t: t("Configurações"), x: t("Aqui você diz onde fica a empresa e define as regras do ponto. É o primeiro passo: sem o local, o ponto é aceito de qualquer lugar.") },
      { a: "#usar-local", t: t("Onde fica a empresa"), x: t("Estando dentro da empresa, de preferência pelo celular, toque neste botão. A localização é preenchida sozinha.") },
      { a: "#raio_m", t: t("Raio aceito"), x: t("A distância máxima da empresa para o ponto valer. 150 metros cobrem a imprecisão normal do GPS.") },
      { a: "h2:Confiança no ponto", t: t("Confiança no ponto"), x: t("Foto na hora da batida, um aparelho por funcionário e por quantos dias ele pode pedir ajuste de um ponto esquecido.") },
      { a: "h2:Jornada e banco de horas", t: t("Jornada e banco de horas"), x: t("Banco de horas ou hora extra, percentuais das extras, adicional noturno, tolerância do dia e se o intervalo precisa ser batido.") },
      { a: "a[href='/painel/assinatura']", t: t("Assinatura e plano"), x: t("Seu plano, a validade e como pagar ficam aqui.") },
      { a: "form[action='/painel/config'] > button[type=submit]", t: t("Não esqueça de salvar"), x: t("As mudanças só valem depois de tocar em Salvar configurações.") }
    ],
    equipe: [
      { t: t("Equipe"), x: t("Todas as pessoas da empresa. Cada funcionário entra no sistema pelo próprio celular; gerentes entram com o e-mail.") },
      { a: "a[href='/painel/equipe/novo']", t: t("Adicionar pessoa"), x: t("Abre a ficha completa: contato, funções com o salário, nível de acesso e classificação tributária.") },
      { a: "a[href='/painel/gerentes']", t: t("Gerentes e permissões"), x: t("Só o dono vê este botão. Nele você convida gerentes e escolhe o que cada um pode usar no painel.") },
      { a: "h2:Pessoas", t: t("O link de acesso"), x: t("Depois de cadastrar, envie o link de acesso pelo WhatsApp. A pessoa abre o link, cria a própria senha e já pode bater o ponto. Em “Ficha completa” você muda os dados dela.") },
      { a: ".aparelho", t: t("Aparelho novo"), x: t("Cada funcionário bate o ponto em um aparelho só. Se ele trocar de celular, o pedido de liberação aparece aqui para você aceitar.") }
    ],
    ficha: [
      { t: t("Ficha da pessoa"), x: t("Tudo sobre a pessoa numa página: contato, trabalho, salário, jornada, nível de acesso e tipo de contrato.") },
      { a: "h2:Contato", t: t("Contato"), x: t("Funcionário entra pelo celular, então o celular é obrigatório para ele. Gerente entra pelo e-mail.") },
      { a: "h2:Trabalho", t: t("Funções e salários"), x: t("O local, a admissão, até 3 funções com salário mensal ou por hora, a jornada do mês, os dependentes e o vale-transporte. A primeira função define o salário da folha.") },
      { a: "h2:Nível de acesso", t: t("Nível de acesso"), x: t("Funcionário só bate o ponto e vê a escala. Gerente e Gerente Geral entram no painel. Só o dono cadastra gerentes.") },
      { a: "h2:Tipo de contrato", t: t("Tipo de contrato"), x: t("CLT, aprendiz, intermitente, estágio ou PJ. Prestador PJ não entra na folha nem nos avisos de hora extra.") }
    ],
    gerentes: [
      { t: t("Gerentes e permissões"), x: t("Convide outras pessoas para o painel e escolha o que cada uma pode fazer. Configurações, Assinatura e esta tela ficam só com você.") },
      { a: "h2:Novo gerente", t: t("Convidar um gerente"), x: t("Nome, e-mail e o que ele pode usar. O sistema gera um link para ele criar a própria senha.") },
      { a: ".perm-grade", t: t("Permissões"), x: t("Marque as partes do painel que a pessoa pode abrir. O que não estiver marcado some do menu dela.") }
    ],
    escala: [
      { t: t("Escala"), x: t("A escala diz quem trabalha em cada dia e em que horário. É por ela que o sistema sabe quem está atrasado.") },
      { a: ".tabela-escala", t: t("A grade da semana"), x: t("Uma linha por funcionário, uma coluna por dia. Toque em qualquer quadrado para definir o horário, a folga, o intervalo e o local. Embaixo, o total de horas e o custo previsto de cada dia.") },
      { a: ".celula", t: t("Repetir toda semana"), x: t("Ao salvar um dia, a opção “Repetir toda semana” já vem marcada: o horário passa a valer sempre, sem copiar nada. Desmarque para mudar só aquela semana.") },
      { a: "form[action='/painel/escala/publicar']", t: t("Publicar e avisar"), x: t("Depois de montar ou mudar a semana, toque aqui: só quem teve a escala mudada recebe o aviso no celular. O número mostra quantas pessoas ainda não foram avisadas.") },
      { a: "form[action='/painel/escala/fixar']", t: t("Repetir esta semana sempre"), x: t("Montou a semana do jeito certo? Este botão transforma a semana inteira na escala fixa de toda a equipe.") },
      { a: "#ofertas", t: t("Turnos abertos e trocas"), x: t("Publique um turno sem dono para a equipe pegar, ou aprove quando alguém cobre o turno de um colega. Antes de aprovar, você vê se a pessoa vai entrar em hora extra.") },
      { a: "a[href*='wa.me']", t: t("Enviar a escala"), x: t("Abre o WhatsApp com a escala da semana do funcionário já escrita. Ele também vê a escala no próprio aplicativo.") }
    ],
    ponto: [
      { t: t("Ponto"), x: t("Todas as batidas da equipe, dia a dia, com as horas trabalhadas e os atrasos.") },
      { a: "a[href='/painel/ponto/problemas']", t: t("Problemas de ponto"), x: t("Saídas esquecidas e saídas automáticas para conferir. O número mostra quantos esperam por você.") },
      { a: "form[action='/painel/ponto'][method=get]", t: t("Período e funcionário"), x: t("Escolha as datas e, se quiser, uma pessoa só.") },
      { a: "h2:Pedidos de ajuste", t: t("Pedidos de ajuste"), x: t("Quando alguém esquece de bater o ponto, pede o ajuste pelo aplicativo. Você aceita ou recusa aqui.") },
      { a: "h2:Resumo por funcionário", t: t("Resumo"), x: t("Dias trabalhados, horas e atrasos de cada funcionário no período.") },
      { a: "form[action='/painel/ponto/manual']", t: t("Lançar à mão"), x: t("Para incluir uma batida que faltou. Ela fica marcada como lançada pelo gestor.") },
      { a: "a[href^='/painel/ponto.csv']", t: t("Planilha"), x: t("Baixa as batidas e as horas do período, no idioma da tela, para abrir no Excel.") },
      { a: "a[href='/painel/historico']", t: t("Histórico"), x: t("Tudo o que foi alterado, por quem e quando. Nada é apagado.") }
    ],
    fechamento: [
      { t: t("Fechamento do mês"), x: t("No fim do período, esta tela soma as horas de cada funcionário e prepara a folha de ponto.") },
      { a: "form[action='/painel/fechamento'][method=get]", t: t("Escolha o período"), x: t("Normalmente o mês. O sistema já sugere do dia seguinte ao último fechamento até o fim do mês.") },
      { a: ".rolagem table", t: t("As horas de cada um"), x: t("Previsto, trabalhado, horas extras, horas noturnas, atrasos, faltas e o saldo do banco de horas.") },
      { a: "a[href^='/painel/folha']", t: t("Folha de ponto"), x: t("Abre a folha do funcionário, com o dia a dia, para imprimir ou salvar em PDF.") },
      { a: "form[action='/painel/fechamento'][method=post] button", t: t("Fechar o período"), x: t("Depois de conferir, feche. As batidas e a escala desse período ficam travadas, e cada funcionário confere e assina a folha pelo aplicativo.") },
      { a: "h2:Períodos fechados", t: t("Períodos fechados"), x: t("Quantas folhas já foram assinadas, a planilha para o contador e, se precisar corrigir algo, reabrir o último período.") }
    ],
    problemas: [
      { t: t("Problemas de ponto"), x: t("As saídas esquecidas dos últimos 14 dias e as saídas registradas automaticamente, para você resolver de uma vez.") },
      { a: "form[action='/painel/ponto/problemas'] table", t: t("Horário sugerido"), x: t("Cada linha já vem com o horário do fim da escala. Confira, mude se precisar e deixe marcado o que quer resolver.") },
      { a: "form[action='/painel/ponto/problemas'] button[type=submit]", t: t("Resolver os marcados"), x: t("Lança as saídas de uma vez. Tudo fica no histórico com o seu nome.") },
      { a: "#regras", t: t("Regras automáticas"), x: t("Quantos minutos antes do turno o ponto abre, e a saída automática para quem esquece de bater. As duas começam desligadas.") }
    ],
    mural: [
      { t: t("Mural da equipe"), x: t("Avisos para todos: mudança de horário, reunião, o cardápio do dia. A equipe lê no celular.") },
      { a: "form[action='/painel/mural']", t: t("Nova mensagem"), x: t("Escreva o aviso. Marque “Avisar a equipe no celular” para mandar uma notificação, e “Fixar no alto” para ele ficar em destaque.") },
      { a: "form[action='/painel/mural/regra']", t: t("Quem pode escrever"), x: t("Desmarque para que só o gestor publique no mural.") }
    ],
    tarefas: [
      { t: t("Tarefas"), x: t("O checklist diário da equipe: abrir o caixa, limpar a vitrine, conferir o estoque.") },
      { a: "h2:Nova tarefa", t: t("Criar uma tarefa"), x: t("Dê um nome, escolha os dias da semana e se é de uma pessoa ou de toda a equipe.") },
      { a: "h2:Checklist", t: t("Suas tarefas"), x: t("Edite, mude os dias ou desative uma tarefa. O que foi feito hoje e ontem aparece ao lado.") }
    ],
    ausencias: [
      { t: t("Ausências e feriados"), x: t("Atestados, férias, licenças e feriados. Nesses dias o ponto não é cobrado e ninguém leva falta.") },
      { a: "h2:Enviados pelos funcionários", t: t("Atestados recebidos"), x: t("O funcionário envia a foto do atestado pelo aplicativo. Você confere e aceita ou recusa.") },
      { a: "h2:Lançar ausência", t: t("Lançar você mesmo"), x: t("Férias, licença, afastamento ou falta abonada, com o período e um anexo opcional.") },
      { a: "#feriados", t: t("Feriados"), x: t("Carregue os feriados nacionais do ano com um toque e acrescente os da sua cidade e do seu estado.") }
    ],
    pagamento: [
      { t: t("Folha de pagamento"), x: t("O cálculo de apoio da folha de cada funcionário, a partir do mês fechado: salário, extras, adicional noturno, DSR, faltas, INSS, IRRF, vale-transporte e FGTS.") },
      { a: "h2:Dados salariais da equipe", t: t("Dados salariais"), x: t("Salário, jornada mensal, dependentes e vale-transporte de cada um. Também dá para preencher pela ficha de cada pessoa.") },
      { a: "h2:Gerar folha", t: t("Gerar a folha"), x: t("Escolha um período já fechado no Fechamento. O sistema monta os holerites.") },
      { a: "h2:Folhas geradas", t: t("Folhas geradas"), x: t("Abra uma folha para conferir os holerites, lançar bônus e descontos, pedir a revisão do Claude e baixar a planilha para o contador.") }
    ],
    pagamento_folha: [
      { t: t("Folha do período"), x: t("Os holerites de cada funcionário. Enquanto a folha estiver em rascunho, os funcionários ainda não veem.") },
      { a: "h2:Holerites", t: t("Holerites"), x: t("Abra o holerite para ver o cálculo e lançar bônus, comissão, gorjeta ou descontos de alguém.") },
      { a: "#revisao", t: t("Revisão do Claude"), x: t("O Claude refaz as contas e aponta diferenças e riscos trabalhistas antes de você pagar. Confira sempre com o seu contador.") }
    ],
    locais: [
      { t: t("Locais e totens"), x: t("Para quem tem mais de um endereço, ou quer um tablet na empresa para bater o ponto.") },
      { a: "h2:Locais", t: t("Outros endereços"), x: t("Cadastre filiais ou outros lugares onde o ponto também vale. Na escala, você pode dizer em qual local a pessoa trabalha em cada dia.") },
      { a: "#totens", t: t("Totem"), x: t("Um tablet fixo na empresa, um por local. Ele mostra um código que muda a cada minuto, para bater o ponto sem depender do GPS, e aceita ponto por PIN de quem não tem celular.") }
    ],
    assinatura: [
      { t: t("Assinatura"), x: t("Seu plano, até quando o acesso vale e quantos funcionários você pode ter.") },
      { a: "form[action='/painel/assinatura/pagar']", t: t("Assinar ou renovar"), x: t("Escolha o plano e pague por Pix, boleto ou cartão. A liberação é automática.") }
    ]
  };

  // Telas que dividem a mesma aba do menu têm o próprio guia.
  function chaveDaTela() {
    var c = CFG.caminho || "";
    if (c === "/painel/equipe/novo" || /^\/painel\/equipe\/\d+\/ficha$/.test(c)) return "ficha";
    if (c === "/painel/gerentes") return "gerentes";
    if (c === "/painel/ponto/problemas") return "problemas";
    if (c === "/painel/mural") return "mural";
    if (/^\/painel\/pagamento\/\d+$/.test(c)) return "pagamento_folha";
    return CFG.aba;
  }

  // ---------------------------------------------------------------- alvos
  function achar(alvo) {
    if (!alvo) return null;
    var el = null;
    if (alvo.indexOf("h2:") === 0) {
      var texto = t(alvo.slice(3));  // o título na tela está no idioma escolhido
      var hs = document.querySelectorAll(".pagina h2");
      for (var i = 0; i < hs.length; i++) {
        if (hs[i].textContent.trim().indexOf(texto) === 0) { el = hs[i].closest(".cartao") || hs[i]; break; }
      }
    } else {
      try { el = document.querySelector(alvo); } catch (e) { el = null; }
    }
    return el && el.getClientRects().length ? el : null;
  }

  // ---------------------------------------------------------------- motor
  var raiz = null, passos = [], i = 0, guiado = false, atual = null, medir = null;

  function fechar(cancelar) {
    if (raiz) raiz.remove();
    raiz = null;
    if (medir) { window.removeEventListener("resize", medir); window.removeEventListener("scroll", medir, true); }
    document.removeEventListener("keydown", tecla);
    if (cancelar) { try { sessionStorage.removeItem(CHAVE); } catch (e) {} }
  }
  function tecla(ev) {
    if (ev.key === "Escape") fechar(true);
    else if (ev.key === "ArrowRight") avancar();
    else if (ev.key === "ArrowLeft" && i > 0) mostrar(i - 1);
  }
  function proximaTela() {
    var p = SEQ.indexOf(CFG.aba);
    return guiado && p >= 0 && p < SEQ.length - 1 ? TELAS[SEQ[p + 1]] : null;
  }
  function avancar() {
    if (i < passos.length - 1) { mostrar(i + 1); return; }
    var prox = proximaTela();
    if (prox) { fechar(false); location.href = prox.url; return; }
    fechar(true);
  }

  function posicionar() {
    if (!raiz) return;
    var foco = raiz.querySelector(".tour-foco"), balao = raiz.querySelector(".tour-balao");
    var fundo = raiz.querySelector(".tour-fundo");
    var r = atual ? atual.getBoundingClientRect() : null, P = 6;
    var lw = window.innerWidth, lh = window.innerHeight;
    var larg = Math.min(360, lw - 24);
    balao.style.width = larg + "px";
    if (r && r.width && r.height) {
      foco.hidden = false; fundo.classList.remove("escuro");
      var topo = Math.max(4, r.top - P), esq = Math.max(4, r.left - P);
      var alt = Math.min(lh - topo - 4, r.height + P * 2), lar = Math.min(lw - esq - 4, r.width + P * 2);
      foco.style.top = topo + "px"; foco.style.left = esq + "px";
      foco.style.width = lar + "px"; foco.style.height = alt + "px";
      var hb = balao.offsetHeight, embaixo = topo + alt + 12 + hb < lh;
      var y = embaixo ? topo + alt + 12 : topo - 12 - hb;
      if (y < 8) y = Math.max(8, lh - hb - 12);            // alvo grande: o balão fica por cima, no pé da tela
      balao.style.top = y + "px";
      balao.style.left = Math.min(Math.max(12, esq + lar / 2 - larg / 2), lw - larg - 12) + "px";
    } else {
      foco.hidden = true; fundo.classList.add("escuro");
      balao.style.top = Math.max(12, lh / 2 - balao.offsetHeight / 2) + "px";
      balao.style.left = (lw - larg) / 2 + "px";
    }
  }

  function mostrar(n) {
    i = n;
    var p = passos[i], ultimo = i === passos.length - 1, prox = proximaTela();
    atual = achar(p.a);
    raiz.querySelector(".tour-conta").textContent = t("Passo ") + (i + 1) + t(" de ") + passos.length;
    raiz.querySelector(".tour-titulo").textContent = p.t;
    raiz.querySelector(".tour-texto").textContent = p.x;
    raiz.querySelector(".tour-voltar").hidden = i === 0;
    raiz.querySelector(".tour-proximo").textContent =
      !ultimo ? t("Próximo") : prox ? t("Próxima tela: ") + prox.nome : t("Entendi");
    var pontos = raiz.querySelector(".tour-pontos");
    pontos.innerHTML = "";
    for (var k = 0; k < passos.length; k++) {
      var s = document.createElement("span");
      if (k === i) s.className = "agora"; else if (k < i) s.className = "feito";
      pontos.appendChild(s);
    }
    if (atual) atual.scrollIntoView({ block: "center", behavior: "smooth" });
    posicionar();
    setTimeout(posicionar, 380);  // depois da rolagem suave
    setTimeout(posicionar, 900);
    raiz.querySelector(".tour-proximo").focus({ preventScroll: true });
  }

  function iniciar(lista, emSequencia) {
    fechar(false);
    guiado = !!emSequencia;
    passos = lista.filter(function (p) { return !p.a || achar(p.a); });
    if (guiado && SEQ.indexOf(CFG.aba) === SEQ.length - 1) {
      passos.push({ t: t("Pronto, você conhece o essencial"), x: t("Agora é só usar: defina o local da empresa, cadastre a equipe e monte a escala. O ícone de Guia (o ponto de interrogação, no rodapé do menu) mostra de novo as explicações de cada tela.") });
    }
    if (!passos.length) return;
    raiz = document.createElement("div");
    raiz.className = "tour";
    raiz.innerHTML =
      '<div class="tour-fundo"></div><div class="tour-foco" hidden></div>' +
      '<div class="tour-balao" role="dialog" aria-modal="true" aria-labelledby="tour-titulo">' +
      '<div class="tour-topo"><div><div class="tour-conta"></div><div class="tour-titulo" id="tour-titulo"></div></div>' +
      '<button type="button" class="tour-fechar" aria-label="' + t("Fechar o guia") + '">×</button></div>' +
      '<p class="tour-texto"></p>' +
      '<div class="tour-pe"><div class="tour-pontos"></div>' +
      '<button type="button" class="botao claro mini tour-voltar">' + t("Voltar") + '</button>' +
      '<button type="button" class="botao mini tour-proximo"></button></div></div>';
    document.body.appendChild(raiz);
    raiz.querySelector(".tour-fundo").addEventListener("click", function () { fechar(true); });
    raiz.querySelector(".tour-fechar").addEventListener("click", function () { fechar(true); });
    raiz.querySelector(".tour-voltar").addEventListener("click", function () { mostrar(i - 1); });
    raiz.querySelector(".tour-proximo").addEventListener("click", avancar);
    medir = posicionar;
    window.addEventListener("resize", medir);
    window.addEventListener("scroll", medir, true);
    document.addEventListener("keydown", tecla);
    mostrar(0);
  }

  function marcarVisto() {
    fetch("/painel/guia/visto", { method: "POST", headers: { "X-CSRF": CFG.csrf } }).catch(function () {});
  }

  // ---------------------------------------------------------------- ligações
  document.addEventListener("DOMContentLoaded", function () {
    var lista = PASSOS[chaveDaTela()];
    var botao = document.getElementById("abrir-guia");
    if (botao) {
      if (!lista) botao.hidden = true;
      else botao.addEventListener("click", function () {
        try { sessionStorage.removeItem(CHAVE); } catch (e) {}
        iniciar(lista, false);
      });
    }
    var convite = document.getElementById("convite-guia");
    if (convite && convite.showModal) {
      convite.showModal();
      convite.querySelector("[data-guia=sim]").addEventListener("click", function () {
        marcarVisto(); convite.close();
        try { sessionStorage.setItem(CHAVE, "1"); } catch (e) {}
        if (CFG.aba === SEQ[0]) iniciar(PASSOS[SEQ[0]], true); else location.href = TELAS[SEQ[0]].url;
      });
      convite.querySelector("[data-guia=nao]").addEventListener("click", function () { marcarVisto(); convite.close(); });
      convite.addEventListener("cancel", marcarVisto);
      return;
    }
    var andando = false;
    try { andando = sessionStorage.getItem(CHAVE) === "1"; } catch (e) {}
    if (andando && lista && SEQ.indexOf(CFG.aba) >= 0) iniciar(lista, true);
  });
  window.iniciarGuia = function () {  // t("Ver o guia de primeiros passos") (em Configurações)
    try { sessionStorage.setItem(CHAVE, "1"); } catch (e) {}
    location.href = TELAS[SEQ[0]].url;
  };
})();
