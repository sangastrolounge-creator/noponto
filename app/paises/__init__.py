"""Camada de regras por país.

O país pertence à empresa (Empresa.pais, escolhido no cadastro) e decide as regras
trabalhistas, os feriados, os períodos, a folha e a cobrança. As telas pedem as regras com
regras_de(empresa) e chamam as funções do contrato abaixo, nunca um módulo de regra direto.
Cada país é um módulo deste pacote (br.py, us/) que cumpre o mesmo contrato.

Idioma é outra coisa: português, inglês e espanhol valem para qualquer país."""
import os
from importlib import import_module

PADRAO = "BR"
PAISES = {"BR": "Brasil", "US": "Estados Unidos"}

# O que todo país precisa oferecer (conferido pelos testes).
CONTRATO = (
    "CODIGO", "NOME", "MOEDA", "IDIOMA", "COBRANCA",
    "feriados", "facultativos", "comemorativas",
    "inicio_semana", "periodo_aberto", "periodo_sugerido", "apurar", "fechar", "ao_reabrir",
    "CHAVES_RESUMO", "TIPOS_PEDIDO_AUSENCIA", "menores", "salario_minimo", "escala_justa", "SEMANA_LIMITE_MIN",
    "calcular_folha", "alertas_folha",
)


def codigo(valor) -> str:
    """Código do país de uma empresa (ou de um texto). Vazio ou desconhecido vira o padrão."""
    c = getattr(valor, "pais", valor)
    c = (c or "").strip().upper() if isinstance(c, str) else ""
    return c if c in PAISES else PADRAO


def regras_de(valor):
    """Módulo de regras do país da empresa (ou do código de país informado)."""
    return import_module(f"{__name__}.{codigo(valor).lower()}")


# Domínios que abrem a versão americana do site (página de vendas, cadastro e textos legais).
DOMINIOS_EUA = {d.strip().lower() for d in os.environ.get(
    "DOMINIOS_EUA", "dataworke.com,www.dataworke.com").split(",") if d.strip()}


def pais_do_host(request) -> str:
    """País padrão das páginas públicas pelo endereço do site. Depois do cadastro vale o país
    da empresa, nunca o domínio."""
    host = (request.headers.get("host", "") if request is not None else "").split(":")[0].lower()
    return "US" if host in DOMINIOS_EUA else PADRAO


def aplicar_padroes(e, pais: str) -> None:
    """Valores iniciais da jornada no país (empresa nova ou trocada de país no Master)."""
    e.pais = pais
    if pais == "US":
        from .us import apuracao as ap_us
        if not e.estado:
            ap_us.aplicar_estado(e, "FL")
        e.fuso = "America/New_York"
        e.intervalo_padrao_min, e.intervalo_auto, e.intervalo_obrigatorio = 30, False, True
        e.pct_noturno, e.pct_feriado, e.tolerancia_dia_min = 0, 0, 7
        e.semana_inicio, e.frequencia = 6, "biweekly"
        e.facial_ativo = False
