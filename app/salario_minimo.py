"""Salário mínimo no Brasil: o mínimo nacional (com data de início) e o piso que a empresa
informar (piso estadual, piso da categoria na convenção coletiva). Vale o maior.

Os valores são mensais, para a jornada de 220 horas. Para quem tem jornada menor, o mínimo é
proporcional (mínimo ÷ 220 × jornada mensal da pessoa); para quem recebe por hora, vale o
mínimo-hora (mínimo ÷ 220).

Fonte: Decreto nº 12.797, de 23/12/2025 (R$ 1.621,00 a partir de 01/01/2026; R$ 7,37 a hora).
Atualize esta tabela todo ano, quando sair o decreto de janeiro."""
from datetime import date, timedelta

ATUALIZADO_EM = date(2026, 11, 19)
JORNADA_PADRAO = 220
# (desde, valor mensal em centavos)
NACIONAL = [(date(2025, 1, 1), 151800), (date(2026, 1, 1), 162100)]

# Não há tabela de cidades no Brasil (o piso estadual e o da categoria vêm da própria empresa).
CIDADES: dict = {}


def _vigente(lista, d: date):
    atual = None
    for linha in lista:
        if linha[0] <= d:
            atual = linha
    return atual


def cidades_do(estado: str = "") -> list:
    return []


def minimo(e, d: date, gorjeta: bool = False) -> dict:
    """Mínimo mensal (220 h) que vale para a empresa no dia `d`: {'centavos', 'fonte', 'hora'}.
    A gorjeta não reduz o mínimo no Brasil (o parâmetro fica por compatibilidade)."""
    nac = _vigente(NACIONAL, d)
    melhor = (nac[1] if nac else 0, "Salário mínimo nacional")
    proprio = e.minimo_proprio_centavos or 0
    if proprio > melhor[0]:
        melhor = (proprio, "Piso informado pela empresa")
    return {"centavos": melhor[0], "fonte": melhor[1], "hora": round(melhor[0] / JORNADA_PADRAO)}


def proxima_mudanca(e, d: date, dias: int = 60) -> dict | None:
    for desde, _ in NACIONAL:
        if d < desde <= d + timedelta(days=dias):
            n = minimo(e, desde)
            if n["centavos"] != minimo(e, d)["centavos"]:
                return {"data": desde, "normal": n, "gorjeta": n}
    return None


def proporcional(m: dict, jornada: int) -> int:
    return round(m["centavos"] * (jornada or JORNADA_PADRAO) / JORNADA_PADRAO)


def abaixo(db, e, d: date) -> list[dict]:
    """Pessoas com salário abaixo do mínimo proporcional à jornada, e funções pagas por hora
    abaixo do mínimo-hora. Não vale para PJ nem para estagiário (bolsa). Salário 0 = ainda
    não cadastrado (não entra)."""
    from sqlalchemy import select
    from .db import FuncaoUsuario, Usuario
    pessoas = list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                    Usuario.na_escala.is_(True))))
    por_hora: dict[int, list] = {}
    for f in db.scalars(select(FuncaoUsuario).where(FuncaoUsuario.empresa_id == e.id,
                                                    FuncaoUsuario.tipo == "hora")):
        por_hora.setdefault(f.usuario_id, []).append(f)
    m = minimo(e, d)
    lista = []
    for u in sorted(pessoas, key=lambda x: x.nome.lower()):
        if (u.classificacao or "clt") in ("pj", "estagio"):
            continue
        piso = proporcional(m, u.jornada_mensal)
        if u.salario_centavos and u.salario_centavos < piso:
            lista.append({"u": u, "funcao": "", "valor": u.salario_centavos, "minimo": piso,
                          "fonte": m["fonte"], "hora": False})
        for f in por_hora.get(u.id, []):
            if f.valor_centavos and f.valor_centavos < m["hora"]:
                lista.append({"u": u, "funcao": f.nome, "valor": f.valor_centavos, "minimo": m["hora"],
                              "fonte": m["fonte"], "hora": True})
    return lista


def dinheiro(c: int) -> str:
    return "R$ " + f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def aviso(lista: list[dict]) -> str:
    """Frase curta para o aviso depois de salvar."""
    nomes = ", ".join(f"{x['u'].nome} ({dinheiro(x['valor'])} < {dinheiro(x['minimo'])})" for x in lista[:5])
    return f"Atenção: salário abaixo do mínimo proporcional à jornada: {nomes}. Corrija antes de pagar."


def situacao(db, e, d: date) -> dict:
    """Tudo o que as telas mostram: mínimo de hoje, próximo aumento e quem fica abaixo."""
    prox = proxima_mudanca(e, d)
    n = minimo(e, d)
    return {"normal": n, "gorjeta": n, "proxima": prox, "nacional": _vigente(NACIONAL, d)[1],
            "abaixo": abaixo(db, e, d), "abaixo_depois": abaixo(db, e, prox["data"]) if prox else []}


# ------------------------------------------------------------------ tela: piso próprio
from fastapi import APIRouter, Depends, Form, Request  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

rota = APIRouter()


def _registrar_rota():
    from . import regras
    from .core import avisar, exigir_csrf, gestor_logado, ir, ler_centavos
    from .db import Usuario, get_db

    @rota.post("/painel/config/salario-minimo", dependencies=[Depends(exigir_csrf)])
    def salvar(request: Request, proprio: str = Form(""),
               u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
        e = u.empresa
        n = ler_centavos(proprio.strip() or "0")
        if n is None or n > 10_000_000:
            avisar(request, "Use o valor mensal do piso, como 1.750,00.", "erro")
        else:
            e.minimo_proprio_centavos = n
            m = minimo(e, regras.hoje(e))
            regras.registrar(db, e.id, u.nome, "piso salarial",
                             f"{m['fonte']}: {dinheiro(m['centavos'])} por mês (220 h).")
            db.commit()
            avisar(request, "Piso salarial salvo.")
        return ir("/painel/config#salario-minimo")


_registrar_rota()
