"""Configuração guiada (triagem): logo depois do cadastro, a empresa responde 7 perguntas curtas
(segmento, tamanho da equipe, locais, UF, horário, equipe e banco de horas/hora extra) e o sistema já
sai configurado: funções do segmento (com gorjeta onde faz sentido), checklist de abertura e
fechamento, locais extras, fuso da UF, ajustes do ponto, modelos de turno e o
plano recomendado. Tudo pode ser mudado depois; refazer a triagem não apaga o que já existe."""
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import apuracao, regras
from . import db as banco
from .core import HORA_RE, avisar, exigir_csrf, gestor_logado, ir, pode, render
from .db import Local, Tarefa, Usuario, get_db
from .i18n import _

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

# segmento -> (nome, funções [(nome, recebe gorjeta)], checklist, abre, fecha, entrada antecipada)
SEGMENTOS = {
    "restaurante": ("Restaurante ou bar",
                    [("Cozinha", False), ("Garçom", True), ("Bartender", True), ("Recepção", True),
                     ("Caixa", False), ("Entregas", True), ("Gerente", False)],
                    ["Abrir o caixa e conferir o troco", "Conferir a temperatura das geladeiras",
                     "Preparar o salão e as mesas", "Fechar o caixa", "Limpar a cozinha e retirar o lixo"],
                    "10:00", "23:00", 15),
    "cafe": ("Café ou padaria",
             [("Barista", True), ("Atendente", True), ("Confeiteiro", False), ("Caixa", False), ("Gerente", False)],
             ["Ligar as máquinas e conferir os grãos", "Repor a vitrine", "Fechar o caixa", "Limpar as máquinas"],
             "06:00", "18:00", 15),
    "fastfood": ("Fast food",
                 [("Atendente", False), ("Cozinha", False), ("Drive-thru", False), ("Limpeza", False),
                  ("Gerente de turno", False)],
                 ["Conferir o estoque do dia", "Registrar a temperatura dos equipamentos", "Limpar o salão",
                  "Fechar o caixa"],
                 "10:00", "22:00", 10),
    "varejo": ("Loja ou varejo",
               [("Vendedor", False), ("Caixa", False), ("Estoquista", False), ("Gerente", False)],
               ["Abrir a loja e ligar as luzes", "Conferir a vitrine", "Repor as prateleiras", "Fechar o caixa"],
               "09:00", "21:00", 15),
    "hotel": ("Hotel ou pousada",
              [("Recepção", False), ("Camareira", True), ("Manutenção", False), ("Café da manhã", False),
               ("Gerente", False)],
              ["Conferir as reservas do dia", "Passagem de turno na recepção", "Conferir os quartos liberados"],
              "00:00", "23:59", 0),
    "clinica": ("Clínica ou saúde",
                [("Recepção", False), ("Enfermagem", False), ("Assistente", False), ("Limpeza", False),
                 ("Gerente", False)],
                ["Conferir a agenda do dia", "Esterilizar os materiais", "Repor os insumos"],
                "08:00", "18:00", 0),
    "servicos": ("Limpeza ou serviços",
                 [("Técnico", False), ("Auxiliar", False), ("Supervisor", False)],
                 ["Conferir os materiais e o uniforme", "Enviar foto do serviço pronto"],
                 "08:00", "17:00", 0),
    "beleza": ("Academia, salão ou spa",
               [("Recepcionista", False), ("Profissional", True), ("Instrutor", False), ("Limpeza", False),
                ("Gerente", False)],
               ["Abrir e conferir os equipamentos", "Limpar as estações", "Fechar o caixa"],
               "07:00", "21:00", 0),
    "outro": ("Outro tipo de negócio", [("Equipe", False), ("Gerente", False)], [], "08:00", "17:00", 0),
}
PORTES = {"1-5": ("1 a 5", 5), "6-15": ("6 a 15", 15), "16-30": ("16 a 30", 30),
          "31-75": ("31 a 75", 75), "76+": ("76 ou mais", 9999)}
LOCAIS = {"1": 1, "2-3": 3, "4-10": 10, "10+": 99}
# Como as horas a mais são tratadas (empresas.controle).
CONTROLES = {"banco": "Banco de horas (compensa depois)", "extra": "Hora extra paga no mês"}
PROVEDORES = {"contador": "Meu contador faz a folha", "noponto": "Quero calcular a folha aqui",
              "sistema": "Outro sistema de folha", "nenhum": "Ainda não sei"}
PONTO = {"celular": "No celular de cada um", "tablet": "Num tablet da empresa (totem)",
         "ambos": "Os dois"}
UFS = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
       "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
       "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará",
       "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
       "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
       "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}
FUSO_UF = {**{uf: "America/Manaus" for uf in ("AM", "RR", "RO", "MT", "MS")}, "AC": "America/Rio_Branco"}


def _min(h: str) -> int:
    return int(h[:2]) * 60 + int(h[3:5])


def _hm(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def modelos(abre: str, fecha: str) -> list[dict]:
    """Modelos de turno a partir do horário de funcionamento."""
    a, f = _min(abre), _min(fecha)
    if f <= a:
        f += 1440
    total = f - a
    if total >= 1380:   # aberto o dia todo: 3 turnos de 8 h
        return [{"nome": _("Manhã"), "inicio": "07:00", "fim": "15:00"},
                {"nome": _("Tarde"), "inicio": "15:00", "fim": "23:00"},
                {"nome": _("Noite"), "inicio": "23:00", "fim": "07:00"}]
    if total <= 540:    # até 9 h: um turno só
        return [{"nome": _("Integral"), "inicio": _hm(a), "fim": _hm(f)}]
    meio = a + (total // 2) // 30 * 30
    sai = [{"nome": _("Abertura"), "inicio": _hm(a), "fim": _hm(min(meio + 30, a + 540))},
           {"nome": _("Fechamento"), "inicio": _hm(max(meio - 30, f - 540)), "fim": _hm(f)}]
    if total <= 900:
        sai.append({"nome": _("Integral"), "inicio": _hm(a), "fim": _hm(min(f, a + 510))})
    return sai


def plano_recomendado(db: Session, pessoas: int, locais: int):
    from .master import planos_a_venda
    planos = sorted((p for p in planos_a_venda(db) if p.preco_centavos > 0 or len(planos_a_venda(db)) == 1),
                    key=lambda p: p.preco_centavos)
    for p in planos:
        if (not p.limite_funcionarios or p.limite_funcionarios >= pessoas) and \
                (not p.limite_locais or p.limite_locais >= locais):
            return p
    return planos[-1] if planos else None


def aplicar(db: Session, e, autor: str, r: dict) -> list[str]:
    """Aplica as respostas. Devolve o resumo do que foi feito (frases já traduzidas)."""
    from . import salario_minimo
    from .core import limite_locais
    feito = []
    seg = SEGMENTOS[r["segmento"]]
    e.segmento, e.porte = r["segmento"], r["porte"]

    # UF e fuso
    e.uf = r["estado"]
    e.fuso = FUSO_UF.get(r["estado"], "America/Sao_Paulo")
    feito.append(_("{v1}: fuso horário {v2}.", v1=UFS[r["estado"]], v2=e.fuso))
    m = salario_minimo.minimo(e, regras.hoje(e))
    feito.append(_("Salário mínimo em uso: {v1} por mês ({v2}). Se a sua categoria tem piso maior, informe em Configurações.",
                   v1=salario_minimo.dinheiro(m["centavos"]), v2=_(m["fonte"])))

    # horário e modelos de turno
    e.abre, e.fecha = r["abre"], r["fecha"]
    e.modelos_turno = json.dumps(modelos(r["abre"], r["fecha"]), ensure_ascii=False)
    feito.append(_("Modelos de turno para a escala: {v1}.",
                   v1=", ".join(f"{x['nome']} {x['inicio']}–{x['fim']}" for x in json.loads(e.modelos_turno))))

    # ponto
    e.intervalo_padrao_min = e.intervalo_padrao_min or 30
    if not e.saida_auto_min:
        e.saida_auto_min = 60
    if seg[5] and not e.entrada_antecipada_min:
        e.entrada_antecipada_min = seg[5]
    feito.append(_("Ponto: tolerância de {v1} min, saída automática 60 min depois do fim do turno e aviso quando alguém estiver perto da hora extra do dia.",
                   v1=e.tolerancia_min))
    if seg[5]:
        feito.append(_("O ponto só abre {v1} min antes do turno, para evitar hora extra de quem chega cedo.", v1=seg[5]))

    # funções
    gorjeta = r.get("gorjeta") == "sim"
    funcoes = [{"nome": _(n), "gorjeta": bool(g and gorjeta)} for n, g in seg[1]]
    e.funcoes_padrao = json.dumps(funcoes, ensure_ascii=False)
    feito.append(_("Funções sugeridas: {v1}.", v1=", ".join(f["nome"] + (" (gorjeta)" if f["gorjeta"] else "") for f in funcoes)))

    # checklist
    if seg[2] and not db.scalar(select(func.count()).select_from(Tarefa).where(Tarefa.empresa_id == e.id)):
        for i, t in enumerate(seg[2]):
            db.add(Tarefa(empresa_id=e.id, titulo=_(t)[:160], dias="0123456", ordem=i))
        feito.append(_("Checklist do dia com {v1} tarefa(s) de abertura e fechamento.", v1=len(seg[2])))

    # locais extras
    nomes = [n.strip()[:80] for n in r.get("locais_nomes", []) if n.strip()]
    lim = limite_locais(e)
    existentes = db.scalar(select(func.count()).select_from(Local).where(Local.empresa_id == e.id)) or 0
    criados = 0
    for n in nomes:
        if lim and 1 + existentes + criados >= lim:
            break
        db.add(Local(empresa_id=e.id, nome=n))
        criados += 1
    if criados:
        feito.append(_("{v1} local(is) extra(s) criado(s). Abra cada um em Locais e marque o endereço no mapa.", v1=criados))

    # jornada e folha
    e.controle = r["frequencia"]
    e.provedor_folha = r.get("provedor", "")
    feito.append(_("Horas a mais: {v1}.", v1=_(CONTROLES[r["frequencia"]])))

    e.triagem_em = banco.agora()
    e.triagem_resumo = json.dumps(feito, ensure_ascii=False)
    regras.registrar(db, e.id, autor, "configuração guiada", " ".join(feito)[:1000])
    return feito


def dicas(db: Session, e, r: dict) -> list[tuple[str, str, str]]:
    """Próximos passos: (texto, rótulo do botão, link)."""
    sai = []
    if e.lat is None:
        sai.append((_("Marque no mapa onde fica a empresa, para o ponto pelo celular conferir o local."),
                    _("Definir o local"), "/painel/config"))
    sai.append((_("Cadastre a equipe e envie o link de acesso de cada pessoa."), _("Adicionar pessoa"), "/painel/equipe/novo"))
    if r.get("ponto") in ("tablet", "ambos"):
        sai.append((_("Ative o tablet da empresa como totem de ponto."), _("Criar totem"), "/painel/locais#totens"))
    if r.get("menores") == "sim":
        sai.append((_("Na ficha de quem tem menos de 18 anos, informe a data de nascimento: o sistema confere os limites da lei."),
                    _("Ver a equipe"), "/painel/equipe"))
    if (r.get("provedor") or e.provedor_folha) == "noponto":
        sai.append((_("Em Pagamento, informe o salário, a jornada e os dependentes de cada um para calcular a folha."),
                    _("Abrir Pagamento"), "/painel/pagamento"))
    sai.append((_("Monte a escala da semana usando os modelos de turno."), _("Abrir a escala"), "/painel/escala"))
    return sai


# ------------------------------------------------------------------ telas
def _form(request, u, e, db, erro=None, v=None):
    return render(request, "painel_triagem.html", 400 if erro else 200, u=u, e=e, aba="config", erro=erro,
                  v=v or {}, SEGMENTOS=SEGMENTOS, PORTES=PORTES, LOCAIS=LOCAIS, FREQUENCIAS=CONTROLES,
                  PROVEDORES=PROVEDORES, PONTO=PONTO, estados=UFS, cidades={})


@rota.get("/painel/comecar")
def tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    if not pode(u, "dono"):
        return ir("/painel")
    return _form(request, u, u.empresa, db)


@rota.post("/painel/comecar", dependencies=CSRF)
async def salvar(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    if not pode(u, "dono"):
        return ir("/painel")
    e = u.empresa
    f = await request.form()
    r = {k: str(f.get(k, "")).strip() for k in ("segmento", "porte", "locais", "estado", "cidade", "abre", "fecha",
                                                 "gorjeta", "menores", "ponto", "frequencia", "provedor")}
    r["locais_nomes"] = [str(x) for x in f.getlist("local_nome")][:12]
    erro = None
    if r["segmento"] not in SEGMENTOS:
        erro = "Escolha o tipo do negócio."
    elif r["porte"] not in PORTES or r["locais"] not in LOCAIS:
        erro = "Informe quantas pessoas e quantos locais."
    elif r["estado"] not in UFS:
        erro = "Escolha o estado (UF)."
    elif not (HORA_RE.match(r["abre"]) and HORA_RE.match(r["fecha"])) or r["abre"] == r["fecha"]:
        erro = "Informe a hora em que abre e a hora em que fecha."
    elif r["frequencia"] not in CONTROLES:
        erro = "Escolha como as horas a mais são tratadas."
    if erro:
        return _form(request, u, e, db, erro, r)
    if r["ponto"] not in PONTO:
        r["ponto"] = "celular"
    feito = aplicar(db, e, u.nome, r)
    db.commit()
    request.session["triagem"] = {k: r[k] for k in ("ponto", "menores", "porte", "locais")}
    return ir("/painel/comecar/pronto")


@rota.get("/painel/comecar/pronto")
def pronto(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    if not e.triagem_em:
        return ir("/painel/comecar")
    r = request.session.get("triagem") or {"porte": e.porte}
    pessoas = PORTES.get(e.porte, ("", 5))[1]
    locais = LOCAIS.get(r.get("locais", "1"), 1)
    plano = plano_recomendado(db, pessoas, locais)
    return render(request, "painel_triagem_pronto.html", u=u, e=e, aba="config",
                  feito=json.loads(e.triagem_resumo or "[]"), dicas=dicas(db, e, r), plano=plano)


def funcoes_da(e) -> list[dict]:
    try:
        return json.loads(e.funcoes_padrao or "[]")
    except ValueError:
        return []


def modelos_da(e) -> list[dict]:
    try:
        return json.loads(e.modelos_turno or "[]")
    except ValueError:
        return []
