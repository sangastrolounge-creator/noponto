# Turno+ — escala, ponto por geolocalização e tarefas de equipe

App web multiempresa, independente do cmv-app e do avalia-app.

- **Gestor** (`/painel`, computador ou celular): painel do dia com avisos, escala da semana,
  espelho de ponto com exportação CSV, checklist diário, equipe e configurações.
- **Funcionário** (`/app`, celular, instalável como PWA): bate o ponto (a localização é
  conferida com a da empresa), vê a escala e marca as tarefas do dia.

## Como funciona por dentro

- Python/FastAPI + Jinja2 (HTML no servidor, sem node) + SQLite. Um contêiner só.
- `app/regras.py` concentra as regras: distância, batida, situação do turno, alertas, espelho.
- A cada minuto o próprio app verifica os turnos (`ciclo_alertas` em `app/main.py`) e cria
  avisos de ponto não batido e de saída esquecida. Por isso roda com **um processo só**.
- Avisos push no aparelho do gestor: `app/push.py` (Web Push). As chaves são criadas sozinhas
  na primeira vez e ficam na tabela `config`.
- O "dia de trabalho" vira às 4h da manhã (`VIRADA_H`), para quem fecha de madrugada.
- Um turno por funcionário por dia. Intervalo = bater saída e entrada de novo.

## Confiança no ponto e fechamento

- `app/regras.py`: um aparelho por funcionário (cookie `turno_aparelho`), foto na batida,
  batida anulada em vez de apagada, pedido de ajuste, lembretes.
- `app/apuracao.py`: cálculo do período. Previsto = escala menos o intervalo (zero em folga,
  feriado e ausência). Diferença diária dentro da tolerância não conta. Dia de escala sem
  batida é falta e fica fora do saldo. Trabalho em folga ou feriado é extra à parte.
  Banco de horas: o saldo final de um fechamento é o saldo anterior do seguinte.
  Percentuais de hora extra (padrão 50% em dia normal, 100% em folga/feriado) ficam na
  empresa e são copiados para o fechamento; "extras com acréscimo" é só informativo, o
  banco continua hora por hora e não há valores em dinheiro.
- `app/fase2.py`: ausências com anexo, feriados, fechamento (trava o período), planilha
  para o contador, folha de ponto imprimível e assinatura do funcionário por senha.
- Revenda (`app/master.py`, regras em `app/core.py`): Painel Master em `/master` para o
  usuário `is_admin` (o e-mail de `ADMIN_EMAIL`; sem ele, o gestor mais antigo, cuja empresa
  vira cortesia). Empresas com situação teste/ativa/cortesia/vencida/bloqueada; planos com
  limite de funcionários ativos; pagamento registrado à mão (não há gateway); entrar no
  painel da empresa como suporte (`master_uid` na sessão). Vencida: o gestor só abre
  `/painel/assinatura`, o ponto continua. Bloqueada: ninguém entra. Tabela `plataforma`
  (nome, teste, cadastro aberto, como pagar). Teste: `tests/test_revenda.py`.
- Asaas (`app/asaas.py`, `app/cobranca.py`, igual ao avalia-app): com `ASAAS_API_KEY` a tela
  Assinatura gera cobrança avulsa (30 dias) ou assinatura mensal; a liberação vem pelo
  webhook `POST /asaas/webhook` (token em `ASAAS_WEBHOOK_TOKEN`, cadastrado sozinho na
  subida se `PUBLIC_URL` for https) ou quando a tela consulta a cobrança pendente.
  `externalReference` = `turno:<id>`; cobranças de outros sistemas na mesma conta são
  ignoradas. Sem a chave, a cobrança é manual. Teste: `tests/test_asaas.py` (Asaas simulado).
- Página de vendas (`app/vendas.py`, `templates/vendas.html`, `templates/master/pagina.html`,
  `static/vendas.css`, igual ao avalia-app): editor em Master → Página de vendas, com
  rascunho → prévia → publicar. Até publicar, a raiz mostra `inicio.html`. Mídia em
  `/dados/midia`, servida em `/midia` (imagem 5 MB, vídeo 100 MB ou link YouTube/Vimeo).
  Teste: `tests/test_vendas.py`.
- Escala fixa (`escala_fixa`: uma regra por funcionário e dia da semana). Os turnos de cada
  data continuam em `turnos` e são criados sozinhos por `regras.gerar_da_fixa` (no ciclo de
  1 minuto, para os próximos 14 dias, e ao abrir a escala de qualquer semana futura). Nunca
  cria para trás. `turnos.fixa` = veio da regra; alterar só uma data cria uma exceção.
  Mudar a regra apaga os turnos futuros sem batida desses dias. Teste: `tests/test_escala_fixa.py`.
- Guia do gestor (`static/tour.js`, sem biblioteca): passos por tela em `PASSOS`, apontando
  para seletores CSS ou `h2:Título do cartão`; passo sem alvo na tela é pulado. Botão "Guia"
  no topo de cada tela; convite de boas-vindas na tela Hoje até o gestor responder
  (`usuarios.guia_visto`, `POST /painel/guia/visto`); o guia de primeiros passos percorre
  `SEQ` (Hoje → Configurações → Equipe → Escala → Ponto → Fechamento) via `sessionStorage`.
  Ao mudar uma tela, conferir se os seletores do guia dela ainda existem.
- Resumo do fechamento (`/painel/fechamento/resumo?de=&ate=`, `templates/resumo.html`): uma
  linha por funcionário e o total da equipe (horas trabalhadas, noturnas, extras, faltas em
  horas e em dias; coluna zerada não aparece). PDF pelo imprimir do navegador.
- Troca de domínio: `APP_HOST` e `PUBLIC_URL` novos, `APP_REGRA_EXTRA` com os endereços extras
  na regra do Traefik e `HOSTS_REDIRECIONAR` com os mesmos endereços (GET/HEAD viram 301 para
  o `PUBLIC_URL`). Cookies são por endereço: depois da troca todos entram de novo, os aparelhos
  autorizados e os totens precisam ser liberados outra vez, e o webhook antigo do Asaas deve
  ser apagado.
- DSR (`empresas.dsr_faltas`, `dsr_extras`; cálculo em `apuracao.apurar`): cada semana
  (segunda a domingo) com falta sem justificativa tira um descanso, com as horas do dia
  faltado; o reflexo das extras = extras com acréscimo ÷ dias úteis (segunda a sábado, fora
  feriados) × domingos e feriados do período, só no controle por hora extra. É informativo:
  não entra no saldo nem no banco. Guardado em `fechamento_linhas`. Teste: `tests/test_dsr.py`.
- Hora de saída para o intervalo (`turnos.intervalo_inicio`, também na escala fixa): opcional,
  precisa caber no turno (`regras.intervalo_cabe`); mostrada na grade, no app e no WhatsApp;
  lembrete push no horário (`lembrou_int`). Não entra no cálculo das horas.
- Adicional noturno: minutos entre 22h e 5h (`apuracao.minutos_noturnos`); com a hora
  reduzida ligada, somam 1/7 a mais ao trabalhado. Guardado em `fechamento_linhas.noturno`.
  Não trata a prorrogação depois das 5h (Súmula 60 do TST).
- Intervalo não concedido (art. 71, §4º; `clt.intervalo_suprimido`, `apuracao.apurar`): mais de 6 h
  de trabalho pedem `empresas.intervalo_minimo_min` (60, ou de 30 a 59 por convenção); de 4 a 6 h,
  15 min. Vale o maior intervalo batido no dia; o intervalo descontado sozinho (pré-assinalado)
  conta como feito. O que faltou vai para `fechamento_linhas.intervalo_suprimido` e entra no
  holerite com 50% a mais (ou o % da extra, se maior), sem INSS, FGTS e IRRF. Teste: `tests/test_ferias.py`.
- Férias (`app/ferias.py`, regras puras em `app/clt.py`, tela `/painel/ferias`): períodos aquisitivos
  a partir de `usuarios.data_admissao`; dias pela tabela de faltas do art. 130 (faltas dos
  fechamentos, divididas pelos dias quando o fechamento pega dois períodos); perde o período com
  mais de 6 meses de afastamento (art. 133); prazo concessivo e dias em dobro (art. 137); avisos
  ao marcar (até 3 partes, 14 e 5 dias, 30 dias de aviso, início antes de feriado/folga, abono de
  até 1/3); ajustes à mão em `ferias_ajustes`; `ausencias.abono_dias` guarda os dias vendidos.
  O funcionário vê o saldo e pede férias em Ajustes do app. Entra na Central de conformidade.
  PJ fica de fora; estágio tem 30 dias de recesso sem a tabela de faltas.
- Intervalo obrigatório (`intervalo_obrigatorio`): mais de 6 h direto gera o aviso
  `sem_intervalo` e a marca "intervalo não batido"; o desconto continua em `intervalo_auto`.
  Teste: `tests/test_fase4.py`.
- `app/fase3.py`: locais extras (`locais`; o principal continua na empresa; turno com
  `local_id` só vale lá), totem (`totens`: tablet ativado por link, cookie `turno_totem`,
  código de 6 dígitos que muda a cada minuto, QR pela biblioteca `qrcode`, ponto por PIN)
  e atalhos `wa.me` (o gestor é quem envia; não há envio automático de WhatsApp).
- Fotos das batidas: guardadas por no máximo 45 dias (`fotos_dias`, 1 a 45) e abertas em
  janela pelo painel (`dlg-foto` em `painel_base.html`).
- Fotos em `/dados/fotos`, anexos em `/dados/anexos` (mesmo volume do banco).
- Testes: `tests/test_fluxo.py`, `tests/test_fase1.py`, `tests/test_fase2.py`, `tests/test_fase3.py`.

## Rodar no computador

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt httpx
.venv/Scripts/python tests/test_fluxo.py        # teste de ponta a ponta
.venv/Scripts/python tools/demo.py              # empresa de demonstração (acessos no próprio arquivo)
.venv/Scripts/python -m uvicorn app.main:app --port 8766
```

## VPS

Pasta `/home/turno-app`, projeto Docker `turno-app`, porta só em `127.0.0.1:8096`, banco no
volume `turno-app_dados`. Segredos em `/home/turno-app/.env` (modelo em `.env.example`).

```
docker compose up -d --build
```

Ver sem domínio: `ssh -N -L 8096:127.0.0.1:8096 vps` e abrir `http://localhost:8096`.
Para publicar: `PUBLICAR=true`, `APP_HOST`, `PUBLIC_URL` e `COOKIE_SECURE=true` no `.env`.
A localização do celular só funciona em **https**.

## Limites conhecidos

- A localização vem do navegador do celular. Quem usa aplicativo de GPS falso consegue burlar.
- É um controle gerencial. Não é um registrador de ponto homologado (REP-P, Portaria 671/2021).
- Sem cobrança/planos e sem recuperação de senha por e-mail (o gestor gera novo link).
