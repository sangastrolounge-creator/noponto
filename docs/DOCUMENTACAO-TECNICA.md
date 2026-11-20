# No Ponto — documentação técnica

Para quem vai manter o sistema. Descreve como ele é montado, onde fica cada regra, como
publicar e como não perder dados. Atualizado em 17/11/2026.

O produto se chama **No Ponto** (nome configurável no Painel Master); no código e no servidor
o projeto ainda se chama `turno-app`, o nome provisório da primeira versão.

---

## 1. Visão geral

Plataforma web multiempresa (SaaS) de **escala, ponto e fechamento do mês** para equipes por
turno, vendida por assinatura.

| Quem | Onde | O que faz |
|---|---|---|
| Dono da plataforma (master) | `/master` | Empresas clientes, planos, pagamentos, página de vendas |
| Gestor da empresa | `/painel` | Escala, ponto, tarefas, ausências, fechamento, equipe, locais |
| Funcionário | `/app` (celular, instalável) | Bater ponto, ver escala, pedir ajuste, assinar a folha |
| Totem (tablet na empresa) | `/totem` | Código do minuto, QR e ponto por PIN |
| Visitante | `/` | Página de vendas, cadastro com teste grátis |

Endereço de produção: **https://noponto.app.br** (o antigo `turno.187-41-107-49.sslip.io`
e o `www` redirecionam com 301).

---

## 2. Tecnologia

- **Python 3.12** (no contêiner), **FastAPI 0.115**, **Jinja2** (HTML gerado no servidor,
  sem framework de front-end e sem Node), **SQLAlchemy 2.0** com **SQLite** (modo WAL).
- Sessão por cookie assinado (`turno_sessao`, 90 dias) via `SessionMiddleware`.
  Proteção CSRF por token na sessão (campo `csrf` ou cabeçalho `X-CSRF`).
- Senhas e PINs com **scrypt** (`core.gerar_hash` / `conferir_senha`).
- **PWA**: `manifest.webmanifest`, service worker `static/sw.js` (cache `turno-vNN`).
- **Web Push** com `pywebpush` (chaves VAPID geradas na primeira vez e guardadas na tabela
  `config`).
- **Asaas** para cobrança (Pix, boleto, cartão) via `httpx`.
- QR Code do totem com a biblioteca `qrcode` (SVG).
- Um único contêiner Docker; um único processo (o agendador roda dentro do app).

---

## 3. Organização do código

```
app/
  main.py        rotas principais: login, painel (hoje, escala, ponto, tarefas, equipe,
                 configurações), app do funcionário, PWA, idioma, agendador
  core.py        configuração, templates, sessão/CSRF, login, situação da assinatura
  db.py          modelos (tabelas), migração de colunas novas, dados iniciais da revenda
  regras.py      regras do ponto: horário local, distância, batida, alertas, escala fixa
  apuracao.py    cálculo do período: previsto, trabalhado, extras, noturno, faltas, DSR,
                 banco de horas, feriados, fechamento
  fase2.py       ausências, feriados, fechamento, folha de ponto, resumo em PDF, planilha
  fase3.py       locais extras (filiais), totem, PIN, atalhos de WhatsApp
  master.py      Painel Master (revenda): empresas, planos, plataforma, suporte
  cobranca.py    assinatura da empresa e webhook do Asaas
  asaas.py       cliente mínimo da API do Asaas
  vendas.py      página de vendas editável (uma por idioma)
  push.py        notificações push
  i18n.py        idiomas (português, inglês, espanhol)
  idiomas/       en.json, es.json (traduções) e _js.json (textos dos scripts)
  templates/     telas (Jinja2)
  static/        style.css, vendas.css, tour.js (guia), sw.js, ícones
tests/           testes de ponta a ponta (um arquivo por área)
tools/           demo.py (empresa de demonstração), ícones, ferramentas de tradução
docs/            esta documentação e o manual do usuário
```

---

## 4. Banco de dados

Arquivo SQLite em `/dados/turno.db` (volume Docker `turno-app_dados`). Fotos das batidas em
`/dados/fotos/<empresa>`, anexos de atestado em `/dados/anexos/<empresa>`, mídia da página
de vendas em `/dados/midia`.

Toda tabela de negócio tem `empresa_id`. A função `core.da_empresa(db, Modelo, id, usuario)`
busca um registro garantindo que ele é da empresa de quem está logado: **use sempre** ao
receber um id vindo do navegador.

| Tabela | Para que serve |
|---|---|
| `plataforma` | Configuração da revenda (nome, teste grátis, cadastro aberto, WhatsApp, como pagar) |
| `planos` | Planos à venda: preço e limite de funcionários ativos |
| `empresas` | Clientes: local do ponto, regras, jornada, situação da assinatura |
| `usuarios` | Gestores (login por e-mail) e funcionários (login pelo celular); `is_admin` = master |
| `pagamentos`, `asaas_eventos` | Pagamentos (à mão ou Asaas) e avisos do Asaas já processados |
| `escala_fixa` | Regra semanal: o que cada funcionário faz em cada dia da semana |
| `turnos` | Escala de cada data (gerada da escala fixa ou exceção marcada pelo gestor) |
| `batidas` | Entradas e saídas; nunca apagadas (`anulada`), com local, distância, foto, origem |
| `locais`, `totens` | Filiais e tablets |
| `aparelhos` | Aparelho autorizado de cada funcionário |
| `ajustes` | Pedidos de ajuste de ponto |
| `ausencias`, `feriados` | Atestados, férias, licenças; feriados por empresa |
| `ajustes_banco` | Créditos e débitos manuais no banco de horas |
| `fechamentos`, `fechamento_linhas` | Períodos fechados e os totais de cada funcionário |
| `alertas`, `historico` | Avisos ao gestor e trilha de auditoria |
| `tarefas`, `tarefas_feitas` | Checklist diário |
| `pagina_vendas` | Página de vendas (linha 1 = português, 2 = inglês, 3 = espanhol) |
| `push_subs`, `config` | Inscrições de notificação e chaves do push |

**Migração:** não há Alembic. Coluna nova entra na lista `COLUNAS_NOVAS` de `db.py`
(tabela, coluna, definição SQL) e é criada com `ALTER TABLE` na subida (`criar_tabelas`).
Tabela nova é criada sozinha pelo `create_all`. Sempre com valor padrão.

---

## 5. Regras de negócio

### Tempo
- Tudo é gravado em **UTC sem fuso**; a tela converte com o fuso da empresa (`empresas.fuso`).
- O **dia de trabalho vira às 4h** (`regras.VIRADA_H`): a saída da madrugada pertence ao
  dia da entrada.
- Turno com fim menor ou igual ao início termina no dia seguinte.

### Ponto
- `regras.bater()` decide entrada ou saída (a próxima da sequência), confere aparelho, foto
  e distância. Distância pela fórmula de haversine até o local principal ou os locais extras;
  turno com local definido só vale naquele local.
- **Totem:** código de 6 dígitos que muda a cada minuto (HMAC do segredo do totem com o
  minuto); vale o do minuto atual e o anterior. Ponto por PIN no próprio tablet.
- Batida nunca é apagada: fica `anulada` e vai para o histórico.
- O agendador (`main.ciclo_alertas`, a cada 60 s) gera alertas (ponto não batido, saída
  esquecida, intervalo não batido), lembretes ao funcionário, turnos da escala fixa para
  os próximos 14 dias e apaga fotos vencidas (máximo 45 dias).

### Escala fixa
- `escala_fixa` guarda a regra; `regras.gerar_da_fixa()` cria os `turnos` que faltam
  (nunca para trás). Alterar só uma data cria uma exceção (`turnos.fixa = False`).
- Mudar a regra apaga os turnos futuros sem batida daqueles dias da semana.

### Cálculo do período (`apuracao.apurar`)
- **Previsto** = duração do turno menos o intervalo (zero em folga, feriado e ausência).
- Diferença diária dentro da **tolerância** (padrão 10 min) não conta.
- **Falta** = dia de escala sem nenhuma batida; fica fora do saldo.
- **Extras** separadas em dia normal e em folga/feriado, com os percentuais da empresa.
  Sábado e domingo não têm regra própria: valem pela escala. "Folga" é o dia marcado como
  Folga na escala. Dia trabalhado **sem escala** não vira extra: fica pendente
  (`sem_escala`, aviso no Fechamento) até o gestor pôr o horário ou marcar Folga.
- **Adicional noturno:** minutos entre 22h e 5h; com a hora reduzida, cada 52min30s vale
  1 hora (soma 1/7). Não trata a prorrogação após as 5h.
- **Intervalo automático:** quem trabalhou mais de 6 h sem bater o intervalo tem o intervalo
  descontado; com o intervalo obrigatório, também gera aviso.
- **DSR:** semana com falta sem justificativa perde um descanso; reflexo das extras =
  extras com acréscimo ÷ dias úteis × dias de descanso, por funcionário, onde descanso = dias
  marcados como Folga na escala dele + feriados (só no controle por hora extra).
  Informativo: não entra no saldo.
- **Banco de horas:** o saldo final de um fechamento é o saldo anterior do seguinte.
- **Fechar** trava batidas, escala e ausências até a data (`apuracao.esta_fechado`); só o
  último período pode ser reaberto.

### Folha de pagamento (`app/pagamento.py`, `calculo_folha.py`, `revisor.py`)
- Gerada a partir de um **período fechado** (`FolhaPagamento` + um `FolhaItem` por linha do
  fechamento). Dados salariais no funcionário (`salario_centavos`, `jornada_mensal`,
  `dependentes`, `vt_centavos`), copiados para o item; "Recalcular" copia de novo.
- Cálculo exato em Python (centavos, meio centavo para cima): valor da hora = salário ÷
  jornada; extras com o percentual do fechamento; DSR sobre extras; adicional noturno; faltas,
  DSR perdido e atrasos descontados (reduzem as bases). No controle por banco de horas, extras,
  atrasos e DSR sobre extras não entram. INSS progressivo com teto; IRRF pela tabela, com a
  maior dedução entre (INSS + dependentes) e o desconto simplificado, depois o redutor de 2026
  (Lei 15.270/2025) sobre o rendimento tributável; retenção abaixo de R$ 10 não é feita; VT até
  6% do salário, limitado ao custo; FGTS 8% informativo. Lançamentos manuais (proventos
  tributáveis ou não; descontos) por holerite.
- Tabelas no `Config` `tabelas_folha` (padrão em `calculo_folha.TABELAS_PADRAO`), editáveis em
  Master → Folha e Claude; cada folha guarda a cópia das tabelas da época.
- Rascunho → Finalizada (o funcionário vê o holerite em `/app/holerite/<id>`). Reabrir o ponto
  apaga as folhas em rascunho do período e é recusado se houver folha finalizada.
- **Revisão pelo Claude:** `revisor.revisar` roda em segundo plano (BackgroundTasks) e chama a
  API da Anthropic (SDK `anthropic`, modelo `claude-sonnet-5-5`, trocável por `CLAUDE_MODELO`;
  `fallbacks="default"`; resposta em JSON por `output_config.format`). O Claude refaz as
  contas e devolve resumo + alertas (alta/média/baixa), gravados em `folhas_pagamento.revisao`.
  Não altera valores. Chave: `ANTHROPIC_API_KEY` no `.env` ou colada em Master → Folha e Claude
  (tabela `config`, chave `anthropic_api_key`). Limite de 20 revisões por empresa por dia.
  Nomes, horas e valores da folha são enviados à Anthropic.
- Fora do escopo: férias, 13º, rescisão, encargos patronais, eSocial, pensão alimentícia.

### Revenda
- Situação da empresa (`core.situacao`): `ativa`, `teste`, `cortesia`, `vencida`, `bloqueada`.
  Carência de 3 dias após o vencimento.
- Vencida: o gestor só abre a tela Assinatura; o ponto continua sendo registrado.
- Bloqueada: ninguém da empresa entra e o totem para.
- O master é o e-mail de `ADMIN_EMAIL`; sem ele, o gestor mais antigo (empresa cortesia).
- Asaas: cobrança avulsa (30 dias) ou assinatura mensal. Liberação pelo webhook
  `POST /asaas/webhook` (token `ASAAS_WEBHOOK_TOKEN`) ou quando a tela Assinatura consulta a
  cobrança. `externalReference = turno:<id>`: a conta Asaas é dividida com outros sistemas,
  e cobranças de outros sistemas são ignoradas.

---

## 6. Idiomas

- Português é a língua do código: o próprio texto é a chave. `i18n._("Entrar")` devolve
  "Sign in" em inglês. Sem tradução, aparece em português.
- O idioma vem do cookie `idioma` (seletor PT · EN · ES no topo de cada página, rota
  `/idioma/<cod>?volta=...`) ou, na primeira visita, do idioma do navegador.
- **Telas:** textos marcados como `{{ _('texto') }}`; com valores,
  `{{ _('Olá, {v1}!', v1=(nome)) }}`.
- **Mensagens montadas no código** (avisos, erros, alertas gravados no banco) não precisam ser
  marcadas: a tradução reconhece o texto pronto pelo modelo com `{v1}`, `{v2}`... Basta exibir
  com `_(texto)` (já feito para avisos, erros, alertas, histórico e situação).
- **Scripts:** textos marcados como `t("texto")`; as traduções vão para a página em
  `window.T` (só as listadas em `idiomas/_js.json`).
- **Página de vendas:** uma versão por idioma; enquanto a do idioma não for publicada, o
  visitante vê a em português.

### Ao criar ou mudar uma tela
1. Escreva os textos em português normalmente.
2. Rode `tools/i18n_marcar.py --aplicar <arquivo>` para marcar os textos da tela.
   Textos dentro de `{{ }}` ou `{% %}` são marcados à mão com `_()`.
3. Em scripts, rode `tools/i18n_marcar_js.py`.
4. Rode `tools/i18n_extrair.py --faltando en` (e `es`) para ver o que falta traduzir e
   acrescente em `app/idiomas/en.json` e `es.json`. Mantenha os `{v1}` iguais.
5. `tests/test_idiomas.py` falha se faltar alguma tradução ou se um `{v1}` sumir.

Não traduzido de propósito: notificações push (saem sem saber o idioma de quem recebe),
cabeçalhos da planilha do contador, nomes de feriados nacionais, e textos que o gestor
digitou (tarefas, nomes, observações).

---

## 7. Configuração (`/home/turno-app/.env`)

Nunca imprimir nem versionar os valores. Modelo em `.env.example`.

| Variável | Uso |
|---|---|
| `SECRET_KEY` | Assina os cookies de sessão (trocar derruba todos os logins) |
| `APP_NOME` | Nome padrão (o nome exibido vem do Painel Master) |
| `PUBLICAR`, `APP_HOST` | Liga a rota no Traefik para o domínio |
| `APP_REGRA_EXTRA`, `HOSTS_REDIRECIONAR` | Endereços extras que redirecionam ao principal |
| `PUBLIC_URL` | Endereço público (links de convite, webhook do Asaas) |
| `COOKIE_SECURE` | `true` em produção (https) |
| `ADMIN_EMAIL` | Quem é o master |
| `ASAAS_API_KEY`, `ASAAS_WEBHOOK_TOKEN`, `ASAAS_API_URL` | Cobrança |
| `PUSH_CONTATO` | Contato exigido pelo serviço de push |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODELO` | Revisão da folha pelo Claude (opcional; a chave também pode ser colada no Master) |

---

## 8. Publicar (deploy)

Servidor: VPS Hostinger (acesso `ssh vps`), pasta `/home/turno-app`, projeto Docker
`turno-app`, porta só em `127.0.0.1:8096`, Traefik na frente com certificado Let's Encrypt.

1. Rodar todos os testes no computador:
   `for t in tests/test_*.py; do .venv/Scripts/python $t; done`
2. Copiar o banco antes de mudanças de estrutura:
   `docker compose exec -T app python -c "import sqlite3; a=sqlite3.connect('/dados/turno.db'); b=sqlite3.connect('/dados/turno-antes-XXX.db'); a.backup(b)"`
3. Enviar o código: `tar czf - app tests tools | ssh vps 'cd /home/turno-app && tar xzf -'`
4. Reconstruir: `docker compose up -d --build` e conferir `docker compose ps` (healthy).
5. Ao mudar CSS ou scripts, aumentar o número em `style.css?v=NN`, `tour.js?v=N` e o
   `CACHE` do `sw.js`, para os celulares pegarem a versão nova.

Rodar no computador: `.venv/Scripts/python -m uvicorn app.main:app --port 8766`
(`tools/demo.py` cria uma empresa de demonstração no banco local).

---

## 9. Backups e cuidados

- Cópias manuais do banco ficam em `/dados/turno-antes-*.db` (uma antes de cada mudança
  grande). **Não há backup automático para fora do servidor**: recomenda-se criar um.
- Trocar de domínio desloga todos e exige liberar de novo aparelhos e totens (cookies são
  por endereço); o webhook antigo do Asaas deve ser apagado no painel do Asaas.
- Um processo só: não rodar com vários workers (o agendador duplicaria alertas).

---

## 10. Testes

Cada arquivo em `tests/` sobe o app com um banco temporário e relógio controlado
(`banco.agora` é substituído) e percorre o fluxo pelas rotas, como um navegador:

`test_fluxo` (ponta a ponta), `test_fase1` (aparelho, foto, ajustes), `test_fase2`
(fechamento, folha, banco de horas), `test_fase3` (locais, totem, PIN), `test_fase4`
(noturno, intervalo), `test_dsr`, `test_escala_fixa`, `test_revenda`, `test_asaas`
(Asaas simulado), `test_vendas`, `test_idiomas`.

---

## 11. Limites conhecidos

- Não é um registrador de ponto homologado (REP-P, Portaria 671/2021): é controle gerencial.
- A localização vem do navegador; aplicativo de GPS falso consegue burlar (foto e aparelho
  único dificultam).
- PDF da folha e do resumo é pelo "imprimir" do navegador.
- Sem valores em dinheiro (horas apenas), sem recuperação de senha por e-mail (o gestor gera
  novo link), sem adicional noturno na prorrogação após as 5h.
