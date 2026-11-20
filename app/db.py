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
    pais_servidores: Mapped[str] = mapped_column(String(60), default="Brasil")


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
    com_cerca: Mapped[bool] = mapped_column(Boolean, default=True)    # cerca desenhada e conferência no turno
    # Cobrança por pessoa (opcional): base + valor por pessoa que trabalhou nos últimos 30 dias,
    # nunca abaixo do mínimo. Com valor por pessoa = 0, vale o preço fixo (preco_centavos).
    # Num plano por pessoa, preco_centavos guarda o menor valor possível (para ordenar e mostrar).
    base_centavos: Mapped[int] = mapped_column(Integer, default=0)
    por_pessoa_centavos: Mapped[int] = mapped_column(Integer, default=0)
    minimo_centavos: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def gratis(self) -> bool:
        return self.preco_centavos == 0

    @property
    def por_pessoa(self) -> bool:
        return (self.por_pessoa_centavos or 0) > 0

    def valor_mensal(self, pessoas: int) -> int:
        if not self.por_pessoa:
            return self.preco_centavos or 0
        return max(self.minimo_centavos or 0,
                   (self.base_centavos or 0) + max(0, pessoas) * self.por_pessoa_centavos)


class Empresa(Base):
    __tablename__ = "empresas"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    endereco: Mapped[str] = mapped_column(String(300), default="")
    # Local onde o ponto é válido. Sem lat/lng o ponto é aceito sem conferência.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    raio_m: Mapped[int] = mapped_column(Integer, default=150)
    bloquear_fora: Mapped[bool] = mapped_column(Boolean, default=True)
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
    # Regras automáticas do ponto (0 = desligada): o ponto só abre X min antes do início da
    # escala; X min depois do fim, quem esqueceu a saída ganha uma no horário do fim (para conferir).
    entrada_antecipada_min: Mapped[int] = mapped_column(Integer, default=0)
    saida_auto_min: Mapped[int] = mapped_column(Integer, default=0)
    # Avisa o gestor quando faltam X min para a hora extra da semana (0 = desligado).
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
    papel: Mapped[str] = mapped_column(String(12), default="funcionario")  # gestor | funcionario
    na_escala: Mapped[bool] = mapped_column(Boolean, default=True)  # aparece na escala e bate ponto
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Link de primeiro acesso (o funcionário cria a própria senha).
    convite_token: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    # PIN para bater o ponto no totem (tablet da empresa). Bloqueia após 5 erros seguidos.
    pin_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pin_erros: Mapped[int] = mapped_column(Integer, default=0)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # dono da plataforma (master)
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
    # "Esqueci a senha": hash do link enviado por e-mail e até quando ele vale.
    redefinir_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    redefinir_ate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Recebe gorjeta (Lei 13.419/2017: integra a remuneração).
    gorjeta: Mapped[bool] = mapped_column(Boolean, default=False)  # funcionário que recebe gorjeta
    # Ficha: tipo de contrato (clt | aprendiz | intermitente | estagio | pj), admissão e local principal.
    classificacao: Mapped[str] = mapped_column(String(12), default="clt")
    data_admissao: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Desligamento: último dia de trabalho, tipo (desligamento.TIPOS), aviso prévio e observação.
    desligado_em: Mapped[date | None] = mapped_column(Date, nullable=True)
    desligamento_tipo: Mapped[str] = mapped_column(String(20), default="")
    aviso_previo: Mapped[str] = mapped_column(String(16), default="")
    desligamento_obs: Mapped[str] = mapped_column(String(300), default="")
    # Só para menores de 18 (regras de trabalho de menores); vazio para os demais.
    nascimento: Mapped[date | None] = mapped_column(Date, nullable=True)
    local_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # Local extra onde a pessoa trabalha nesse dia. Vazio = qualquer local da empresa.
    local_id: Mapped[int | None] = mapped_column(ForeignKey("locais.id"), nullable=True)
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
    conferida: Mapped[bool] = mapped_column(Boolean, default=True)  # False = saída automática a conferir
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
