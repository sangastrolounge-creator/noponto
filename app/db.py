"""Banco de dados (SQLite) e modelos. Toda tabela de negócio carrega empresa_id."""
import os
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, create_engine, event,
                        inspect, text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)

_PADRAO = (Path(__file__).resolve().parent.parent / "dados" / "turno.db").as_posix()
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_PADRAO}")

if DATABASE_URL.startswith("sqlite:///"):
    pasta = os.path.dirname(DATABASE_URL.replace("sqlite:///", "", 1))
    if pasta:
        os.makedirs(pasta, exist_ok=True)

# Pasta dos arquivos (fotos das batidas): ao lado do banco.
PASTA_DADOS = Path(DATABASE_URL.replace("sqlite:///", "", 1)).parent     if DATABASE_URL.startswith("sqlite:///") else Path("dados")
PASTA_FOTOS = PASTA_DADOS / "fotos"
PASTA_ANEXOS = PASTA_DADOS / "anexos"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def agora() -> datetime:
    """UTC sem tzinfo (é assim que o SQLite guarda). Os testes trocam esta função."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Config(Base):
    """Valores da plataforma gerados pelo próprio app (ex.: chaves do aviso push)."""
    __tablename__ = "config"
    chave: Mapped[str] = mapped_column(String(40), primary_key=True)
    valor: Mapped[str] = mapped_column(Text)


class ConversaVenda(Base):
    """Conversa do assistente de vendas do site (Master -> Conversas). Apagada após 180 dias parada."""
    __tablename__ = "conversas_venda"
    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime)
    mensagens: Mapped[str] = mapped_column(Text, default="[]")
    origem: Mapped[str] = mapped_column(String(200), default="")
    ip_hash: Mapped[str] = mapped_column(String(16), default="")
    foi_whatsapp: Mapped[bool] = mapped_column(Boolean, default=False)
    # Contato deixado pela pessoa para o time chamar (com autorização) e acompanhamento da venda.
    nome: Mapped[str] = mapped_column(String(80), default="")
    telefone: Mapped[str] = mapped_column(String(20), default="")
    empresa: Mapped[str] = mapped_column(String(80), default="")
    autorizou: Mapped[bool] = mapped_column(Boolean, default=False)
    contato_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    situacao: Mapped[str] = mapped_column(String(20), default="")  # "", "contatado", "fechou", "perdido"
    # Atendente da equipe que assumiu a conversa no lugar do robô (chat ao vivo).
    atendente_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atendente_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Lead(Base):
    """CRM da plataforma: um possível cliente no funil de vendas."""
    __tablename__ = "crm_leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80), default="")
    empresa_nome: Mapped[str] = mapped_column(String(80), default="")
    telefone: Mapped[str] = mapped_column(String(20), default="", index=True)
    email: Mapped[str] = mapped_column(String(120), default="", index=True)
    funcionarios: Mapped[int] = mapped_column(Integer, default=0)
    origem: Mapped[str] = mapped_column(String(20), default="manual")  # chat | teste | manual | indicacao
    etapa: Mapped[str] = mapped_column(String(20), default="novo", index=True)
    dono_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # atendente; vazio = fila
    conversa_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    empresa_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # conta criada no sistema
    valor_centavos: Mapped[int] = mapped_column(Integer, default=0)  # mensalidade esperada
    motivo_perda: Mapped[str] = mapped_column(String(120), default="")
    autorizou: Mapped[bool] = mapped_column(Boolean, default=False)  # autorizou contato (LGPD)
    criado_em: Mapped[datetime] = mapped_column(DateTime)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime)


class LeadAtividade(Base):
    """Histórico do lead: anotações de ligação/WhatsApp, mudanças de etapa e eventos do sistema."""
    __tablename__ = "crm_atividades"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(Integer, index=True)
    autor_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    tipo: Mapped[str] = mapped_column(String(20), default="nota")  # nota | ligacao | whatsapp | email | etapa | sistema
    texto: Mapped[str] = mapped_column(Text, default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, index=True)


class LeadTarefa(Base):
    """Lembrete de retorno: ligar, mandar proposta, conferir o teste."""
    __tablename__ = "crm_tarefas"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(Integer, index=True)
    dono_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    titulo: Mapped[str] = mapped_column(String(120), default="")
    quando: Mapped[datetime] = mapped_column(DateTime, index=True)
    feita_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime)


class CaixaEmail(Base):
    """Cópia local dos e-mails da caixa de contato (Master -> E-mail), lida por IMAP.
    Os anexos ficam no servidor de e-mail e são baixados na hora (só o nome e o tamanho ficam aqui)."""
    __tablename__ = "caixa_emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    pasta: Mapped[str] = mapped_column(String(12), default="entrada", index=True)  # entrada | enviados | lixeira
    uid: Mapped[int] = mapped_column(Integer, default=0, index=True)      # UID no IMAP (0 = enviado daqui)
    message_id: Mapped[str] = mapped_column(String(300), default="", index=True)
    em_resposta_a: Mapped[str] = mapped_column(String(300), default="")
    referencias: Mapped[str] = mapped_column(Text, default="")
    de_nome: Mapped[str] = mapped_column(String(200), default="")
    de_email: Mapped[str] = mapped_column(String(200), default="", index=True)
    para: Mapped[str] = mapped_column(Text, default="")
    cc: Mapped[str] = mapped_column(Text, default="")
    assunto: Mapped[str] = mapped_column(String(500), default="")
    texto: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    anexos: Mapped[str] = mapped_column(Text, default="[]")   # [{"n": índice, "nome", "tipo", "tam"}]
    data: Mapped[datetime] = mapped_column(DateTime, index=True)
    lido: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    autor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # quem respondeu pela plataforma


class Plataforma(Base):
    """Configuração da revenda (uma linha só, id 1)."""
    __tablename__ = "plataforma"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(60), default="")  # vazio = APP_NOME
    nome_curto: Mapped[str] = mapped_column(String(20), default="")  # embaixo do ícone; vazio = nome
    whatsapp: Mapped[str] = mapped_column(String(20), default="")
    dias_teste: Mapped[int] = mapped_column(Integer, default=7)
    # Carência: dias que a empresa ainda usa depois do vencimento (assinatura) e do fim do teste.
    carencia_dias: Mapped[int] = mapped_column(Integer, default=3)
    carencia_teste_dias: Mapped[int] = mapped_column(Integer, default=0)
    cadastro_aberto: Mapped[bool] = mapped_column(Boolean, default=True)
    instrucoes_pagamento: Mapped[str] = mapped_column(Text, default="")
    titulo: Mapped[str] = mapped_column(String(200), default="")
    subtitulo: Mapped[str] = mapped_column(String(600), default="")
    # Dados legais que aparecem nos Termos de Uso e na Política de Privacidade.
    razao_social: Mapped[str] = mapped_column(String(160), default="")  # vazio = nome da plataforma
    endereco: Mapped[str] = mapped_column(String(300), default="")
    email_legal: Mapped[str] = mapped_column(String(200), default="")
    estado_lei: Mapped[str] = mapped_column(String(40), default="")  # foro (cidade/UF)
    # Telas de Entrar e Criar conta: cores, textos e imagens trocados no Painel Master.
    entrada_cor1: Mapped[str] = mapped_column(String(7), default="#4f46e5")
    entrada_cor2: Mapped[str] = mapped_column(String(7), default="#0ea5e9")
    entrada_titulo: Mapped[str] = mapped_column(String(120), default="")
    entrada_texto: Mapped[str] = mapped_column(String(300), default="")
    entrada_imagens: Mapped[str] = mapped_column(Text, default="")  # nomes separados por vírgula
    pais_servidores: Mapped[str] = mapped_column(String(60), default="Brasil")
    # Plano anual: quantas mensalidades a empresa paga por 12 meses (10 = 2 meses grátis).
    meses_anual: Mapped[int] = mapped_column(Integer, default=10)


class PaginaVendas(Base):
    """Página de vendas longa (linha id=1): rascunho e versão publicada, em JSON."""
    __tablename__ = "pagina_vendas"
    id: Mapped[int] = mapped_column(primary_key=True)
    rascunho: Mapped[str | None] = mapped_column(Text, nullable=True)
    publicado: Mapped[str | None] = mapped_column(Text, nullable=True)
    atualizado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    publicado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Plano(Base):
    __tablename__ = "planos"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(60))
    preco_centavos: Mapped[int] = mapped_column(Integer, default=0)
    limite_funcionarios: Mapped[int] = mapped_column(Integer, default=0)  # 0 = ilimitado
    descricao: Mapped[str] = mapped_column(Text, default="")  # um benefício por linha
    destaque: Mapped[bool] = mapped_column(Boolean, default=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)  # à venda
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    limite_locais: Mapped[int] = mapped_column(Integer, default=0)  # locais de ponto, contando o principal; 0 = ilimitado
    com_pagamento: Mapped[bool] = mapped_column(Boolean, default=True)  # tela Pagamento e revisão do Claude
    com_alertas: Mapped[bool] = mapped_column(Boolean, default=True)  # alertas de atraso e de não comparecimento
    com_gps: Mapped[bool] = mapped_column(Boolean, default=True)      # ponto conferido pela localização (GPS)
    com_foto: Mapped[bool] = mapped_column(Boolean, default=True)     # foto (selfie) ao bater o ponto
    com_facial: Mapped[bool] = mapped_column(Boolean, default=True)   # reconhecimento facial no ponto
    com_cerca: Mapped[bool] = mapped_column(Boolean, default=True)    # cerca desenhada e conferência no turno
    # Cobrança por pessoa (opcional): base + valor por pessoa que trabalhou nos últimos 30 dias,
    # nunca abaixo do mínimo. Com valor por pessoa = 0, vale o preço fixo (preco_centavos).
    # Num plano por pessoa, preco_centavos guarda o menor valor possível (para ordenar e mostrar).
    base_centavos: Mapped[int] = mapped_column(Integer, default=0)
    por_pessoa_centavos: Mapped[int] = mapped_column(Integer, default=0)
    minimo_centavos: Mapped[int] = mapped_column(Integer, default=0)
    # Faixas: planos com o mesmo grupo são faixas de um só produto. A empresa vai sozinha para a
    # faixa que fica mais barata para o número de pessoas dela (e nunca paga mais que a seguinte).
    grupo: Mapped[str] = mapped_column(String(30), default="")
    # Pessoas a mais aceitas além do limite antes de exigir o plano seguinte.
    tolerancia: Mapped[int] = mapped_column(Integer, default=0)
    # Desconto por volume na cobrança por pessoa: "50:15;150:30" = 15% acima de 50 pessoas, 30% acima de 150.
    descontos_volume: Mapped[str] = mapped_column(String(80), default="")
    # Recursos liberados no plano (além dos de cima). No teste vale tudo.
    com_escala: Mapped[bool] = mapped_column(Boolean, default=True)      # escala, trocas e turnos abertos
    com_fechamento: Mapped[bool] = mapped_column(Boolean, default=True)  # fechamento, banco de horas e relatórios
    com_rh: Mapped[bool] = mapped_column(Boolean, default=True)          # dados de RH, férias e desligamento
    com_totem: Mapped[bool] = mapped_column(Boolean, default=True)       # ponto no tablet (totem)
    com_gerentes: Mapped[bool] = mapped_column(Boolean, default=True)    # gerentes e setores
    com_cmo: Mapped[bool] = mapped_column(Boolean, default=True)         # faturamento e custo de mão de obra

    @property
    def limite_efetivo(self) -> int:
        """Limite de funcionários com a tolerância (0 = ilimitado)."""
        return (self.limite_funcionarios + (self.tolerancia or 0)) if self.limite_funcionarios else 0

    def _desconto_volume(self, pessoas: int) -> int:
        pct = 0
        for parte in (self.descontos_volume or "").replace(",", ";").split(";"):
            try:
                acima, p = (int(x) for x in parte.split(":"))
            except ValueError:
                continue
            if pessoas > acima and 0 <= p <= 90:
                pct = max(pct, p)
        return pct

    @property
    def gratis(self) -> bool:
        return self.preco_centavos == 0

    @property
    def por_pessoa(self) -> bool:
        return (self.por_pessoa_centavos or 0) > 0

    def valor_mensal(self, pessoas: int) -> int:
        if not self.por_pessoa:
            return self.preco_centavos or 0
        pessoas_valor = max(0, pessoas) * self.por_pessoa_centavos
        pct = self._desconto_volume(pessoas)
        if pct:
            pessoas_valor = round(pessoas_valor * (100 - pct) / 100)
        return max(self.minimo_centavos or 0, (self.base_centavos or 0) + pessoas_valor)


class Empresa(Base):
    __tablename__ = "empresas"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    endereco: Mapped[str] = mapped_column(String(300), default="")
    logo: Mapped[str] = mapped_column(String(60), default="")  # arquivo em dados/logos (impressos)
    facial_ativo: Mapped[bool] = mapped_column(Boolean, default=False)  # reconhecimento facial no ponto
    # O dono pediu o encerramento da conta e a exclusão dos dados (a plataforma conclui).
    exclusao_pedida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Local onde o ponto é válido. Sem lat/lng o ponto é aceito sem conferência.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    raio_m: Mapped[int] = mapped_column(Integer, default=150)
    bloquear_fora: Mapped[bool] = mapped_column(Boolean, default=True)
    # Ponto só no local de lotação de cada pessoa (o da ficha: principal ou uma filial), salvo quando o
    # dia da escala aponta outro local. Desligado, vale qualquer endereço cadastrado da empresa.
    ponto_na_lotacao: Mapped[bool] = mapped_column(Boolean, default=True)
    tolerancia_min: Mapped[int] = mapped_column(Integer, default=10)
    fuso: Mapped[str] = mapped_column(String(40), default="America/Sao_Paulo")
    # --- confiança no ponto ---
    exigir_foto: Mapped[bool] = mapped_column(Boolean, default=False)
    aparelho_unico: Mapped[bool] = mapped_column(Boolean, default=True)
    ajuste_dias: Mapped[int] = mapped_column(Integer, default=7)   # até quantos dias atrás o funcionário pede ajuste
    fotos_dias: Mapped[int] = mapped_column(Integer, default=45)   # depois disso a foto é apagada
    # --- jornada e banco de horas ---
    controle: Mapped[str] = mapped_column(String(8), default="banco")  # banco | extra
    intervalo_padrao_min: Mapped[int] = mapped_column(Integer, default=60)
    # Desconta o intervalo de quem trabalhou mais de 6 h e não bateu a saída para o intervalo.
    intervalo_auto: Mapped[bool] = mapped_column(Boolean, default=True)
    # Diferença diária de até X minutos não conta como extra nem como atraso.
    tolerancia_dia_min: Mapped[int] = mapped_column(Integer, default=10)
    # Acréscimo (%) da hora extra em dia normal e em folga/feriado.
    pct_extra_normal: Mapped[int] = mapped_column(Integer, default=50)
    pct_extra_especial: Mapped[int] = mapped_column(Integer, default=100)
    # Adicional noturno (22h às 5h) e hora noturna reduzida (52min30s valem 1 hora).
    pct_noturno: Mapped[int] = mapped_column(Integer, default=20)
    noturno_reduzida: Mapped[bool] = mapped_column(Boolean, default=True)
    # O funcionário precisa bater a saída e a volta do intervalo.
    intervalo_obrigatorio: Mapped[bool] = mapped_column(Boolean, default=True)
    # Intervalo mínimo de quem trabalha mais de 6 h (art. 71): 60 min, ou de 30 a 59 se a
    # convenção ou o acordo coletivo reduzir (art. 611-A, III). O que faltar é indenizado (+50%).
    intervalo_minimo_min: Mapped[int] = mapped_column(Integer, default=60)
    # DSR (descanso semanal remunerado): desconto na semana com falta e reflexo das horas extras.
    dsr_faltas: Mapped[bool] = mapped_column(Boolean, default=True)
    dsr_extras: Mapped[bool] = mapped_column(Boolean, default=True)
    # Turnos oferecidos (abertos e repassados): o gestor precisa aprovar antes de valer.
    trocas_aprovacao: Mapped[bool] = mapped_column(Boolean, default=True)
    # Funcionários podem publicar no mural (o gestor sempre pode).
    mural_equipe: Mapped[bool] = mapped_column(Boolean, default=True)
    # Regras do ponto (Portaria 671: nada de bloqueio de horário nem marcação automática):
    # entrada feita mais de X min antes da escala fica sinalizada para o gestor decidir (0 = desligado);
    # X min depois do fim, quem esqueceu a saída recebe lembrete e o gestor um alerta (0 = 60 min).
    entrada_antecipada_min: Mapped[int] = mapped_column(Integer, default=0)
    saida_auto_min: Mapped[int] = mapped_column(Integer, default=0)
    # Avisa o gestor quando faltam X min para acabar a jornada do dia (hora extra no Brasil é diária; 0 = desligado).
    aviso_extra_min: Mapped[int] = mapped_column(Integer, default=30)
    # Pergunta ao funcionário "como foi o turno?" (1 a 5) quando ele bate a saída.
    avaliar_turno: Mapped[bool] = mapped_column(Boolean, default=True)
    # Configuração guiada (triagem): respostas e o que foi preparado a partir delas.
    segmento: Mapped[str] = mapped_column(String(20), default="")
    # Faturamento mensal e CMO (opcional): liga o bloco na tela Hoje; meta em % e regime tributário
    # (o regime muda os encargos estimados sobre a folha).
    usa_faturamento: Mapped[bool] = mapped_column(Boolean, default=False)
    meta_cmo: Mapped[int] = mapped_column(Integer, default=30)
    regime_tributario: Mapped[str] = mapped_column(String(12), default="simples")
    uf: Mapped[str] = mapped_column(String(2), default="")
    porte: Mapped[str] = mapped_column(String(10), default="")
    provedor_folha: Mapped[str] = mapped_column(String(20), default="")
    abre: Mapped[str] = mapped_column(String(5), default="")
    fecha: Mapped[str] = mapped_column(String(5), default="")
    funcoes_padrao: Mapped[str] = mapped_column(Text, default="[]")   # [{nome, gorjeta}]
    modelos_turno: Mapped[str] = mapped_column(Text, default="[]")    # [{nome, inicio, fim}]
    triagem_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    triagem_resumo: Mapped[str] = mapped_column(Text, default="[]")
    # Confirmação de turno: pedir ao funcionário que confirme os turnos publicados; quantas horas
    # antes do início cobrar; e se o turno não confirmado é oferecido à equipe.
    confirmar_turno: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmar_antes_h: Mapped[int] = mapped_column(Integer, default=24)
    oferecer_sem_confirmacao: Mapped[bool] = mapped_column(Boolean, default=False)
    # Trava a escala: dias passados há mais de N dias não podem mais ser mudados (0 = sem trava).
    travar_escala_dias: Mapped[int] = mapped_column(Integer, default=0)
    # Cerca virtual: contorno do local principal ([[lat, lng], ...]; vazio = raio), pedir o motivo
    # de quem bate fora da área e conferir a área com o app aberto durante o turno.
    cerca: Mapped[str] = mapped_column(Text, default="")
    motivo_fora: Mapped[bool] = mapped_column(Boolean, default=True)
    cerca_turno: Mapped[bool] = mapped_column(Boolean, default=False)
    cerca_turno_min: Mapped[int] = mapped_column(Integer, default=10)
    # Piso salarial mensal (220 h) da categoria ou estadual; 0 = vale o mínimo nacional.
    minimo_proprio_centavos: Mapped[int] = mapped_column(Integer, default=0)
    # --- revenda: assinatura da empresa ---
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)  # False = bloqueada pelo master
    plano_id: Mapped[int | None] = mapped_column(ForeignKey("planos.id"), nullable=True)
    teste_ate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pago_ate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    isenta: Mapped[bool] = mapped_column(Boolean, default=False)  # cortesia: nunca vence
    obs_master: Mapped[str] = mapped_column(Text, default="")
    cpf_cnpj: Mapped[str] = mapped_column(String(20), default="")  # exigido pelo Asaas
    asaas_cliente_id: Mapped[str] = mapped_column(String(40), default="")
    asaas_assinatura_id: Mapped[str] = mapped_column(String(40), default="")
    # Plano por pessoa: quantas pessoas trabalharam nos últimos 30 dias e o valor que vai na próxima
    # fatura (conferidos uma vez por dia).
    pessoas_cobradas: Mapped[int] = mapped_column(Integer, default=0)
    valor_assinatura_centavos: Mapped[int] = mapped_column(Integer, default=0)
    valor_conferido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Ciclo da assinatura: mensal ou anual (paga as mensalidades de Plataforma.meses_anual por 12 meses).
    ciclo: Mapped[str] = mapped_column(String(8), default="mensal")
    # Cupom de desconto (vale a partir de cupom_desde, pelos meses do cupom ou para sempre).
    cupom_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cupom_desde: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Indique e ganhe: código do link desta empresa, quem indicou e se o mês grátis já foi dado.
    codigo_indicacao: Mapped[str] = mapped_column(String(12), default="")
    indicada_por_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indicacao_premiada: Mapped[bool] = mapped_column(Boolean, default=False)
    # Portaria 671 (REP-P): CNO ou CAEPF, CPF do responsável pelas alterações cadastrais e local
    # da prestação do serviço que vai no comprovante (vazio = endereço da empresa).
    rep_cno: Mapped[str] = mapped_column(String(14), default="")
    rep_cpf_resp: Mapped[str] = mapped_column(String(11), default="")
    rep_local: Mapped[str] = mapped_column(String(100), default="")
    # Parceiro (contador ou revenda) que trouxe a empresa e se ele pode baixar os relatórios de ponto.
    parceiro_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    contador_acesso: Mapped[bool] = mapped_column(Boolean, default=False)
    plano: Mapped[Plano | None] = relationship(lazy="joined")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Usuario(Base):
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    nome: Mapped[str] = mapped_column(String(120))
    cargo: Mapped[str] = mapped_column(String(80), default="")
    # Gestor entra com e-mail; funcionário entra com o celular.
    email: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    celular: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    senha_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    papel: Mapped[str] = mapped_column(String(12), default="funcionario")  # gestor | funcionario
    na_escala: Mapped[bool] = mapped_column(Boolean, default=True)  # aparece na escala e bate ponto
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Link de primeiro acesso (o funcionário cria a própria senha).
    convite_token: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    # PIN para bater o ponto no totem (tablet da empresa). Bloqueia após 5 erros seguidos.
    pin_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pin_erros: Mapped[int] = mapped_column(Integer, default=0)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # dono da plataforma (master)
    # Equipe da plataforma (suporte, call center): áreas do Master liberadas, separadas por vírgula.
    master_areas: Mapped[str] = mapped_column(String(120), default="")
    guia_visto: Mapped[bool] = mapped_column(Boolean, default=False)  # já viu o convite do guia
    # Dados para a folha de pagamento (valores em centavos).
    salario_centavos: Mapped[int] = mapped_column(Integer, default=0)
    jornada_mensal: Mapped[int] = mapped_column(Integer, default=220)  # horas/mês do valor-hora
    dependentes: Mapped[int] = mapped_column(Integer, default=0)       # para o IRRF
    vt_centavos: Mapped[int] = mapped_column(Integer, default=0)       # custo do vale-transporte no mês
    # Gestores: o dono (quem criou a conta) pode tudo; os outros gerentes só o que estiver em
    # `permissoes` (chaves de core.PERMISSOES separadas por vírgula).
    dono: Mapped[bool] = mapped_column(Boolean, default=False)
    permissoes: Mapped[str] = mapped_column(String(300), default="")
    # Gerente com alcance limitado: ids de setores e de locais ("0" = endereço principal),
    # separados por vírgula. Vazio = sem limite naquela dimensão.
    alcance_setores: Mapped[str] = mapped_column(String(300), default="")
    alcance_locais: Mapped[str] = mapped_column(String(300), default="")
    # "Esqueci a senha": hash do link enviado por e-mail e até quando ele vale.
    redefinir_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    redefinir_ate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Recebe gorjeta (Lei 13.419/2017: integra a remuneração).
    gorjeta: Mapped[bool] = mapped_column(Boolean, default=False)  # funcionário que recebe gorjeta
    # Ficha: tipo de contrato (clt | aprendiz | intermitente | estagio | pj), admissão e local principal.
    classificacao: Mapped[str] = mapped_column(String(12), default="clt")
    data_admissao: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Vigência: primeiro dia em que o ponto pode ser registrado no Data Worke (migração, treinamento).
    ponto_desde: Mapped[date | None] = mapped_column(Date, nullable=True)
    # A própria pessoa excluiu a conta de acesso (registros trabalhistas ficam guardados).
    conta_excluida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Desligamento: último dia de trabalho, tipo (desligamento.TIPOS), aviso prévio e observação.
    desligado_em: Mapped[date | None] = mapped_column(Date, nullable=True)
    desligamento_tipo: Mapped[str] = mapped_column(String(20), default="")
    aviso_previo: Mapped[str] = mapped_column(String(16), default="")
    desligamento_obs: Mapped[str] = mapped_column(String(300), default="")
    # Reconhecimento facial (LGPD art. 11, I): consentimento "" | aceito | recusado, e o modelo
    # matemático do rosto (128 números, nunca a foto). Apagado ao revogar ou no desligamento.
    facial_consentimento: Mapped[str] = mapped_column(String(10), default="")
    facial_consentido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    facial_descritor: Mapped[str] = mapped_column(Text, default="")
    # Só para menores de 18 (regras de trabalho de menores); vazio para os demais.
    nascimento: Mapped[date | None] = mapped_column(Date, nullable=True)
    local_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    setor_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Consentimento do funcionário para localização (e foto) ao bater o ponto.
    consentimento_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mural_visto_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Aceite dos Termos de Uso e da Política de Privacidade (data e versão aceita).
    termos_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    termos_versao: Mapped[str] = mapped_column(String(20), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    empresa: Mapped[Empresa] = relationship(lazy="joined")

    @property
    def gestor(self) -> bool:
        return self.papel == "gestor"

    @property
    def primeiro_nome(self) -> str:
        return self.nome.strip().split(" ")[0]

    @property
    def areas_master(self) -> set[str]:
        """Áreas do Master que esta pessoa acessa (o dono da plataforma acessa todas)."""
        if self.is_admin:
            return {"crm", "supervisao", "suporte", "financeiro", "config", "email"}
        return {a for a in (self.master_areas or "").split(",") if a}

    @property
    def acesso_master(self) -> bool:
        return bool(self.areas_master)


class AsaasEvento(Base):
    """Avisos do Asaas já processados (ele entrega "ao menos uma vez")."""
    __tablename__ = "asaas_eventos"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    evento: Mapped[str] = mapped_column(String(40), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Pagamento(Base):
    """Pagamento registrado à mão pelo master ou cobrança do Asaas."""
    __tablename__ = "pagamentos"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    plano_nome: Mapped[str] = mapped_column(String(60), default="")
    valor_centavos: Mapped[int] = mapped_column(Integer, default=0)
    dias: Mapped[int] = mapped_column(Integer, default=30)
    forma: Mapped[str] = mapped_column(String(20), default="pix")
    obs: Mapped[str] = mapped_column(String(300), default="")
    registrado_por: Mapped[str] = mapped_column(String(200), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, index=True)
    plano_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo: Mapped[str] = mapped_column(String(12), default="manual")   # manual | avulso | assinatura
    # pago | pendente | vencido | cancelado | estornado
    status: Mapped[str] = mapped_column(String(12), default="pago")
    pago_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    asaas_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    fatura_url: Mapped[str] = mapped_column(String(300), default="")


class RegistroRep(Base):
    """Armazenamento de Registro de Ponto (ARP) do REP-P, Portaria MTP 671/2021, Anexo IX.
    Nunca é alterado nem apagado. NSR sequencial por empresa (estabelecimento). Tipos do AFD:
    2 = identificação do empregador, 5 = inclusão/alteração/exclusão de empregado,
    6 = evento sensível (07 serviço disponível, 08 indisponível), 7 = marcação de ponto."""
    __tablename__ = "rep_registros"
    __table_args__ = (UniqueConstraint("empresa_id", "nsr", name="uq_rep_nsr"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    nsr: Mapped[int] = mapped_column(Integer)
    tipo: Mapped[int] = mapped_column(Integer)
    gravado_em: Mapped[datetime] = mapped_column(DateTime, index=True)   # UTC
    marcacao_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # UTC (tipo 7)
    cpf: Mapped[str] = mapped_column(String(11), default="")
    coletor: Mapped[str] = mapped_column(String(2), default="")
    offline: Mapped[str] = mapped_column(String(1), default="0")
    hash: Mapped[str] = mapped_column(String(64), default="")
    batida_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dados: Mapped[str] = mapped_column(Text, default="{}")


class Cupom(Base):
    """Cupom de desconto na assinatura (ex.: FUNDADORES, 30% para sempre, 100 usos)."""
    __tablename__ = "cupons"
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(30), unique=True)
    descricao: Mapped[str] = mapped_column(String(200), default="")
    pct: Mapped[int] = mapped_column(Integer, default=10)          # % de desconto
    meses: Mapped[int] = mapped_column(Integer, default=0)         # por quantos meses; 0 = para sempre
    max_usos: Mapped[int] = mapped_column(Integer, default=0)      # 0 = sem limite
    usos: Mapped[int] = mapped_column(Integer, default=0)
    valido_ate: Mapped[date | None] = mapped_column(Date, nullable=True)  # último dia para usar
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Parceiro(Base):
    """Contador ou revenda: traz empresas pelo link e ganha comissão sobre o que elas pagam."""
    __tablename__ = "parceiros"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    escritorio: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(200), unique=True)
    celular: Mapped[str] = mapped_column(String(20), default="")
    senha_hash: Mapped[str] = mapped_column(String(200), default="")
    cpf_cnpj: Mapped[str] = mapped_column(String(20), default="")
    chave_pix: Mapped[str] = mapped_column(String(120), default="")  # para receber as comissões
    codigo: Mapped[str] = mapped_column(String(12), unique=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    nivel_manual: Mapped[int] = mapped_column(Integer, default=0)    # 0 = automático; 1 a 5 fixa o nível
    # Marca própria (white-label): liberada pelo master; vale no domínio do parceiro.
    marca_liberada: Mapped[bool] = mapped_column(Boolean, default=False)
    marca_nome: Mapped[str] = mapped_column(String(60), default="")
    marca_logo: Mapped[str] = mapped_column(String(60), default="")
    marca_cor: Mapped[str] = mapped_column(String(7), default="")
    dominio: Mapped[str] = mapped_column(String(120), default="", index=True)
    aceite_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Comissao(Base):
    """Comissão do parceiro sobre um pagamento confirmado de uma empresa dele."""
    __tablename__ = "comissoes"
    id: Mapped[int] = mapped_column(primary_key=True)
    parceiro_id: Mapped[int] = mapped_column(ForeignKey("parceiros.id"), index=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    pagamento_id: Mapped[int] = mapped_column(Integer, unique=True)
    valor_pago_centavos: Mapped[int] = mapped_column(Integer, default=0)
    pct: Mapped[int] = mapped_column(Integer, default=0)
    valor_centavos: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="a_pagar")  # a_pagar | paga | cancelada
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    paga_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Local(Base):
    """Local extra onde o ponto vale (filial, obra, evento). O principal fica na empresa."""
    __tablename__ = "locais"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    nome: Mapped[str] = mapped_column(String(80))
    endereco: Mapped[str] = mapped_column(String(300), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    raio_m: Mapped[int] = mapped_column(Integer, default=150)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Cerca desenhada no mapa: [[lat, lng], ...]. Vazia = vale o raio.
    cerca: Mapped[str] = mapped_column(Text, default="")


class Setor(Base):
    """Setor da empresa (Cozinha, Salão, Caixa...). Cada pessoa pode estar em um setor."""
    __tablename__ = "setores"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    nome: Mapped[str] = mapped_column(String(60))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)


class Totem(Base):
    """Tablet fixo da empresa: mostra um código que muda a cada minuto e aceita ponto por PIN."""
    __tablename__ = "totens"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    local_id: Mapped[int | None] = mapped_column(ForeignKey("locais.id"), nullable=True)
    nome: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), index=True)  # do link de ativação
    segredo: Mapped[str] = mapped_column(String(64))                 # gera o código do minuto
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    visto_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    empresa: Mapped[Empresa] = relationship()
    local: Mapped[Local | None] = relationship()


class Turno(Base):
    """Um dia da escala de um funcionário: horário de trabalho ou folga."""
    __tablename__ = "turnos"
    __table_args__ = (UniqueConstraint("usuario_id", "data"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    inicio: Mapped[str] = mapped_column(String(5), default="")  # HH:MM
    fim: Mapped[str] = mapped_column(String(5), default="")     # menor que o início = termina no dia seguinte
    folga: Mapped[bool] = mapped_column(Boolean, default=False)
    obs: Mapped[str] = mapped_column(String(200), default="")
    intervalo_min: Mapped[int] = mapped_column(Integer, default=0)  # intervalo não remunerado
    lembrou_ini: Mapped[bool] = mapped_column(Boolean, default=False)  # lembrete de entrada enviado
    lembrou_fim: Mapped[bool] = mapped_column(Boolean, default=False)
    # Hora combinada para sair para o intervalo ("" = o funcionário escolhe).
    intervalo_inicio: Mapped[str] = mapped_column(String(5), default="")
    lembrou_int: Mapped[bool] = mapped_column(Boolean, default=False)
    # Local extra onde a pessoa trabalha nesse dia. Vazio = o local da ficha (ou qualquer um, sem a
    # regra de lotação). so_principal = nesse dia trabalha no endereço principal (matriz).
    local_id: Mapped[int | None] = mapped_column(ForeignKey("locais.id"), nullable=True)
    so_principal: Mapped[bool] = mapped_column(Boolean, default=False)
    # True = veio da escala fixa e não foi alterado só para este dia.
    fixa: Mapped[bool] = mapped_column(Boolean, default=False)
    # Confirmação do funcionário ("vou estar lá") e se o lembrete de confirmar já foi enviado.
    confirmado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lembrou_conf: Mapped[bool] = mapped_column(Boolean, default=False)
    usuario: Mapped[Usuario] = relationship()
    local: Mapped[Local | None] = relationship()


class EscalaFixa(Base):
    """Escala permanente: o que o funcionário faz em cada dia da semana, toda semana.
    Os turnos de cada data são criados a partir daqui, sozinhos."""
    __tablename__ = "escala_fixa"
    __table_args__ = (UniqueConstraint("usuario_id", "dia_semana"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    dia_semana: Mapped[int] = mapped_column(Integer)  # 0 = segunda
    inicio: Mapped[str] = mapped_column(String(5), default="")
    fim: Mapped[str] = mapped_column(String(5), default="")
    folga: Mapped[bool] = mapped_column(Boolean, default=False)
    intervalo_min: Mapped[int] = mapped_column(Integer, default=0)
    intervalo_inicio: Mapped[str] = mapped_column(String(5), default="")
    local_id: Mapped[int | None] = mapped_column(ForeignKey("locais.id"), nullable=True)
    so_principal: Mapped[bool] = mapped_column(Boolean, default=False)
    obs: Mapped[str] = mapped_column(String(200), default="")


class Batida(Base):
    __tablename__ = "batidas"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(8))  # entrada | saida
    momento: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    # Dia de trabalho a que a batida pertence (a saída da madrugada fica no dia da entrada).
    dia: Mapped[date] = mapped_column(Date, index=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    precisao_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distancia_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dentro: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None = não conferido
    turno_id: Mapped[int | None] = mapped_column(
        ForeignKey("turnos.id", ondelete="SET NULL"), nullable=True, index=True)
    atraso_min: Mapped[int] = mapped_column(Integer, default=0)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)  # lançada pelo gestor
    obs: Mapped[str] = mapped_column(String(200), default="")
    foto: Mapped[str] = mapped_column(String(80), default="")  # nome do arquivo em PASTA_FOTOS/<empresa>
    # Batida nunca é apagada: fica anulada, com quem anulou e quando.
    anulada: Mapped[bool] = mapped_column(Boolean, default=False)
    anulada_por: Mapped[str] = mapped_column(String(120), default="")
    anulada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    local_nome: Mapped[str] = mapped_column(String(80), default="")  # local extra ou totem
    origem: Mapped[str] = mapped_column(String(8), default="")  # "" celular+GPS | codigo | totem | auto
    conferida: Mapped[bool] = mapped_column(Boolean, default=True)  # False = para o gestor conferir (entrada antecipada; saída automática antiga)
    # Reconhecimento facial: "ok" (rosto conferiu), "diverge" (não conferiu), "sem" (não fez), "" (não usa)
    facial: Mapped[str] = mapped_column(String(10), default="")
    facial_dist: Mapped[float | None] = mapped_column(Float, nullable=True)
    motivo_fora: Mapped[str] = mapped_column(String(200), default="")  # por que bateu fora da área
    usuario: Mapped[Usuario] = relationship()


class Tarefa(Base):
    """Item do checklist diário. Sem usuario_id vale para a equipe toda (basta um fazer)."""
    __tablename__ = "tarefas"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    titulo: Mapped[str] = mapped_column(String(160))
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    dias: Mapped[str] = mapped_column(String(7), default="0123456")  # 0 = segunda
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    usuario: Mapped[Usuario | None] = relationship()


class TarefaFeita(Base):
    __tablename__ = "tarefas_feitas"
    __table_args__ = (UniqueConstraint("tarefa_id", "data"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    tarefa_id: Mapped[int] = mapped_column(ForeignKey("tarefas.id", ondelete="CASCADE"))
    data: Mapped[date] = mapped_column(Date, index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    feito_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class Alerta(Base):
    __tablename__ = "alertas"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    turno_id: Mapped[int | None] = mapped_column(
        ForeignKey("turnos.id", ondelete="SET NULL"), nullable=True, index=True)
    # sem_ponto | atraso | fora_area | sem_saida | saida_antecipada | sem_escala | sem_intervalo
    tipo: Mapped[str] = mapped_column(String(20))
    texto: Mapped[str] = mapped_column(String(300))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, index=True)
    lido: Mapped[bool] = mapped_column(Boolean, default=False)


class PushSub(Base):
    """Aparelho de um gestor que aceitou receber avisos."""
    __tablename__ = "push_subs"
    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(600), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Aparelho(Base):
    """Celular/navegador de um funcionário (identificado por um cookie de longa duração)."""
    __tablename__ = "aparelhos"
    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    nome: Mapped[str] = mapped_column(String(80), default="")
    situacao: Mapped[str] = mapped_column(String(12), default="autorizado")  # autorizado | pendente | recusado
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    ultimo_uso: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    usuario: Mapped[Usuario] = relationship()


class Ajuste(Base):
    """Pedido do funcionário para lançar uma batida que ele esqueceu."""
    __tablename__ = "ajustes"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(8))  # entrada | saida
    data: Mapped[date] = mapped_column(Date)
    hora: Mapped[str] = mapped_column(String(5))
    motivo: Mapped[str] = mapped_column(String(300))
    situacao: Mapped[str] = mapped_column(String(10), default="pendente")  # pendente | aprovado | recusado
    resposta: Mapped[str] = mapped_column(String(300), default="")
    decidido_por: Mapped[str] = mapped_column(String(120), default="")
    decidido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class Historico(Base):
    """Trilha de auditoria: tudo o que o gestor altera no ponto fica registrado aqui."""
    __tablename__ = "historico"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    autor: Mapped[str] = mapped_column(String(120))
    acao: Mapped[str] = mapped_column(String(40))
    detalhe: Mapped[str] = mapped_column(String(500))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, index=True)
    # Pessoa e dia afetados (quando a alteração é num dia de ponto): o Ajuste de ponto filtra por eles.
    usuario_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dia: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)


class Feriado(Base):
    __tablename__ = "feriados"
    __table_args__ = (UniqueConstraint("empresa_id", "data"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    nome: Mapped[str] = mapped_column(String(80))


class Ausencia(Base):
    """Atestado, férias, licença ou afastamento: dias em que a pessoa não deve trabalhar."""
    __tablename__ = "ausencias"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(12))
    inicio: Mapped[date] = mapped_column(Date)
    fim: Mapped[date] = mapped_column(Date)
    obs: Mapped[str] = mapped_column(String(300), default="")
    anexo: Mapped[str] = mapped_column(String(80), default="")  # arquivo em PASTA_ANEXOS/<empresa>
    situacao: Mapped[str] = mapped_column(String(10), default="aprovado")  # pendente | aprovado | recusado
    abono_dias: Mapped[int] = mapped_column(Integer, default=0)  # férias: dias vendidos (abono pecuniário)
    criado_por: Mapped[str] = mapped_column(String(120), default="")
    decidido_por: Mapped[str] = mapped_column(String(120), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class Banner(Base):
    """Banner promocional que o Master publica no topo da tela Hoje de todas as empresas."""
    __tablename__ = "banners"
    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str] = mapped_column(String(90))
    texto: Mapped[str] = mapped_column(String(240), default="")
    link: Mapped[str] = mapped_column(String(300), default="")       # https://... ou /painel/...
    botao: Mapped[str] = mapped_column(String(40), default="")       # texto do botão do link
    imagem: Mapped[str] = mapped_column(String(400), default="")     # https://... (opcional, fundo)
    cor: Mapped[str] = mapped_column(String(12), default="azul")     # azul, verde, roxo, laranja
    inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    fim: Mapped[date | None] = mapped_column(Date, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Sobreaviso(Base):
    """Horas em que a pessoa ficou à disposição fora do expediente, aguardando ser chamada
    (CLT art. 244, §2º; Súmula 428 do TST). Pagas à razão de 1/3 do valor da hora normal."""
    __tablename__ = "sobreavisos"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    dia: Mapped[date] = mapped_column(Date, index=True)
    inicio: Mapped[str] = mapped_column(String(5))      # HH:MM
    fim: Mapped[str] = mapped_column(String(5))         # menor que o início = vai até o dia seguinte
    minutos: Mapped[int] = mapped_column(Integer, default=0)
    obs: Mapped[str] = mapped_column(String(200), default="")
    criado_por: Mapped[str] = mapped_column(String(120), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class MotivoAjuste(Base):
    """Justificativa do gestor para um dia de ponto (por que foi ajustado ou abonado)."""
    __tablename__ = "motivos_ajuste"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    dia: Mapped[date] = mapped_column(Date, index=True)
    motivo: Mapped[str] = mapped_column(String(60))
    obs: Mapped[str] = mapped_column(String(300), default="")
    criado_por: Mapped[str] = mapped_column(String(120), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class FeriasAjuste(Base):
    """Dias de férias somados ou tirados à mão num período aquisitivo (ex.: saldo de antes do
    sistema, faltas que o ponto não registrou, acordo)."""
    __tablename__ = "ferias_ajustes"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    periodo_inicio: Mapped[date] = mapped_column(Date)   # início do período aquisitivo
    dias: Mapped[int] = mapped_column(Integer)           # positivo soma, negativo tira
    motivo: Mapped[str] = mapped_column(String(200))
    autor: Mapped[str] = mapped_column(String(120))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class AjusteBanco(Base):
    """Crédito ou débito lançado à mão no banco de horas (ex.: horas pagas na folha)."""
    __tablename__ = "ajustes_banco"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    data: Mapped[date] = mapped_column(Date)
    minutos: Mapped[int] = mapped_column(Integer)  # positivo = crédito
    motivo: Mapped[str] = mapped_column(String(200))
    autor: Mapped[str] = mapped_column(String(120))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class Fechamento(Base):
    """Período de ponto fechado: nada dentro dele pode mais ser alterado."""
    __tablename__ = "fechamentos"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    inicio: Mapped[date] = mapped_column(Date)
    fim: Mapped[date] = mapped_column(Date)
    criado_por: Mapped[str] = mapped_column(String(120))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    # Percentuais de hora extra que valiam no dia do fechamento.
    pct_normal: Mapped[int] = mapped_column(Integer, default=50)
    pct_especial: Mapped[int] = mapped_column(Integer, default=100)
    pct_noturno: Mapped[int] = mapped_column(Integer, default=20)
    linhas: Mapped[list["FechamentoLinha"]] = relationship(
        back_populates="fechamento", cascade="all, delete-orphan")


class FechamentoLinha(Base):
    """Totais de um funcionário num período fechado (tudo em minutos) e o aceite dele."""
    __tablename__ = "fechamento_linhas"
    id: Mapped[int] = mapped_column(primary_key=True)
    fechamento_id: Mapped[int] = mapped_column(ForeignKey("fechamentos.id", ondelete="CASCADE"), index=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    previsto: Mapped[int] = mapped_column(Integer, default=0)
    trabalhado: Mapped[int] = mapped_column(Integer, default=0)
    extra_normal: Mapped[int] = mapped_column(Integer, default=0)
    extra_especial: Mapped[int] = mapped_column(Integer, default=0)  # em folga ou feriado
    noturno: Mapped[int] = mapped_column(Integer, default=0)         # horas noturnas (já reduzidas)
    dsr_perdidos: Mapped[int] = mapped_column(Integer, default=0)    # descansos descontados (dias)
    dsr_perdido_min: Mapped[int] = mapped_column(Integer, default=0)
    dsr_extras: Mapped[int] = mapped_column(Integer, default=0)      # reflexo das extras, em horas normais
    intervalo_suprimido: Mapped[int] = mapped_column(Integer, default=0)  # art. 71, §4º (indenizado +50%)
    sobreaviso: Mapped[int] = mapped_column(Integer, default=0)   # minutos (art. 244, §2º: 1/3 da hora)
    debito: Mapped[int] = mapped_column(Integer, default=0)          # atrasos e saídas antecipadas
    faltas: Mapped[int] = mapped_column(Integer, default=0)          # dias
    falta_min: Mapped[int] = mapped_column(Integer, default=0)
    dias_ausencia: Mapped[int] = mapped_column(Integer, default=0)
    ajustes: Mapped[int] = mapped_column(Integer, default=0)
    saldo_anterior: Mapped[int] = mapped_column(Integer, default=0)
    saldo_final: Mapped[int] = mapped_column(Integer, default=0)
    aceite_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    aceite_ip: Mapped[str] = mapped_column(String(60), default="")
    contestacao: Mapped[str] = mapped_column(String(500), default="")
    fechamento: Mapped[Fechamento] = relationship(back_populates="linhas")
    usuario: Mapped[Usuario] = relationship()


class FolhaPagamento(Base):
    """Folha de pagamento gerada a partir de um período de ponto fechado."""
    __tablename__ = "folhas_pagamento"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    fechamento_id: Mapped[int] = mapped_column(ForeignKey("fechamentos.id"), index=True)
    competencia: Mapped[str] = mapped_column(String(7))           # AAAA-MM
    inicio: Mapped[date] = mapped_column(Date)
    fim: Mapped[date] = mapped_column(Date)
    controle: Mapped[str] = mapped_column(String(8), default="extra")
    tabelas: Mapped[str] = mapped_column(Text, default="")        # INSS/IRRF usados (JSON)
    situacao: Mapped[str] = mapped_column(String(12), default="rascunho")  # rascunho | finalizada
    criado_por: Mapped[str] = mapped_column(String(120), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    finalizada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Revisão feita pelo Claude: "" | andamento | pronta | erro
    revisao_situacao: Mapped[str] = mapped_column(String(12), default="")
    revisao: Mapped[str] = mapped_column(Text, default="")        # JSON com resumo e alertas
    revisao_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fechamento: Mapped[Fechamento] = relationship()
    itens: Mapped[list["FolhaItem"]] = relationship(
        back_populates="folha", cascade="all, delete-orphan")


class FolhaItem(Base):
    """Holerite de um funcionário dentro da folha de pagamento."""
    __tablename__ = "folha_itens"
    id: Mapped[int] = mapped_column(primary_key=True)
    folha_id: Mapped[int] = mapped_column(ForeignKey("folhas_pagamento.id", ondelete="CASCADE"), index=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    linha_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FechamentoLinha
    salario_centavos: Mapped[int] = mapped_column(Integer, default=0)
    jornada_mensal: Mapped[int] = mapped_column(Integer, default=220)
    dependentes: Mapped[int] = mapped_column(Integer, default=0)
    vt_centavos: Mapped[int] = mapped_column(Integer, default=0)
    lancamentos: Mapped[str] = mapped_column(Text, default="[]")  # bônus, adiantamentos... (JSON)
    calculo: Mapped[str] = mapped_column(Text, default="{}")      # linhas do holerite (JSON)
    bruto: Mapped[int] = mapped_column(Integer, default=0)
    descontos: Mapped[int] = mapped_column(Integer, default=0)
    liquido: Mapped[int] = mapped_column(Integer, default=0)
    fgts: Mapped[int] = mapped_column(Integer, default=0)
    folha: Mapped[FolhaPagamento] = relationship(back_populates="itens")
    usuario: Mapped[Usuario] = relationship()


class Oferta(Base):
    """Turno oferecido à equipe: aberto (criado pelo gestor, sem dono) ou repassado (um
    funcionário oferece o próprio turno). Alguém pede, o gestor aprova e o turno passa a ser dele."""
    __tablename__ = "ofertas"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(8))  # aberto | repasse
    turno_id: Mapped[int | None] = mapped_column(
        ForeignKey("turnos.id", ondelete="SET NULL"), nullable=True)  # só no repasse
    de_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)  # quem repassa
    data: Mapped[date] = mapped_column(Date, index=True)
    inicio: Mapped[str] = mapped_column(String(5))
    fim: Mapped[str] = mapped_column(String(5))
    intervalo_min: Mapped[int] = mapped_column(Integer, default=0)
    intervalo_inicio: Mapped[str] = mapped_column(String(5), default="")
    local_id: Mapped[int | None] = mapped_column(ForeignKey("locais.id"), nullable=True)
    cargo: Mapped[str] = mapped_column(String(80), default="")  # vazio = qualquer cargo
    obs: Mapped[str] = mapped_column(String(200), default="")
    # aberta | pedida | aprovada | recusada | cancelada
    situacao: Mapped[str] = mapped_column(String(10), default="aberta", index=True)
    candidato_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    pedido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criado_por: Mapped[str] = mapped_column(String(120), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    decidido_por: Mapped[str] = mapped_column(String(120), default="")
    decidido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    de: Mapped[Usuario | None] = relationship(foreign_keys=[de_id])
    candidato: Mapped[Usuario | None] = relationship(foreign_keys=[candidato_id])
    local: Mapped[Local | None] = relationship()


class FuncaoUsuario(Base):
    """Funções (cargos) de uma pessoa, com o valor de cada uma. A de ordem 0 é a principal."""
    __tablename__ = "funcoes_usuario"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(80), default="")
    valor_centavos: Mapped[int] = mapped_column(Integer, default=0)
    tipo: Mapped[str] = mapped_column(String(6), default="mensal")   # mensal | hora
    ordem: Mapped[int] = mapped_column(Integer, default=0)


class DadosRH(Base):
    """Documentos e dados pessoais do colaborador (ficha de RH). Campos em app/rh.py."""
    __tablename__ = "dados_rh"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), unique=True, index=True)
    cpf: Mapped[str] = mapped_column(String(11), default="")
    pis: Mapped[str] = mapped_column(String(11), default="")
    rg_numero: Mapped[str] = mapped_column(String(20), default="")
    rg_orgao: Mapped[str] = mapped_column(String(20), default="")
    rg_uf: Mapped[str] = mapped_column(String(2), default="")
    rg_emissao: Mapped[str] = mapped_column(String(10), default="")
    ctps_numero: Mapped[str] = mapped_column(String(11), default="")
    ctps_serie: Mapped[str] = mapped_column(String(5), default="")
    ctps_uf: Mapped[str] = mapped_column(String(2), default="")
    titulo_eleitor: Mapped[str] = mapped_column(String(14), default="")
    titulo_zona: Mapped[str] = mapped_column(String(4), default="")
    titulo_secao: Mapped[str] = mapped_column(String(5), default="")
    cnh_numero: Mapped[str] = mapped_column(String(12), default="")
    cnh_categoria: Mapped[str] = mapped_column(String(20), default="")
    cnh_validade: Mapped[str] = mapped_column(String(10), default="")
    reservista: Mapped[str] = mapped_column(String(20), default="")
    sexo: Mapped[str] = mapped_column(String(20), default="")
    estado_civil: Mapped[str] = mapped_column(String(20), default="")
    instrucao: Mapped[str] = mapped_column(String(20), default="")
    raca_cor: Mapped[str] = mapped_column(String(20), default="")
    nome_social: Mapped[str] = mapped_column(String(120), default="")
    deficiencia: Mapped[str] = mapped_column(String(20), default="")
    nacionalidade: Mapped[str] = mapped_column(String(40), default="")
    naturalidade: Mapped[str] = mapped_column(String(60), default="")
    naturalidade_uf: Mapped[str] = mapped_column(String(2), default="")
    nome_mae: Mapped[str] = mapped_column(String(120), default="")
    nome_pai: Mapped[str] = mapped_column(String(120), default="")
    email_pessoal: Mapped[str] = mapped_column(String(120), default="")
    matricula: Mapped[str] = mapped_column(String(30), default="")
    categoria_esocial: Mapped[str] = mapped_column(String(20), default="")
    tipo_contrato: Mapped[str] = mapped_column(String(20), default="")
    fim_experiencia: Mapped[str] = mapped_column(String(10), default="")
    vencimento_contrato: Mapped[str] = mapped_column(String(10), default="")
    centro_custo: Mapped[str] = mapped_column(String(40), default="")
    insalubridade: Mapped[str] = mapped_column(String(20), default="")
    periculosidade: Mapped[str] = mapped_column(String(20), default="")
    filhos: Mapped[str] = mapped_column(String(2), default="")
    camisa: Mapped[str] = mapped_column(String(20), default="")
    cep: Mapped[str] = mapped_column(String(8), default="")
    logradouro: Mapped[str] = mapped_column(String(120), default="")
    numero: Mapped[str] = mapped_column(String(10), default="")
    complemento: Mapped[str] = mapped_column(String(60), default="")
    bairro: Mapped[str] = mapped_column(String(60), default="")
    cidade: Mapped[str] = mapped_column(String(60), default="")
    uf: Mapped[str] = mapped_column(String(2), default="")
    banco: Mapped[str] = mapped_column(String(40), default="")
    agencia: Mapped[str] = mapped_column(String(10), default="")
    conta: Mapped[str] = mapped_column(String(20), default="")
    tipo_conta: Mapped[str] = mapped_column(String(20), default="")
    pix: Mapped[str] = mapped_column(String(120), default="")
    emergencia_nome: Mapped[str] = mapped_column(String(120), default="")
    emergencia_parentesco: Mapped[str] = mapped_column(String(40), default="")
    emergencia_telefone: Mapped[str] = mapped_column(String(20), default="")


class FaturamentoMes(Base):
    """Faturamento do mês informado pelo gestor (base do CMO: custo de mão de obra ÷ faturamento)."""
    __tablename__ = "faturamento_mes"
    __table_args__ = (UniqueConstraint("empresa_id", "mes"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    mes: Mapped[str] = mapped_column(String(7), index=True)      # AAAA-MM
    valor_centavos: Mapped[int] = mapped_column(Integer, default=0)
    registrado_por: Mapped[str] = mapped_column(String(120), default="")
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class VendaDia(Base):
    """Vendas do dia digitadas pelo gestor (para o custo da equipe em % das vendas)."""
    __tablename__ = "vendas_dia"
    __table_args__ = (UniqueConstraint("empresa_id", "data"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    valor_centavos: Mapped[int] = mapped_column(Integer, default=0)
    registrado_por: Mapped[str] = mapped_column(String(120), default="")


class EscalaPublicada(Base):
    """Última publicação da escala de uma semana: um resumo da semana de cada pessoa, para saber
    quem teve mudança desde então (e avisar só essas pessoas)."""
    __tablename__ = "escalas_publicadas"
    __table_args__ = (UniqueConstraint("empresa_id", "semana"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    semana: Mapped[date] = mapped_column(Date)  # segunda-feira da grade
    assinaturas: Mapped[str] = mapped_column(Text, default="{}")
    publicado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    publicado_por: Mapped[str] = mapped_column(String(120), default="")


class Mensagem(Base):
    """Mural da equipe: avisos do gestor e recados dos funcionários."""
    __tablename__ = "mensagens"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    autor_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    texto: Mapped[str] = mapped_column(Text)
    fixada: Mapped[bool] = mapped_column(Boolean, default=False)  # fica no alto do mural
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, index=True)
    autor: Mapped[Usuario] = relationship()


class Disponibilidade(Base):
    """Quando o funcionário pode trabalhar em cada dia da semana. Sem linha = o dia todo.
    tipo: "parcial" (só de inicio a fim) | "nao" (não pode)."""
    __tablename__ = "disponibilidades"
    __table_args__ = (UniqueConstraint("usuario_id", "dia_semana"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    dia_semana: Mapped[int] = mapped_column(Integer)  # 0 = segunda
    tipo: Mapped[str] = mapped_column(String(8), default="parcial")
    inicio: Mapped[str] = mapped_column(String(5), default="")
    fim: Mapped[str] = mapped_column(String(5), default="")
    obs: Mapped[str] = mapped_column(String(120), default="")
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class AvaliacaoTurno(Base):
    """Nota de 1 a 5 que o funcionário dá ao turno quando bate a saída."""
    __tablename__ = "avaliacoes_turno"
    __table_args__ = (UniqueConstraint("usuario_id", "dia"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    dia: Mapped[date] = mapped_column(Date, index=True)
    nota: Mapped[int] = mapped_column(Integer)
    comentario: Mapped[str] = mapped_column(String(300), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()


class LeituraMensagem(Base):
    """Quem já viu cada mensagem do mural (para o "lida por X de Y")."""
    __tablename__ = "leituras_mensagem"
    __table_args__ = (UniqueConstraint("mensagem_id", "usuario_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mensagem_id: Mapped[int] = mapped_column(ForeignKey("mensagens.id", ondelete="CASCADE"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    lida_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class PerguntaTurno(Base):
    """Pergunta que a empresa faz ao funcionário quando ele bate a saída."""
    __tablename__ = "perguntas_turno"
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    texto: Mapped[str] = mapped_column(String(200))
    tipo: Mapped[str] = mapped_column(String(8), default="sim_nao")   # sim_nao | numero | texto
    avisar_se: Mapped[str] = mapped_column(String(3), default="")     # "" | sim | nao (só sim_nao)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    ordem: Mapped[int] = mapped_column(Integer, default=0)


class RespostaTurno(Base):
    __tablename__ = "respostas_turno"
    __table_args__ = (UniqueConstraint("usuario_id", "dia", "pergunta_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresas.id"), index=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    pergunta_id: Mapped[int] = mapped_column(ForeignKey("perguntas_turno.id", ondelete="CASCADE"), index=True)
    dia: Mapped[date] = mapped_column(Date, index=True)
    valor: Mapped[str] = mapped_column(String(300), default="")
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    usuario: Mapped[Usuario] = relationship()
    pergunta: Mapped[PerguntaTurno] = relationship()


# Colunas acrescentadas depois da primeira versão: (tabela, coluna, definição SQL).
COLUNAS_NOVAS = [
    ("plataforma", "entrada_cor1", "VARCHAR(7) NOT NULL DEFAULT '#4f46e5'"),
    ("plataforma", "entrada_cor2", "VARCHAR(7) NOT NULL DEFAULT '#0ea5e9'"),
    ("plataforma", "entrada_titulo", "VARCHAR(120) NOT NULL DEFAULT ''"),
    ("plataforma", "entrada_texto", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("plataforma", "entrada_imagens", "TEXT NOT NULL DEFAULT ''"),
    ("usuarios", "google_sub", "VARCHAR(64)"),
    ("usuarios", "alcance_setores", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("usuarios", "alcance_locais", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("usuarios", "setor_id", "INTEGER"),
    ("empresas", "exigir_foto", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "aparelho_unico", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "ajuste_dias", "INTEGER NOT NULL DEFAULT 7"),
    ("empresas", "fotos_dias", "INTEGER NOT NULL DEFAULT 45"),
    ("turnos", "lembrou_ini", "BOOLEAN NOT NULL DEFAULT 0"),
    ("turnos", "lembrou_fim", "BOOLEAN NOT NULL DEFAULT 0"),
    ("batidas", "foto", "VARCHAR(80) NOT NULL DEFAULT ''"),
    ("batidas", "anulada", "BOOLEAN NOT NULL DEFAULT 0"),
    ("batidas", "anulada_por", "VARCHAR(120) NOT NULL DEFAULT ''"),
    ("batidas", "anulada_em", "DATETIME"),
    ("empresas", "controle", "VARCHAR(8) NOT NULL DEFAULT 'banco'"),
    ("empresas", "intervalo_padrao_min", "INTEGER NOT NULL DEFAULT 60"),
    ("empresas", "intervalo_auto", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "tolerancia_dia_min", "INTEGER NOT NULL DEFAULT 10"),
    ("turnos", "intervalo_min", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "pct_extra_normal", "INTEGER NOT NULL DEFAULT 50"),
    ("empresas", "pct_extra_especial", "INTEGER NOT NULL DEFAULT 100"),
    ("fechamentos", "pct_normal", "INTEGER NOT NULL DEFAULT 50"),
    ("fechamentos", "pct_especial", "INTEGER NOT NULL DEFAULT 100"),
    ("empresas", "pct_noturno", "INTEGER NOT NULL DEFAULT 20"),
    ("empresas", "noturno_reduzida", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "intervalo_obrigatorio", "BOOLEAN NOT NULL DEFAULT 1"),
    ("fechamentos", "pct_noturno", "INTEGER NOT NULL DEFAULT 20"),
    ("fechamento_linhas", "noturno", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "dsr_faltas", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "dsr_extras", "BOOLEAN NOT NULL DEFAULT 1"),
    ("fechamento_linhas", "dsr_perdidos", "INTEGER NOT NULL DEFAULT 0"),
    ("fechamento_linhas", "dsr_perdido_min", "INTEGER NOT NULL DEFAULT 0"),
    ("fechamento_linhas", "dsr_extras", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "ativo", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "plano_id", "INTEGER"),
    ("empresas", "teste_ate", "DATETIME"),
    ("empresas", "pago_ate", "DATETIME"),
    ("empresas", "isenta", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "obs_master", "TEXT NOT NULL DEFAULT ''"),
    ("usuarios", "is_admin", "BOOLEAN NOT NULL DEFAULT 0"),
    ("usuarios", "guia_visto", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "cpf_cnpj", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("empresas", "asaas_cliente_id", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("empresas", "asaas_assinatura_id", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("pagamentos", "plano_id", "INTEGER"),
    ("pagamentos", "tipo", "VARCHAR(12) NOT NULL DEFAULT 'manual'"),
    ("pagamentos", "status", "VARCHAR(12) NOT NULL DEFAULT 'pago'"),
    ("pagamentos", "pago_em", "DATETIME"),
    ("pagamentos", "asaas_id", "VARCHAR(40)"),
    ("pagamentos", "fatura_url", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("usuarios", "pin_hash", "VARCHAR(200)"),
    ("usuarios", "pin_erros", "INTEGER NOT NULL DEFAULT 0"),
    ("turnos", "local_id", "INTEGER"),
    ("turnos", "fixa", "BOOLEAN NOT NULL DEFAULT 0"),
    ("turnos", "intervalo_inicio", "VARCHAR(5) NOT NULL DEFAULT ''"),
    ("turnos", "lembrou_int", "BOOLEAN NOT NULL DEFAULT 0"),
    ("escala_fixa", "intervalo_inicio", "VARCHAR(5) NOT NULL DEFAULT ''"),
    ("batidas", "local_nome", "VARCHAR(80) NOT NULL DEFAULT ''"),
    ("batidas", "origem", "VARCHAR(8) NOT NULL DEFAULT ''"),
    ("usuarios", "salario_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("usuarios", "jornada_mensal", "INTEGER NOT NULL DEFAULT 220"),
    ("usuarios", "dependentes", "INTEGER NOT NULL DEFAULT 0"),
    ("usuarios", "vt_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("planos", "limite_locais", "INTEGER NOT NULL DEFAULT 0"),
    ("planos", "com_pagamento", "BOOLEAN NOT NULL DEFAULT 1"),
    ("planos", "com_alertas", "BOOLEAN NOT NULL DEFAULT 1"),
    ("planos", "com_gps", "BOOLEAN NOT NULL DEFAULT 1"),
    ("planos", "com_foto", "BOOLEAN NOT NULL DEFAULT 1"),
    ("planos", "com_facial", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "trocas_aprovacao", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "avaliar_turno", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "segmento", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("empresas", "uf", "VARCHAR(2) NOT NULL DEFAULT ''"),
    ("empresas", "porte", "VARCHAR(10) NOT NULL DEFAULT ''"),
    ("empresas", "provedor_folha", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("empresas", "abre", "VARCHAR(5) NOT NULL DEFAULT ''"),
    ("empresas", "fecha", "VARCHAR(5) NOT NULL DEFAULT ''"),
    ("empresas", "funcoes_padrao", "TEXT NOT NULL DEFAULT '[]'"),
    ("empresas", "modelos_turno", "TEXT NOT NULL DEFAULT '[]'"),
    ("empresas", "triagem_em", "DATETIME"),
    ("empresas", "triagem_resumo", "TEXT NOT NULL DEFAULT '[]'"),
    ("empresas", "confirmar_turno", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "confirmar_antes_h", "INTEGER NOT NULL DEFAULT 24"),
    ("empresas", "oferecer_sem_confirmacao", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "travar_escala_dias", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "cerca", "TEXT NOT NULL DEFAULT ''"),
    ("empresas", "pessoas_cobradas", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "valor_assinatura_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "valor_conferido_em", "DATETIME"),
    ("planos", "com_cerca", "BOOLEAN NOT NULL DEFAULT 1"),
    ("planos", "base_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("planos", "por_pessoa_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("planos", "minimo_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "motivo_fora", "BOOLEAN NOT NULL DEFAULT 1"),
    ("empresas", "cerca_turno", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "cerca_turno_min", "INTEGER NOT NULL DEFAULT 10"),
    ("locais", "cerca", "TEXT NOT NULL DEFAULT ''"),
    ("batidas", "motivo_fora", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("turnos", "confirmado_em", "DATETIME"),
    ("turnos", "lembrou_conf", "BOOLEAN NOT NULL DEFAULT 0"),
    ("usuarios", "nascimento", "DATE"),
    ("empresas", "minimo_proprio_centavos", "INTEGER NOT NULL DEFAULT 0"),
    ("plataforma", "razao_social", "VARCHAR(160) NOT NULL DEFAULT ''"),
    ("plataforma", "endereco", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("plataforma", "email_legal", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("plataforma", "estado_lei", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("plataforma", "pais_servidores", "VARCHAR(60) NOT NULL DEFAULT 'Brasil'"),
    ("usuarios", "termos_em", "DATETIME"),
    ("usuarios", "termos_versao", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("empresas", "mural_equipe", "BOOLEAN NOT NULL DEFAULT 1"),
    ("usuarios", "mural_visto_em", "DATETIME"),
    ("usuarios", "dono", "BOOLEAN NOT NULL DEFAULT 0"),
    ("usuarios", "permissoes", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("usuarios", "redefinir_hash", "VARCHAR(64)"),
    ("usuarios", "redefinir_ate", "DATETIME"),
    ("plataforma", "carencia_dias", "INTEGER NOT NULL DEFAULT 3"),
    ("plataforma", "carencia_teste_dias", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "entrada_antecipada_min", "INTEGER NOT NULL DEFAULT 0"),
    ("empresas", "saida_auto_min", "INTEGER NOT NULL DEFAULT 0"),
    ("batidas", "conferida", "BOOLEAN NOT NULL DEFAULT 1"),
    ("usuarios", "classificacao", "VARCHAR(12) NOT NULL DEFAULT 'clt'"),
    ("usuarios", "data_admissao", "DATE"),
    ("usuarios", "ponto_desde", "DATE"),
    ("usuarios", "conta_excluida_em", "DATETIME"),
    ("conversas_venda", "nome", "VARCHAR(80) NOT NULL DEFAULT ''"),
    ("conversas_venda", "telefone", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("conversas_venda", "empresa", "VARCHAR(80) NOT NULL DEFAULT ''"),
    ("conversas_venda", "autorizou", "BOOLEAN NOT NULL DEFAULT 0"),
    ("conversas_venda", "contato_em", "DATETIME"),
    ("conversas_venda", "situacao", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("conversas_venda", "atendente_id", "INTEGER"),
    ("conversas_venda", "atendente_em", "DATETIME"),
    ("usuarios", "master_areas", "VARCHAR(120) NOT NULL DEFAULT ''"),
    ("empresas", "exclusao_pedida_em", "DATETIME"),
    ("usuarios", "desligado_em", "DATE"),
    ("usuarios", "desligamento_tipo", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("usuarios", "aviso_previo", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("usuarios", "desligamento_obs", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("usuarios", "local_id", "INTEGER"),
    ("empresas", "aviso_extra_min", "INTEGER NOT NULL DEFAULT 30"),
    ("empresas", "intervalo_minimo_min", "INTEGER NOT NULL DEFAULT 60"),
    ("ausencias", "abono_dias", "INTEGER NOT NULL DEFAULT 0"),
    ("fechamento_linhas", "intervalo_suprimido", "INTEGER NOT NULL DEFAULT 0"),
    ("fechamento_linhas", "sobreaviso", "INTEGER NOT NULL DEFAULT 0"),
    ("plataforma", "nome_curto", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("empresas", "usa_faturamento", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "logo", "VARCHAR(60) NOT NULL DEFAULT ''"),
    ("empresas", "facial_ativo", "BOOLEAN NOT NULL DEFAULT 0"),
    ("empresas", "ponto_na_lotacao", "BOOLEAN NOT NULL DEFAULT 0"),
    ("turnos", "so_principal", "BOOLEAN NOT NULL DEFAULT 0"),
    ("escala_fixa", "so_principal", "BOOLEAN NOT NULL DEFAULT 0"),
    ("usuarios", "facial_consentimento", "VARCHAR(10) NOT NULL DEFAULT ''"),
    ("usuarios", "facial_consentido_em", "DATETIME"),
    ("usuarios", "facial_descritor", "TEXT NOT NULL DEFAULT ''"),
    ("batidas", "facial", "VARCHAR(10) NOT NULL DEFAULT ''"),
    ("batidas", "facial_dist", "FLOAT"),
    ("empresas", "meta_cmo", "INTEGER NOT NULL DEFAULT 30"),
    ("empresas", "regime_tributario", "VARCHAR(12) NOT NULL DEFAULT 'simples'"),
    ("historico", "usuario_id", "INTEGER"),
    ("historico", "dia", "DATE"),
]


def _definicao(col, dialeto) -> str:
    """Tipo e valor padrão de uma coluna nova, aceitos pelo ALTER TABLE do SQLite."""
    tipo = col.type.compile(dialect=dialeto)
    padrao = col.default.arg if col.default is not None and col.default.is_scalar else None
    if padrao is None:
        try:
            py = col.type.python_type
        except NotImplementedError:
            py = None
        padrao = {bool: False, int: 0, float: 0.0, str: ""}.get(py)
    if padrao is None:
        return tipo
    if isinstance(padrao, bool):
        literal = "1" if padrao else "0"
    elif isinstance(padrao, (int, float)):
        literal = str(padrao)
    else:
        literal = "'" + str(padrao).replace("'", "''") + "'"
    return f"{tipo} NOT NULL DEFAULT {literal}" if not col.nullable else f"{tipo} DEFAULT {literal}"


def criar_tabelas():
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as con:
        acrescentadas = set()
        for tabela, coluna, definicao in COLUNAS_NOVAS:
            if coluna not in {c["name"] for c in insp.get_columns(tabela)}:
                con.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}"))
                acrescentadas.add((tabela, coluna))
        # Rede de segurança: qualquer coluna do modelo que ainda falte no banco é criada
        # (com um valor padrão seguro), para o app nunca subir com "no such column".
        for tabela in Base.metadata.sorted_tables:
            existentes = {c["name"] for c in inspect(con).get_columns(tabela.name)}
            for col in tabela.columns:
                if col.name not in existentes:
                    con.execute(text(f"ALTER TABLE {tabela.name} ADD COLUMN {col.name} "
                                     f"{_definicao(col, con.dialect)}"))
                    acrescentadas.add((tabela.name, col.name))
        if ("usuarios", "dono") in acrescentadas:
            # Antes só existia um gestor por empresa: o que criou a conta. Ele vira o dono.
            con.execute(text("UPDATE usuarios SET dono = 1 WHERE papel = 'gestor'"))
        # As fotos das batidas ficam guardadas por no máximo 45 dias.
        con.execute(text("UPDATE empresas SET fotos_dias = 45 WHERE fotos_dias > 45"))
        con.execute(text("UPDATE pagamentos SET pago_em = criado_em "
                         "WHERE pago_em IS NULL AND status = 'pago'"))
    _preparar_revenda()


# Exemplos, criados fora de venda: o dono ajusta os precos e poe a venda em Master -> Planos.
PLANO_GRATIS = ("Grátis", 0, 10, "Escala da semana e turnos abertos\nPonto por geolocalização\nMural da equipe\nAvisos de atraso e falta")
PLANOS_INICIAIS = [
    ("Essencial", 4990, 10, "Escala da semana\nPonto por geolocalização\nAvisos de atraso e falta\nChecklist diário"),
    ("Profissional", 9990, 30, "Tudo do Essencial\nFechamento, banco de horas e folha\nFiliais e totem"),
    ("Ilimitado", 19990, 0, "Tudo do Profissional\nFuncionários ilimitados"),
]


def definir_admin(db) -> None:
    """Quem é o dono da plataforma: o e-mail de ADMIN_EMAIL; sem ele, a conta de gestor
    mais antiga. A empresa do dono vira cortesia (nunca vence)."""
    from sqlalchemy import select
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if email:
        u = db.scalar(select(Usuario).where(Usuario.email == email))
    elif db.scalar(select(Usuario.id).where(Usuario.is_admin.is_(True)).limit(1)):
        return
    else:
        u = db.scalar(select(Usuario).where(Usuario.papel == "gestor").order_by(Usuario.id).limit(1))
    if u and not u.is_admin:
        u.is_admin = True
        u.empresa.isenta = True


def _preparar_revenda():
    from datetime import timedelta
    from sqlalchemy import select
    with SessionLocal() as db:
        if not db.get(Plataforma, 1):
            db.add(Plataforma(id=1, cadastro_aberto=os.environ.get(
                "CADASTRO_ABERTO", "true").lower() == "true"))
        if not db.scalar(select(Plano.id).limit(1)):
            for i, (nome, preco, limite, desc) in enumerate(PLANOS_INICIAIS):
                db.add(Plano(nome=nome, preco_centavos=preco, limite_funcionarios=limite,
                             descricao=desc, destaque=i == 1, ordem=i, ativo=False))
        if not db.scalar(select(Plano.id).where(Plano.preco_centavos == 0).limit(1)):
            nome, preco, limite, desc = PLANO_GRATIS
            db.add(Plano(nome=nome, preco_centavos=preco, limite_funcionarios=limite,
                         limite_locais=1, com_pagamento=False, descricao=desc, ordem=-1,
                         ativo=False))
        definir_admin(db)
        db.flush()
        # Empresas de antes da revenda ganham 7 dias de teste a partir de agora.
        for e in db.scalars(select(Empresa).where(
                Empresa.teste_ate.is_(None), Empresa.pago_ate.is_(None),
                Empresa.isenta.is_(False))):
            e.teste_ate = agora() + timedelta(days=7)
        db.commit()


# ---------------------------------------------------------------- alcance do gerente
# Um gerente ligado a setores/locais só enxerga as pessoas deles. core.gestor_logado põe em
# db.info["alcance"] = (empresa_id, ids visíveis); aqui toda consulta ORM dessa sessão recebe o
# filtro (pessoas e os registros ligados a elas). Consultas marcadas com
# .execution_options(alcance_livre=True) passam sem filtro (ex.: celular já cadastrado).
LIGADOS_A_PESSOA = ("Turno", "EscalaFixa", "Batida", "Tarefa", "TarefaFeita", "Alerta", "Aparelho",
                    "Ajuste", "Historico", "Ausencia", "Sobreaviso", "MotivoAjuste", "FeriasAjuste",
                    "AjusteBanco", "FechamentoLinha", "FolhaItem", "FuncaoUsuario", "Disponibilidade",
                    "AvaliacaoTurno", "RespostaTurno", "DadosRH")


def _aplicar_alcance(estado):
    alcance = estado.session.info.get("alcance")
    if not alcance or not estado.is_select or estado.execution_options.get("alcance_livre"):
        return
    from sqlalchemy import or_
    from sqlalchemy.orm import with_loader_criteria
    eid, ids = alcance
    ids = tuple(ids)
    opcoes = [with_loader_criteria(Usuario, or_(Usuario.empresa_id != eid, Usuario.id.in_(ids)),
                                   include_aliases=True)]
    for nome in LIGADOS_A_PESSOA:
        m = globals()[nome]
        opcoes.append(with_loader_criteria(m, or_(m.usuario_id.is_(None), m.usuario_id.in_(ids)),
                                           include_aliases=True))
    opcoes.append(with_loader_criteria(Oferta, or_(Oferta.de_id.is_(None), Oferta.de_id.in_(ids)),
                                       include_aliases=True))
    estado.statement = estado.statement.options(*opcoes)


event.listen(SessionLocal, "do_orm_execute", _aplicar_alcance)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
