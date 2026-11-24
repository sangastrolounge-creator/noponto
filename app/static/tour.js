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
      { t: t("Bem-vindo ao painel"), x: t("Esta é a tela Hoje, o seu ponto de partida. Ela mostra, ao vivo, quem está trabalhando, quem atrasou e quem não apareceu, e se atualiza sozinha a cada minuto. Use o ícone de Guia (o ponto de interrogação, no rodapé do menu) em qualquer tela para ver a explicação dela.") },
      { a: ".abas", t: t("O menu"), x: t("O menu é dividido em três partes. Dia a dia: Hoje, Escala, Ponto, Ausências, Mural e Tarefas. Pagamento: Fechamento e Folha de pagamento. Empresa: Equipe, Locais e Configurações. Onde há uma setinha, ela abre um submenu; no celular, o menu fica na barra de baixo, só com os ícones.") },
      { a: ".menu-selo", t: t("Contadores"), x: t("O número laranja ao lado de um item mostra o que espera por você: problemas de ponto, pedidos de ajuste, atestados enviados, pedidos de folga ou de troca de turno. Quando ele some, não há nada pendente.") },
      { a: "#recolher-lateral", t: t("Recolher o menu"), x: t("Este botão deixa o menu estreito, só com os ícones, para sobrar mais espaço para a escala e as tabelas. Toque de novo para abrir. O sistema lembra a sua escolha neste aparelho.") },
      { a: ".contagem-teste", t: t("Seu teste grátis"), x: t("A barra mostra quantos dias de teste ainda restam. Ela fica amarela nos últimos dias. Toque nela para ver os planos: o pagamento é por Pix, boleto ou cartão, e a liberação é automática.") },
      { a: ".numeros-dia", t: t("Visão geral do dia"), x: t("Quantos estão escalados, quantos estão trabalhando agora, os atrasados, quem ainda não bateu o ponto e os avisos. Os quadros com número clicável levam direto para a lista daquela situação.") },
      { a: ".sugestoes", t: t("Sugestões do sistema"), x: t("O sistema olha a escala, o ponto e as configurações e sugere o próximo passo: definir o local da empresa, cadastrar o salário de alguém, fechar o mês, conferir um descanso curto. Toque na sugestão para ir direto ao lugar certo.") },
      { a: ".rotinas", t: t("Rotinas diárias"), x: t("Atalhos para o que você faz todo dia: ajuste de ponto, atestados, escala, pendências, férias, fechamento e folha. O número no atalho mostra quantas coisas esperam por você naquela rotina.") },
      { a: "h2:" + t("Horas trabalhadas · 7 dias"), t: t("Indicadores"), x: t("Os gráficos mostram as horas trabalhadas dos últimos 7 dias, o custo da mão de obra (CMO) dos últimos 6 meses e a situação da equipe agora. Passe o dedo ou o mouse sobre uma barra para ver o número exato.") },
      { a: "h2:" + t("Equipe de hoje"), t: t("Equipe de hoje"), x: t("Cada pessoa escalada hoje, com a hora em que entrou e saiu e a situação: trabalhando, no intervalo, atrasada, ausente ou de folga. Para quem não bateu o ponto aparece um atalho para chamar no WhatsApp.") },
      { a: "#ativar-avisos", t: t("Avisos no seu celular"), x: t("Toque aqui para receber os avisos como notificação neste aparelho, mesmo com o sistema fechado: atraso, ponto não batido, ponto fora da área e pedidos da equipe. Faça isso em cada aparelho em que quiser ser avisado.") },
      { a: "#faturamento", t: t("Faturamento e CMO"), x: t("Digite o faturamento do dia ou do mês. O sistema compara com o custo da equipe e mostra o CMO, a porcentagem das vendas que vai para a mão de obra. Em restaurantes, o normal fica entre 25% e 35%; a meta você define em Configurações.") },
      { a: ".conta-pessoa", t: t("Sua conta"), x: t("Seu nome e a empresa. Logo abaixo ficam o tema claro ou escuro, as bandeiras para trocar o idioma (Português, English e Español) e os ícones de Master, Guia, Minha conta e Sair.") }
    ],
    avisos: [
      { t: t("Avisos"), x: t("Tudo o que o sistema percebeu nos últimos 7 dias: atrasos, faltas, ponto fora da área, intervalo não feito, saída não batida, aparelhos novos e pedidos da equipe. Cada aviso mostra a pessoa, o dia e o que aconteceu.") },
      { a: ".pagina table, .pagina .cartao", t: t("Como usar os avisos"), x: t("Leia de cima para baixo: os mais recentes ficam primeiro. Para resolver um problema de ponto, use Ponto → Pendências; para um aparelho novo, use Equipe. Os mesmos avisos chegam no seu celular se você ativou as notificações na tela Hoje.") }
    ],
    config: [
      { t: t("Configurações"), x: t("Aqui você diz onde fica a empresa e define as regras do ponto, da jornada e da escala. É o primeiro passo: sem o local da empresa, o ponto é aceito de qualquer lugar. Tudo o que muda aqui fica registrado no histórico.") },
      { a: "h2:" + t("Empresa e local do ponto"), t: t("Nome, endereço e fuso"), x: t("O nome da empresa aparece no aplicativo da equipe e nos documentos. Confira o fuso horário: ele define a hora de todas as batidas e da escala.") },
      { a: "#usar-local", t: t("Onde fica a empresa"), x: t("Estando dentro da empresa, de preferência pelo celular, toque neste botão. A latitude e a longitude são preenchidas sozinhas. Também dá para copiar os dois números do Google Maps. Use “Ver no mapa” para conferir.") },
      { a: "h2:" + t("Regras do ponto"), t: t("Regras do ponto"), x: t("O raio aceito (150 m cobrem a imprecisão normal do GPS), se o ponto fora do raio é bloqueado ou só marcado como “fora da área”, a tolerância de atraso antes do aviso e se cada pessoa bate o ponto só no local da ficha dela (matriz ou filial).") },
      { a: "h2:" + t("Confiança no ponto"), t: t("Confiança no ponto"), x: t("Foto na hora da batida, um aparelho por funcionário (troca de celular precisa da sua liberação), por quantos dias a pessoa pode pedir ajuste de um ponto esquecido e por quanto tempo as fotos ficam guardadas.") },
      { a: "h2:" + t("Jornada e banco de horas"), t: t("Jornada e banco de horas"), x: t("Escolha entre banco de horas ou pagar hora extra, os percentuais das extras em dia normal e em folga ou feriado, o adicional noturno, a tolerância diária de minutos e o intervalo padrão de refeição.") },
      { a: "h2:" + t("Regras da escala"), t: t("Regras da escala"), x: t("Se a equipe confirma os turnos publicados pelo aplicativo (“Vou estar lá”), com quantas horas de antecedência, o que fazer com o turno não confirmado e depois de quantos dias a escala trava para mudanças.") },
      { a: "h2:" + t("Reconhecimento facial no ponto"), t: t("Reconhecimento facial"), x: t("Confere o rosto de quem bate o ponto, no celular e no totem. Cada pessoa cadastra o rosto uma vez, com consentimento. Batidas em que o rosto não conferiu ficam marcadas para você revisar.") },
      { a: "h2:" + t("Logo da empresa"), t: t("Logo"), x: t("A logo aparece no aplicativo da equipe, na folha de ponto e nos holerites. Use uma imagem quadrada ou horizontal, com fundo transparente se possível.") },
      { a: "h2:" + t("Contador parceiro"), t: t("Contador parceiro"), x: t("Ligue a sua empresa ao contador com o código dele. Ele baixa o resumo de horas do mês de cada funcionário para fazer a folha, sem ver fotos, localização, salários nem documentos.") },
      { a: "h2:" + t("Salário mínimo e piso"), t: t("Salário mínimo e piso"), x: t("O sistema avisa quando alguém ganha abaixo do mínimo nacional, do mínimo do seu estado ou do piso da categoria que você informar.") },
      { a: "a[href='/painel/assinatura']", t: t("Assinatura e plano"), x: t("Seu plano, a validade, os pagamentos e como mudar de plano ficam aqui.") },
      { a: "form[action='/painel/config'] > button[type=submit]", t: t("Não esqueça de salvar"), x: t("As mudanças só valem depois de tocar em Salvar configurações. Se algo estiver errado, a tela avisa em vermelho qual campo conferir.") }
    ],
    equipe: [
      { t: t("Equipe"), x: t("Todas as pessoas da empresa. Cada funcionário entra no sistema pelo próprio celular, com o link de acesso que você envia; gerentes entram com o e-mail. Quem sai da empresa é desligado, e o histórico continua guardado.") },
      { a: "a[href='/painel/equipe/novo']", t: t("Adicionar pessoa"), x: t("Abre a ficha completa: nome, celular, local e setor, admissão, funções com o salário, jornada, nível de acesso e tipo de contrato. Só nome e celular são obrigatórios para começar.") },
      { a: "a[href='/painel/importar-equipe']", t: t("Importar planilha"), x: t("Tem muita gente? Envie uma planilha (CSV) com uma pessoa por linha. O sistema mostra uma prévia e só salva depois que você confirmar.") },
      { a: "a[href='/painel/setores']", t: t("Setores"), x: t("Divida a equipe em setores, como Cozinha, Salão e Caixa, para filtrar a Equipe, a Escala, o Ponto e os Relatórios.") },
      { a: "a[href='/painel/gerentes']", t: t("Gerentes e permissões"), x: t("Só o dono vê este botão. Nele você convida gerentes e escolhe, item por item, o que cada um pode usar no painel e de quais locais e setores ele cuida.") },
      { a: "h2:" + t("Pessoas ({v1})"), t: t("O link de acesso"), x: t("Depois de cadastrar, envie o link de acesso pelo WhatsApp. A pessoa abre o link, cria a própria senha e já pode bater o ponto. Em “Ficha completa” você muda os dados dela, o setor e o local de trabalho.") },
      { a: ".aparelho", t: t("Aparelho novo"), x: t("Cada funcionário bate o ponto em um aparelho só. Se ele trocar de celular, o pedido de liberação aparece aqui para você aceitar. Isso evita que alguém bata o ponto pelo colega.") }
    ],
    ficha: [
      { t: t("Ficha da pessoa"), x: t("Tudo sobre a pessoa numa página: contato, trabalho, salário, jornada, documentos, nível de acesso e tipo de contrato. O que você preenche aqui alimenta a escala, o ponto, o fechamento e a folha.") },
      { a: "h2:" + t("Contato"), t: t("Contato"), x: t("Funcionário entra pelo celular, então o celular é obrigatório para ele. Gerente entra pelo e-mail. O celular também é usado para mandar a escala e o link de acesso pelo WhatsApp.") },
      { a: "#f-local", t: t("Local de trabalho"), x: t("O endereço principal ou a filial onde a pessoa trabalha. Com a regra “Cada pessoa bate o ponto só no local dela” ligada em Configurações, o ponto só vale aqui, a não ser que a escala do dia indique outro local.") },
      { a: "#f-vigencia", t: t("Vigência do ponto"), x: t("A partir de que dia a pessoa pode bater o ponto. Útil para quem foi cadastrado antes de começar ou para a migração de outro sistema. Em branco, vale desde já.") },
      { a: "h2:" + t("Trabalho"), t: t("Funções e salários"), x: t("A admissão, até 3 funções com salário mensal ou por hora, a jornada do mês, os dependentes e o vale-transporte. A primeira função define o salário da folha. Sem admissão, o sistema não calcula as férias.") },
      { a: "#documentos", t: t("Documentos"), x: t("CPF, PIS, carteira de trabalho, endereço e dados do banco. Guarde aqui para o contador não precisar pedir de novo.") },
      { a: "#na-escala", t: t("Aparece na escala"), x: t("Desmarque para quem não bate ponto, como um sócio ou um prestador. A pessoa continua na equipe, mas sai da escala e dos avisos.") },
      { a: "h2:" + t("Nível de acesso"), t: t("Nível de acesso"), x: t("Funcionário só bate o ponto, vê a escala e os holerites. Gerente e Gerente Geral entram no painel com as permissões que você escolher. Só o dono cadastra gerentes.") },
      { a: "h2:" + t("Tipo de contrato"), t: t("Tipo de contrato"), x: t("CLT, aprendiz, intermitente, estágio ou PJ. Prestador PJ não entra na folha nem nos avisos de hora extra. Menores de idade ganham as conferências da lei do menor na escala.") }
    ],
    gerentes: [
      { t: t("Gerentes e permissões"), x: t("Convide outras pessoas para o painel e escolha o que cada uma pode fazer. Configurações, Assinatura e esta tela ficam só com você, o dono.") },
      { a: "h2:" + t("Novo gerente"), t: t("Convidar um gerente"), x: t("Nome, e-mail e o que ele pode usar. O sistema gera um link para ele criar a própria senha. Um funcionário que já existe também pode virar gerente pela ficha dele.") },
      { a: ".perm-grade", t: t("Permissões"), x: t("Marque as partes do painel que a pessoa pode abrir: escala, ponto, ausências, fechamento, pagamento e outras. O que não estiver marcado some do menu dela. Os valores de salário só aparecem para quem tem a permissão de pagamento.") }
    ],
    setores: [
      { t: t("Setores"), x: t("Organize a equipe em setores, como Cozinha, Salão, Caixa e Entrega. Os setores aparecem como filtro na Equipe, na Escala, no Ponto e nos Relatórios, e agrupam a grade da escala.") },
      { a: "h2:" + t("Novo setor"), t: t("Criar um setor"), x: t("Dê um nome curto. Depois, escolha o setor de cada pessoa em Equipe → Ficha completa.") },
      { a: "h2:" + t("Setores ({v1})"), t: t("Seus setores"), x: t("Renomeie ou exclua. Excluir um setor não apaga ninguém: as pessoas dele ficam sem setor até você escolher outro.") }
    ],
    importar: [
      { t: t("Importar a equipe"), x: t("Traga todo mundo de uma vez a partir de uma planilha, por exemplo exportada do sistema do contador ou do Excel.") },
      { a: "h2:" + t("1. Prepare a planilha"), t: t("Prepare a planilha"), x: t("Uma pessoa por linha, com as colunas da lista. Só nome e celular são obrigatórios. No Excel, salve como CSV separado por ponto e vírgula.") },
      { a: "h2:" + t("2. Envie o arquivo"), t: t("Envie e confira"), x: t("Depois de enviar, aparece uma prévia com quem está pronto e quem tem erro. Nada é salvo antes de você confirmar. As linhas com erro ficam de fora para você corrigir.") }
    ],
    escala: [
      { t: t("Escala"), x: t("A escala diz quem trabalha em cada dia, em que horário e em que local. É por ela que o sistema sabe quem está atrasado, calcula as horas previstas e mostra o custo da semana.") },
      { a: ".semana-nav", t: t("Semana e filtros"), x: t("Use as setas para trocar de semana. Os filtros Setor e Local mostram só uma parte da equipe; o filtro Local inclui quem foi escalado naquele local durante a semana. Os filtros ficam guardados enquanto você trabalha.") },
      { a: ".tabela-escala", t: t("A grade da semana"), x: t("Uma linha por funcionário, uma coluna por dia. Embaixo do nome, o total de horas da semana e, se você vê os valores, o custo previsto. Quadros em vermelho mostram um problema, como descanso curto ou limite do menor de idade.") },
      { a: ".celula", t: t("Editar um dia"), x: t("Toque em um quadrado para escolher Trabalha, Folga ou Limpar, o horário, a hora e a duração do intervalo, o local e uma observação. Os botões com os turnos mais usados preenchem o horário com um toque.") },
      { a: ".celula", t: t("Repetir toda semana"), x: t("A opção “Repetir toda semana” já vem marcada: o horário passa a valer sempre, sem copiar nada. Desmarque para mudar só aquela semana; o dia ganha a marca “só nesta semana”.") },
      { a: ".celula", t: t("Trabalhar em outro local"), x: t("No campo Local escolha o endereço principal ou uma filial para aquele dia. O local vale só naquele dia; para repetir nas próximas semanas, marque “Usar este local também nas próximas semanas”.") },
      { a: "#interjornada", t: t("Descanso entre turnos"), x: t("A CLT exige 11 horas de descanso entre o fim de um turno e o começo do outro. Os turnos que desrespeitam isso aparecem marcados na grade.") },
      { a: "#escala-justa", t: t("Publicação com antecedência"), x: t("Mostra se a escala da semana já foi publicada e com quanta antecedência. Publicar cedo dá previsibilidade para a equipe.") },
      { a: "form[action='/painel/escala/copiar']", t: t("Copiar semana anterior"), x: t("Preenche os dias vazios desta semana com os horários e locais da semana anterior. O que já está preenchido não é mudado.") },
      { a: "form[action='/painel/escala/publicar']", t: t("Publicar e avisar"), x: t("Depois de montar ou mudar a semana, toque aqui: só quem teve a escala mudada recebe o aviso no celular. O número mostra quantas pessoas ainda não foram avisadas.") },
      { a: "form[action='/painel/escala/fixar']", t: t("Repetir esta semana sempre"), x: t("Montou a semana do jeito certo? Este botão transforma a semana inteira na escala fixa de toda a equipe, e as próximas semanas passam a ser criadas sozinhas.") },
      { a: "#ofertas", t: t("Turnos abertos e trocas"), x: t("Publique um turno sem dono para a equipe pegar, ou aprove quando alguém cobre o turno de um colega. Antes de aprovar, você vê se a pessoa vai entrar em hora extra ou ficar sem descanso.") },
      { a: "a[href*='wa.me']", t: t("Enviar a escala"), x: t("Abre o WhatsApp com a escala da semana do funcionário já escrita. Ele também vê a escala no próprio aplicativo e recebe um lembrete na hora de bater o ponto.") }
    ],
    ponto: [
      { t: t("Ponto"), x: t("Todas as batidas da equipe, dia a dia, com as horas trabalhadas, as previstas e os atrasos. As batidas nunca são apagadas: uma correção anula a original e a guarda no histórico, como pede a Portaria 671.") },
      { a: "a[href='/painel/ponto/ajuste']", t: t("Ajuste de ponto"), x: t("A tela para corrigir o dia a dia de cada pessoa: lançar a batida que faltou, anular uma batida errada ou abonar um dia, sempre com o motivo.") },
      { a: "a[href='/painel/ponto/problemas']", t: t("Pendências"), x: t("Saídas esquecidas e entradas antes do horário para conferir, todas juntas. O número mostra quantas esperam por você.") },
      { a: "h2:" + t("Pedidos de ajuste ({v1})"), t: t("Pedidos de ajuste"), x: t("Quando alguém esquece de bater o ponto, pede o ajuste pelo aplicativo com o horário e o motivo. Você aceita ou recusa aqui, e a pessoa recebe a resposta no celular.") },
      { a: "form[action='/painel/ponto'][method=get]", t: t("Período e funcionário"), x: t("Escolha as datas e, se quiser, uma pessoa ou um setor. Os números e a planilha seguem esse filtro.") },
      { a: "#calendario", t: t("Calendário"), x: t("Cada dia do período com a situação da equipe. Toque num dia para abrir a escala daquele dia.") },
      { a: "h2:" + t("Resumo por funcionário"), t: t("Resumo"), x: t("Dias trabalhados, horas previstas e trabalhadas, a diferença e os atrasos de cada funcionário no período.") },
      { a: "form[action='/painel/ponto/manual']", t: t("Lançar à mão"), x: t("Para incluir uma batida que faltou. Ela fica marcada como lançada pelo gestor, com o motivo, e aparece assim na folha de ponto.") },
      { a: "a[href^='/painel/ponto.csv']", t: t("Planilha"), x: t("Baixa as batidas e as horas do período, no idioma da tela, para abrir no Excel.") },
      { a: "a[href='/painel/historico']", t: t("Histórico"), x: t("Tudo o que foi alterado, por quem e quando. Nada é apagado.") }
    ],
    ajuste: [
      { t: t("Ajuste de ponto"), x: t("O dia a dia de cada colaborador numa grade: as batidas, as faltando e as marcadas para conferir. É aqui que você corrige o ponto antes do fechamento.") },
      { a: "form[method=get]", t: t("Período"), x: t("Escolha as datas. Dias de um período já fechado não mudam: para corrigir, reabra o período em Fechamento.") },
      { a: "h2:" + t("Colaboradores"), t: t("A grade de cada pessoa"), x: t("Os quadros vazios com tracejado são batidas que faltam: clique para lançar. Passe o mouse nos três pontinhos de um dia para abonar, anular uma batida ou registrar um ponto em atraso. Batidas em que o rosto não conferiu ficam marcadas.") },
      { t: t("Tudo fica registrado"), x: t("Cada ajuste pede o motivo e guarda quem fez e quando. A batida original nunca some: fica em Histórico → Batidas anuladas.") }
    ],
    problemas: [
      { t: t("Pendências de ponto"), x: t("As saídas esquecidas e as entradas antes do horário dos últimos 14 dias, para você resolver de uma vez.") },
      { a: "form[action='/painel/ponto/problemas'] table", t: t("Horário sugerido"), x: t("Cada saída esquecida já vem com o horário do fim da escala. Na entrada antecipada, manter o horário deixa a entrada como está; mudar o horário faz a entrada contar a partir dele, e a marcação original continua guardada.") },
      { a: "form[action='/painel/ponto/problemas'] button[type=submit]", t: t("Resolver os marcados"), x: t("Lança tudo o que está marcado de uma vez. Cada mudança fica no histórico com o seu nome.") },
      { a: "#regras", t: t("Regras do ponto"), x: t("Quantos minutos antes do turno a entrada passa a ser marcada para conferir e o lembrete para quem esquece de bater a saída. Pela Portaria 671, o sistema não impede a batida em nenhum horário: só sinaliza para você decidir.") }
    ],
    historico: [
      { t: t("Histórico"), x: t("Tudo o que foi alterado no ponto e nas regras, por quem e quando. Nada aqui pode ser apagado, nem por você.") },
      { a: "#alteracoes", t: t("Alterações"), x: t("Escala, ajustes, configurações, aparelhos liberados e fechamentos, com a data e o nome de quem fez.") },
      { a: "#anuladas", t: t("Batidas anuladas"), x: t("O registro original de cada batida que foi corrigida, com a origem (aplicativo, totem ou lançamento) e a distância da empresa.") }
    ],
    perguntas: [
      { t: t("Perguntas de fim de turno"), x: t("Perguntas que a equipe responde no celular ao bater a saída, como “Faltou algum produto?” ou “Quanto recebeu de gorjeta em dinheiro?”. Use poucas, para a saída continuar rápida.") },
      { a: "h2:" + t("Perguntas"), t: t("Criar uma pergunta"), x: t("Escolha o tipo de resposta: sim ou não, número ou texto livre. Nas de sim ou não, você pode ser avisado quando vier a resposta que pede atenção.") },
      { a: "h2:" + t("Respostas dos últimos 14 dias"), t: t("Respostas"), x: t("O que cada pessoa respondeu nos últimos 14 dias.") }
    ],
    fechamento: [
      { t: t("Fechamento do mês"), x: t("No fim do período, esta tela soma as horas de cada funcionário, mostra o que ainda precisa de atenção e prepara a folha de ponto para a equipe assinar.") },
      { a: "form[action='/painel/fechamento'][method=get]", t: t("Escolha o período"), x: t("Normalmente o mês. O sistema já sugere do dia seguinte ao último fechamento até o fim do mês.") },
      { a: ".pagina .alerta", t: t("O que conferir antes"), x: t("Os avisos amarelos mostram dias sem escala, batidas faltando, intervalos não feitos e pedidos sem resposta. Resolva antes de fechar para a folha sair certa.") },
      { a: ".rolagem table", t: t("As horas de cada um"), x: t("Previsto, trabalhado, horas extras, extras em folga ou feriado, horas noturnas, atrasos, faltas e o saldo do banco de horas. Os percentuais seguem o que está em Configurações.") },
      { a: "a[href^='/painel/folha']", t: t("Folha de ponto"), x: t("Abre a folha do funcionário, com o dia a dia, para imprimir ou salvar em PDF.") },
      { a: "form[action='/painel/fechamento'][method=post] button", t: t("Fechar o período"), x: t("Depois de conferir, feche. As batidas e a escala desse período ficam travadas, e cada funcionário confere e assina a folha pelo aplicativo.") },
      { a: "h2:" + t("Períodos fechados"), t: t("Períodos fechados"), x: t("Quantas folhas já foram assinadas, a planilha para o contador e, se precisar corrigir algo, reabrir o último período.") }
    ],
    pagamento: [
      { t: t("Folha de pagamento"), x: t("O cálculo de apoio da folha de cada funcionário, a partir do mês fechado: salário, extras, adicional noturno, DSR, faltas, INSS, IRRF, vale-transporte e FGTS. Confira sempre com o seu contador.") },
      { a: "h2:" + t("Dados salariais da equipe"), t: t("Dados salariais"), x: t("Salário, jornada mensal, dependentes e vale-transporte de cada um. Também dá para preencher pela ficha de cada pessoa. Sem salário, a pessoa fica fora da folha.") },
      { a: "h2:" + t("Gerar folha"), t: t("Gerar a folha"), x: t("Escolha um período já fechado no Fechamento. O sistema monta os holerites em rascunho.") },
      { a: "h2:" + t("Folhas geradas"), t: t("Folhas geradas"), x: t("Abra uma folha para conferir os holerites, lançar bônus e descontos, ratear gorjetas, pedir a revisão do Claude e baixar a planilha para o contador.") }
    ],
    pagamento_folha: [
      { t: t("Folha do período"), x: t("Os holerites de cada funcionário. Enquanto a folha estiver em rascunho, os funcionários ainda não veem. Ao liberar, cada um vê o próprio holerite no aplicativo.") },
      { a: "h2:" + t("Holerites"), t: t("Holerites"), x: t("Abra o holerite para ver o cálculo e lançar bônus, comissão, gorjeta ou descontos de alguém.") },
      { a: "#gorjetas", t: t("Rateio de gorjetas"), x: t("Informe o total das gorjetas e como dividir: pelas horas trabalhadas ou em partes iguais. O sistema divide e lança no holerite de cada um.") },
      { a: "#revisao", t: t("Revisão do Claude"), x: t("O Claude refaz as contas e aponta diferenças e riscos trabalhistas antes de você pagar. Confira sempre com o seu contador.") }
    ],
    ausencias: [
      { t: t("Ausências e feriados"), x: t("Atestados, férias, licenças e feriados. Nesses dias o ponto não é cobrado, ninguém leva falta e o fechamento já sabe o que fazer.") },
      { a: "h2:" + t("Enviados pelos funcionários ({v1})"), t: t("Atestados recebidos"), x: t("O funcionário envia a foto do atestado pelo aplicativo. Você confere e aceita ou recusa; ao aceitar, os dias viram ausência justificada.") },
      { a: "h2:" + t("Lançar ausência"), t: t("Lançar você mesmo"), x: t("Férias, licença, afastamento ou falta abonada, com o período e um anexo opcional.") },
      { a: "h2:" + t("Ausências recentes e futuras"), t: t("Ausências lançadas"), x: t("As ausências de agora e as próximas. Dá para corrigir ou excluir uma ausência lançada por engano.") },
      { a: "#feriados", t: t("Feriados"), x: t("Carregue os feriados nacionais do ano com um toque e acrescente os da sua cidade e do seu estado. Trabalho em feriado conta como extra com o percentual de folga.") }
    ],
    ferias: [
      { t: t("Férias"), x: t("O saldo de cada pessoa por período aquisitivo, os prazos para dar as férias e o que a CLT pede ao marcar. O cálculo usa a data de admissão da ficha.") },
      { a: "h2:" + t("Saldos da equipe"), t: t("Saldos"), x: t("Quantos dias cada um tem, até quando precisa tirar e quem está perto de vencer. Férias dadas fora do prazo são pagas em dobro.") },
      { a: "h2:" + t("Marcar férias"), t: t("Marcar férias"), x: t("Escolha a pessoa e as datas. Antes de lançar, o sistema mostra uma prévia e confere as regras da CLT, como o aviso com 30 dias e o início que não pode cair logo antes de folga ou feriado.") },
      { a: "h2:" + t("Ajustar um saldo"), t: t("Ajustar um saldo"), x: t("Para férias tiradas antes de usar o sistema ou outro acerto, com o motivo registrado.") }
    ],
    tarefas: [
      { t: t("Tarefas"), x: t("O checklist diário da equipe: abrir o caixa, limpar a vitrine, conferir o estoque. A equipe marca no celular o que fez.") },
      { a: "h2:" + t("Nova tarefa"), t: t("Criar uma tarefa"), x: t("Dê um nome, escolha os dias da semana e se é de uma pessoa ou de toda a equipe.") },
      { a: "h2:" + t("Checklist ({v1})"), t: t("Suas tarefas"), x: t("Edite, mude os dias ou desative uma tarefa. O que foi feito hoje e ontem aparece ao lado, com quem fez.") }
    ],
    mural: [
      { t: t("Mural da equipe"), x: t("Avisos para todos: mudança de horário, reunião, o cardápio do dia. A equipe lê no celular.") },
      { a: "form[action='/painel/mural']", t: t("Nova mensagem"), x: t("Escreva o aviso. Marque “Avisar a equipe no celular” para mandar uma notificação, e “Fixar no alto” para ele ficar em destaque.") },
      { a: "form[action='/painel/mural/regra']", t: t("Quem pode escrever"), x: t("Desmarque para que só o gestor publique no mural.") }
    ],
    locais: [
      { t: t("Locais e totens"), x: t("Para quem tem mais de um endereço, quer desenhar a área exata do ponto ou quer um tablet na empresa para bater o ponto.") },
      { a: "h2:" + t("Locais"), t: t("Filiais e outros endereços"), x: t("Cadastre cada filial com o nome, a localização e o raio. Na ficha de cada pessoa você escolhe o local dela, e na escala pode indicar outro local para um dia.") },
      { a: "#usar-local", t: t("Localização da filial"), x: t("Estando dentro da filial, toque em “Usar minha localização atual”. Também dá para colar os dois números do Google Maps.") },
      { a: "#cerca", t: t("Cerca virtual"), x: t("Em vez de um círculo, desenhe no mapa o contorno exato do terreno ou do prédio. Você também pode ser avisado quando alguém sai da área durante o turno.") },
      { a: "#totens", t: t("Totem"), x: t("Um tablet fixo na empresa, um por local. Ele mostra um código que muda a cada minuto para bater o ponto sem depender do GPS, aceita ponto por PIN de quem não tem celular e pode conferir o rosto.") }
    ],
    cerca: [
      { t: t("Desenhar a cerca"), x: t("Marque no mapa os cantos da área onde o ponto vale: o terreno, o prédio, o estacionamento.") },
      { a: "#mapa", t: t("O mapa"), x: t("Toque para colocar cada canto e arraste os pontos para ajustar. Deixe uma folga de alguns metros: o GPS do celular costuma errar de 10 a 30 metros.") },
      { a: "#cerca-form", t: t("Salvar"), x: t("Salve para a cerca valer. Para voltar ao círculo do raio, apague o contorno.") }
    ],
    relatorios: [
      { t: t("Relatórios"), x: t("Escalado contra trabalhado, pontualidade, faltas, custo e como a equipe avaliou os turnos, no período que você escolher.") },
      { a: ".relatorio-filtro", t: t("Período"), x: t("Escolha as datas e o setor. Os números são recalculados na hora.") },
      { a: "h2:" + t("Por pessoa"), t: t("Por pessoa"), x: t("Horas, atrasos, faltas e extras de cada funcionário. Bom para conversas de desempenho e para achar quem precisa de ajuda com a escala.") },
      { a: "h2:" + t("Por cargo"), t: t("Por cargo"), x: t("O mesmo resumo agrupado por função, para ver onde está o custo e a falta de gente.") },
      { a: "h2:" + t("O que a equipe disse"), t: t("O que a equipe disse"), x: t("As notas e os comentários que a equipe deu ao bater a saída. Ligue a avaliação do turno nesta tela.") }
    ],
    conformidade: [
      { t: t("Central de conformidade"), x: t("As regras da CLT que o sistema confere com a escala e o ponto, num lugar só, atualizadas toda vez que você abre a página.") },
      { a: ".pagina .cartao", t: t("As cores"), x: t("Vermelho pode gerar multa ou ação trabalhista; amarelo, confira em breve; verde, em dia. Cada item explica o problema e leva para a tela onde se resolve.") },
      { t: t("Portaria 671"), x: t("Os comprovantes de ponto, o AFD, o AEJ e o Atestado Técnico para a fiscalização ficam disponíveis por aqui. Não é orientação jurídica: confira com o seu contador ou advogado.") }
    ],
    assinatura: [
      { t: t("Assinatura"), x: t("Seu plano, até quando o acesso vale e quantos funcionários você pode ter.") },
      { a: "h2:" + t("Planos"), t: t("Planos"), x: t("Compare os planos pelo número de pessoas e pelos recursos. Dá para mudar de plano quando quiser.") },
      { a: "form[action='/painel/assinatura/pagar']", t: t("Assinar ou renovar"), x: t("Escolha o plano e pague por Pix, boleto ou cartão. A liberação é automática. No cartão, a cobrança se repete sozinha todo mês.") },
      { a: "h2:" + t("Tem um cupom?"), t: t("Cupom"), x: t("Tem um código de desconto? Aplique aqui antes de pagar.") },
      { a: "#indique", t: t("Indique e ganhe"), x: t("Envie o seu link para outra empresa. Quando ela assinar, você ganha 1 mês grátis.") }
    ]
  };

  // Telas que dividem a mesma aba do menu têm o próprio guia.
  function chaveDaTela() {
    var c = CFG.caminho || "";
    if (c === "/painel/equipe/novo" || /^\/painel\/equipe\/\d+\/ficha$/.test(c)) return "ficha";
    if (c === "/painel/gerentes") return "gerentes";
    if (c === "/painel/setores") return "setores";
    if (c === "/painel/importar-equipe") return "importar";
    if (c === "/painel/ponto/problemas") return "problemas";
    if (c === "/painel/ponto/ajuste") return "ajuste";
    if (c === "/painel/historico") return "historico";
    if (c === "/painel/perguntas") return "perguntas";
    if (c === "/painel/avisos") return "avisos";
    if (c === "/painel/ferias") return "ferias";
    if (c === "/painel/mural") return "mural";
    if (c.indexOf("/painel/cerca/") === 0) return "cerca";
    if (/^\/painel\/pagamento\/\d+$/.test(c)) return "pagamento_folha";
    return CFG.aba;
  }

  // ---------------------------------------------------------------- alvos
  function achar(alvo) {
    if (!alvo) return null;
    var el = null;
    if (alvo.indexOf("h2:") === 0) {
      var texto = alvo.slice(3);  // já traduzido com t(); títulos com contagem: só o começo
      if (texto.indexOf("{") > 0) texto = texto.slice(0, texto.indexOf("{")).trim();
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
