"""Salário mínimo dos EUA: federal, por estado e por cidade/região, com data de início.

Cada valor é (desde, normal, com_gorjeta), em centavos por hora. `com_gorjeta` é o mínimo em
dinheiro para quem recebe gorjeta (tipped cash wage). None = o lugar não aceita tip credit
(quem recebe gorjeta ganha o mínimo cheio).

Fontes (conferidas em 19/11/2026):
- Estados: U.S. Department of Labor, "State Minimum Wage Laws" e "Minimum Wages for Tipped
  Employees" (atualizados em 05/11/2026). Aumentos de 01/01/2027 já anunciados pelos estados.
- Cidades: Washington L&I (local minimum wage rates), Colorado CDLE, cidade de St. Paul,
  UC Berkeley Labor Center (Califórnia), GovDocs (01/07/2026 e 01/01/2027) e guias jurídicos.
Onde a lei tem faixas por tamanho de empresa, usamos a de empresa grande (a mais alta).
Atualize esta tabela pelo menos duas vezes por ano (janeiro e julho)."""
from datetime import date, timedelta

ATUALIZADO_EM = date(2026, 11, 19)
D26, D27 = date(2026, 1, 1), date(2027, 1, 1)
FEDERAL = [(date(2009, 7, 24), 725, 213)]

# estado -> lista de (desde, normal, com_gorjeta)
ESTADOS: dict[str, list[tuple[date, int, int | None]]] = {
    "AL": [(D26, 725, 213)], "LA": [(D26, 725, 213)], "MS": [(D26, 725, 213)],
    "SC": [(D26, 725, 213)], "TN": [(D26, 725, 213)], "GA": [(D26, 725, 213)],
    "WY": [(D26, 725, 213)], "ID": [(D26, 725, 335)], "IN": [(D26, 725, 213)],
    "IA": [(D26, 725, 435)], "KS": [(D26, 725, 213)], "KY": [(D26, 725, 213)],
    "NH": [(D26, 725, 327)], "NC": [(D26, 725, 213)], "ND": [(D26, 725, 486)],
    "OK": [(D26, 725, 213)], "PA": [(D26, 725, 283)], "TX": [(D26, 725, 213)],
    "UT": [(D26, 725, 213)], "WI": [(D26, 725, 233)], "WV": [(D26, 875, 262)],
    "AK": [(D26, 1400, None)],
    "AZ": [(D26, 1515, 1215), (D27, 1565, 1265)],
    "AR": [(D26, 1100, 263)],
    "CA": [(D26, 1690, None), (D27, 1740, None)],
    "CO": [(D26, 1516, 1214), (D27, 1571, 1269)],
    "CT": [(D26, 1694, 638), (D27, 1748, 638)],
    "DE": [(D26, 1500, 223)],
    "DC": [(date(2026, 7, 1), 1840, 1030)],
    "FL": [(date(2026, 9, 30), 1500, 1198)],
    "HI": [(D26, 1600, 1475)],
    "IL": [(D26, 1500, 900)],
    "ME": [(D26, 1510, 755), (D27, 1570, 785)],
    "MD": [(D26, 1500, 363)],
    "MA": [(D26, 1500, 675)],
    "MI": [(D26, 1373, 549), (D27, 1500, 630)],
    "MN": [(D26, 1141, None), (D27, 1187, None)],
    "MO": [(D26, 1500, 750)],
    "MT": [(D26, 1085, None), (D27, 1120, None)],
    "NE": [(D26, 1500, 213), (D27, 1526, 213)],
    "NV": [(D26, 1200, None)],
    "NJ": [(D26, 1592, 605), (D27, 1648, 605)],
    "NM": [(D26, 1200, 300)],
    "NY": [(D26, 1600, 1070)],
    "OH": [(D26, 1100, 550), (D27, 1140, 570)],
    "OR": [(date(2026, 7, 1), 1555, None)],
    "RI": [(D26, 1600, 389), (D27, 1700, 389)],
    "SD": [(D26, 1185, 593), (D27, 1230, 615)],
    "VT": [(D26, 1442, 721), (D27, 1491, 746)],
    "VA": [(D26, 1277, 213), (D27, 1375, 213)],
    "WA": [(D26, 1713, None), (D27, 1773, None)],
}

# chave -> (nome, estado, lista, substitui_estado). substitui_estado=True quando a região tem
# mínimo MENOR que o padrão do estado (ex.: condados não urbanos do Oregon).
J1, J7 = D26, date(2026, 7, 1)
CIDADES: dict[str, tuple[str, str, list, bool]] = {
    # Washington (sem tip credit)
    "seattle": ("Seattle", "WA", [(J1, 2130, None), (D27, 2214, None)], False),
    "tukwila": ("Tukwila", "WA", [(J1, 2165, None), (D27, 2245, None)], False),
    "renton": ("Renton", "WA", [(J1, 2157, None), (D27, 2228, None)], False),
    "burien": ("Burien", "WA", [(J1, 2178, None), (D27, 2249, None)], False),
    "king": ("King County (unincorporated)", "WA", [(J1, 2082, None), (D27, 2157, None)], False),
    "seatac": ("SeaTac (hospitality and transportation)", "WA", [(J1, 2074, None), (D27, 2147, None)], False),
    "everett": ("Everett", "WA", [(J1, 2077, None)], False),
    "bellingham": ("Bellingham", "WA", [(J1, 1913, None)], False),
    # Califórnia (sem tip credit)
    "alameda": ("Alameda", "CA", [(J7, 1776, None)], False),
    "belmont": ("Belmont", "CA", [(J1, 1895, None)], False),
    "berkeley": ("Berkeley", "CA", [(J7, 1961, None)], False),
    "burlingame": ("Burlingame", "CA", [(J1, 1786, None)], False),
    "cupertino": ("Cupertino", "CA", [(J1, 1870, None)], False),
    "dalycity": ("Daly City", "CA", [(J1, 1750, None)], False),
    "eastpaloalto": ("East Palo Alto", "CA", [(J1, 1790, None)], False),
    "elcerrito": ("El Cerrito", "CA", [(J1, 1882, None)], False),
    "emeryville": ("Emeryville", "CA", [(J7, 2034, None)], False),
    "fostercity": ("Foster City", "CA", [(J1, 1785, None)], False),
    "fremont": ("Fremont", "CA", [(J7, 1805, None)], False),
    "halfmoonbay": ("Half Moon Bay", "CA", [(J1, 1791, None)], False),
    "hayward": ("Hayward", "CA", [(J1, 1779, None)], False),
    "losaltos": ("Los Altos", "CA", [(J1, 1870, None)], False),
    "losangeles": ("Los Angeles (city)", "CA", [(J7, 1842, None)], False),
    "lacounty": ("Los Angeles County (unincorporated)", "CA", [(J7, 1847, None)], False),
    "malibu": ("Malibu", "CA", [(J7, 1791, None)], False),
    "menlopark": ("Menlo Park", "CA", [(J1, 1755, None)], False),
    "milpitas": ("Milpitas", "CA", [(J7, 1850, None)], False),
    "mountainview": ("Mountain View", "CA", [(J1, 1970, None)], False),
    "novato": ("Novato", "CA", [(J1, 1773, None)], False),
    "oakland": ("Oakland", "CA", [(J1, 1734, None)], False),
    "paloalto": ("Palo Alto", "CA", [(J1, 1870, None)], False),
    "pasadena": ("Pasadena", "CA", [(J7, 1857, None)], False),
    "petaluma": ("Petaluma", "CA", [(J1, 1831, None)], False),
    "redwoodcity": ("Redwood City", "CA", [(J1, 1865, None)], False),
    "richmond": ("Richmond", "CA", [(J1, 1918, None)], False),
    "sancarlos": ("San Carlos", "CA", [(J1, 1775, None)], False),
    "sandiego": ("San Diego", "CA", [(J1, 1775, None)], False),
    "sanfrancisco": ("San Francisco", "CA", [(J7, 1961, None)], False),
    "sanjose": ("San Jose", "CA", [(J1, 1845, None)], False),
    "sanmateo": ("San Mateo", "CA", [(J1, 1860, None)], False),
    "sanmateocounty": ("San Mateo County (unincorporated)", "CA", [(J1, 1795, None)], False),
    "santaclara": ("Santa Clara", "CA", [(J1, 1870, None)], False),
    "santamonica": ("Santa Monica", "CA", [(J7, 1847, None)], False),
    "santarosa": ("Santa Rosa", "CA", [(J1, 1821, None)], False),
    "southsf": ("South San Francisco", "CA", [(J1, 1815, None)], False),
    "sonoma": ("Sonoma", "CA", [(J1, 1847, None)], False),
    "sunnyvale": ("Sunnyvale", "CA", [(J1, 1950, None)], False),
    "westhollywood": ("West Hollywood", "CA", [(J1, 2025, None)], False),
    # Colorado (tip credit de $3,02, salvo indicação)
    "denver": ("Denver", "CO", [(J1, 1929, 1627), (D27, 1984, 1682)], False),
    "edgewater": ("Edgewater", "CO", [(J1, 1817, 1350), (D27, 1984, None)], False),
    "boulder": ("Boulder (city)", "CO", [(J1, 1682, 1380), (D27, 1817, 1515)], False),
    "bouldercounty": ("Boulder County (unincorporated)", "CO", [(J1, 1682, 1380), (D27, 1743, 1441)], False),
    # Illinois
    "chicago": ("Chicago", "IL", [(J7, 1705, 1432)], False),
    "cook": ("Cook County", "IL", [(J7, 1540, 924)], False),
    # Minnesota (sem tip credit)
    "minneapolis": ("Minneapolis", "MN", [(J1, 1637, None), (D27, 1702, None)], False),
    "stpaul": ("Saint Paul", "MN", [(J1, 1637, None)], False),
    # Nova York: NYC, Long Island e Westchester
    "nyc": ("New York City, Long Island and Westchester", "NY", [(J1, 1700, 1135)], False),
    # Oregon: regiões
    "portlandmetro": ("Oregon: Portland metro area", "OR", [(J7, 1680, None)], False),
    "ornaourbano": ("Oregon: non-urban counties", "OR", [(J7, 1455, None)], True),
    # Maryland
    "montgomery": ("Montgomery County", "MD", [(J7, 1800, 400)], False),
    # Arizona
    "flagstaff": ("Flagstaff", "AZ", [(J1, 1835, None)], False),
    "tucson": ("Tucson", "AZ", [(J1, 1545, 1245)], False),
    # Maine
    "portlandme": ("Portland", "ME", [(J1, 1675, 838), (D27, 1775, 888)], False),
    # Novo México
    "lascruces": ("Las Cruces", "NM", [(J1, 1301, None)], False),
}


def _vigente(lista, d: date):
    atual = None
    for linha in lista:
        if linha[0] <= d:
            atual = linha
    return atual


def _valor(linha, gorjeta: bool, credito_estado: int | None = None) -> int:
    _, normal, tip = linha
    if not gorjeta:
        return normal
    if tip is not None:
        return tip
    if credito_estado:   # cidade sem valor próprio de gorjeta: o mesmo crédito do estado
        return max(213, normal - credito_estado)
    return normal


def cidades_do(estado: str) -> list[tuple[str, str]]:
    return sorted(((k, v[0]) for k, v in CIDADES.items() if v[1] == estado), key=lambda x: x[1])


def minimo(e, d: date, gorjeta: bool = False) -> dict:
    """Mínimo que vale para a empresa no dia `d`: {'centavos', 'fonte'}."""
    from .apuracao import ESTADOS as NOMES
    fed = _vigente(FEDERAL, d)
    melhor = (_valor(fed, gorjeta), "Federal")
    est = _vigente(ESTADOS.get(e.estado, []), d)
    credito = None
    if est:
        credito = est[1] - est[2] if est[2] is not None else 0
        v = _valor(est, gorjeta)
        if v > melhor[0]:
            melhor = (v, NOMES.get(e.estado, e.estado))
    cid = CIDADES.get(e.cidade_minimo or "")
    if cid and cid[1] == e.estado:
        linha = _vigente(cid[2], d)
        if linha:
            v = _valor(linha, gorjeta, credito)
            if cid[3]:
                melhor = (max(v, _valor(fed, gorjeta)), cid[0])
            elif v > melhor[0]:
                melhor = (v, cid[0])
    proprio = e.minimo_gorjeta_proprio_centavos if gorjeta else e.minimo_proprio_centavos
    if proprio and proprio > melhor[0]:
        melhor = (proprio, "Valor informado pela empresa")
    return {"centavos": melhor[0], "fonte": melhor[1]}


def proxima_mudanca(e, d: date, dias: int = 60) -> dict | None:
    """Próximo aumento já conhecido nos próximos `dias` dias (ou None)."""
    datas = set()
    for lista in (ESTADOS.get(e.estado, []), (CIDADES.get(e.cidade_minimo or "") or ("", "", [], False))[2]):
        datas.update(linha[0] for linha in lista if d < linha[0] <= d + timedelta(days=dias))
    for alvo in sorted(datas):
        n, g = minimo(e, alvo), minimo(e, alvo, True)
        if n["centavos"] != minimo(e, d)["centavos"] or g["centavos"] != minimo(e, d, True)["centavos"]:
            return {"data": alvo, "normal": n, "gorjeta": g}
    return None


def abaixo(db, e, d: date) -> list[dict]:
    """Pessoas (e funções) pagas por hora abaixo do mínimo no dia `d`.
    Não vale para exempt nem para prestador 1099. Valor da hora 0 = ainda não cadastrado."""
    from sqlalchemy import select
    from ...db import FuncaoUsuario, Usuario
    pessoas = list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                    Usuario.na_escala.is_(True))))
    funcoes: dict[int, list] = {}
    for f in db.scalars(select(FuncaoUsuario).where(FuncaoUsuario.empresa_id == e.id,
                                                    FuncaoUsuario.tipo == "hora")):
        funcoes.setdefault(f.usuario_id, []).append(f)
    n, g = minimo(e, d), minimo(e, d, True)
    lista = []
    for u in sorted(pessoas, key=lambda x: x.nome.lower()):
        if u.isento or (u.classificacao or "w2").startswith("1099"):
            continue
        m = g if u.gorjeta else n
        valores = [("", u.taxa_hora_centavos)] + [(f.nome, f.valor_centavos) for f in funcoes.get(u.id, [])]
        vistos = set()
        for nome, v in valores:
            if v and v < m["centavos"] and (nome, v) not in vistos and v not in {x[1] for x in vistos}:
                vistos.add((nome, v))
                lista.append({"u": u, "funcao": nome, "valor": v, "minimo": m["centavos"],
                              "fonte": m["fonte"], "gorjeta": u.gorjeta})
    return lista


def dinheiro(c: int) -> str:
    return f"${c / 100:,.2f}"


def aviso(lista: list[dict]) -> str:
    """Frase curta para o aviso depois de salvar (traduzida pelo modelo do texto)."""
    nomes = ", ".join(f"{x['u'].nome} ({dinheiro(x['valor'])} < {dinheiro(x['minimo'])})" for x in lista[:5])
    return f"Atenção: valor da hora abaixo do salário mínimo do local: {nomes}. Corrija antes de pagar."


def situacao(db, e, d: date) -> dict:
    """Tudo o que as telas mostram: mínimos de hoje, próximo aumento e quem fica abaixo."""
    prox = proxima_mudanca(e, d)
    return {"normal": minimo(e, d), "gorjeta": minimo(e, d, True), "proxima": prox,
            "abaixo": abaixo(db, e, d), "abaixo_depois": abaixo(db, e, prox["data"]) if prox else []}


# ------------------------------------------------------------------ tela: cidade / mínimo próprio
from fastapi import APIRouter, Depends, Form, Request  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

rota = APIRouter()


def _registrar_rota():
    from ... import regras
    from ...core import avisar, exigir_csrf, gestor_logado, ir, ler_centavos
    from ...db import Usuario, get_db

    @rota.post("/painel/config/salario-minimo-eua", dependencies=[Depends(exigir_csrf)])
    def salvar(request: Request, cidade: str = Form(""), proprio: str = Form(""), proprio_gorjeta: str = Form(""),
               u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
        e = u.empresa
        n, g = ler_centavos(proprio.strip() or "0"), ler_centavos(proprio_gorjeta.strip() or "0")
        if cidade and (cidade not in CIDADES or CIDADES[cidade][1] != e.estado):
            avisar(request, "Escolha uma cidade do estado da empresa.", "erro")
        elif n is None or g is None or n > 10000 or g > 10000:
            avisar(request, "Use valores em dólares por hora, como 16.50.", "erro")
        else:
            e.cidade_minimo, e.minimo_proprio_centavos, e.minimo_gorjeta_proprio_centavos = cidade, n, g
            m = minimo(e, regras.hoje(e))
            regras.registrar(db, e.id, u.nome, "salário mínimo local",
                             f"{m['fonte']}: {dinheiro(m['centavos'])} por hora.")
            db.commit()
            avisar(request, "Salário mínimo do local salvo.")
        return ir("/painel/config#salario-minimo")


_registrar_rota()
