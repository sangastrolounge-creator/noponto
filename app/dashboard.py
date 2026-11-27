"""Dados e geometria dos gráficos da tela Hoje (SVG feito no servidor: sem biblioteca externa)."""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import core, regras
from .db import Feriado, Turno, Usuario

DIAS_CURTOS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
LARGURA, ALTURA, TOPO, BASE = 360, 176, 18, 24     # área do gráfico (px do viewBox ≈ px na tela)


def _passo(maximo: float) -> float:
    """Intervalo "redondo" para as linhas de grade (1, 2, 5 × 10^n)."""
    if maximo <= 0:
        return 1
    bruto = maximo / 4
    pot = 10 ** (len(str(int(bruto))) - 1) if bruto >= 1 else 1
    for m in (1, 2, 5, 10):
        if bruto <= m * pot:
            return m * pot
    return 10 * pot


def barras(itens: list[dict], referencia: float | None = None) -> dict:
    """itens: [{rotulo, valor, dica, destaque}] -> retângulos e grade prontos para o SVG.
    `referencia`: valor de uma linha tracejada (meta)."""
    n = max(len(itens), 1)
    maximo = max([i["valor"] for i in itens] + [0, referencia or 0])
    passo = _passo(maximo)
    teto = max(passo * 4, passo * (int(maximo / passo) + (1 if maximo % passo else 0))) or 1
    util = ALTURA - TOPO - BASE
    largura_col = (LARGURA - 44) / n
    larg = min(46, largura_col * 0.56)
    saida = []
    for k, i in enumerate(itens):
        h = 0 if teto == 0 else util * i["valor"] / teto
        x = 44 + k * largura_col + (largura_col - larg) / 2
        y = TOPO + util - h
        r = min(4, h / 2, larg / 2)
        # topo arredondado (4px) e base reta, presa no eixo
        def c(letra, *nums):
            return letra + ",".join("{:.1f}".format(v) for v in nums)
        pts = [c("M", x, TOPO + util), c("V", y + r), c("Q", x, y), c("", x + r, y), c("H", x + larg - r),
               c("Q", x + larg, y), c("", x + larg, y + r), c("V", TOPO + util), "Z"]
        caminho = " ".join(pts) if h > 0 else ""
        saida.append(dict(i, x=x, y=y, w=larg, h=h, cx=x + larg / 2, caminho=caminho,
                          alvo_x=44 + k * largura_col, alvo_w=largura_col))
    grade = []
    v = 0
    while v <= teto + 1e-9:
        grade.append(dict(valor=v, y=TOPO + util - util * v / teto))
        v += passo
    ref_y = TOPO + util - util * referencia / teto if referencia else None
    return dict(barras=saida, grade=grade, largura=LARGURA, altura=ALTURA, base_y=TOPO + util, ref_y=ref_y)


def grafico_cmo(linhas: list[dict], meta: int) -> dict:
    """CMO em % do faturamento, do mês mais antigo ao atual, com a linha da meta."""
    itens = [dict(rotulo=l["nome"].split("/")[0], valor=l["pct"] or 0, destaque=l["atual"], linha=l,
                  rotulo_valor=(f"{l['pct']:.0f}%" if l["pct"] is not None else "—"))
             for l in reversed(linhas)]
    return barras(itens, referencia=meta)


def semana(db: Session, e, dia: date, resumo: list[dict] | None) -> dict:
    """Últimos 7 dias: horas trabalhadas (com atrasos na dica) e custo da equipe."""
    ini = dia - timedelta(days=6)
    linhas, _ = regras.espelho(db, e, ini, dia)
    por_dia = {ini + timedelta(days=k): dict(minutos=0, pessoas=0, atrasos=0) for k in range(7)}
    for l in linhas:
        if l["dia"] in por_dia:
            r = por_dia[l["dia"]]
            r["minutos"] += l["minutos"]
            r["pessoas"] += 1
            r["atrasos"] += 1 if l["atraso"] > (e.tolerancia_min or 0) else 0
    horas = barras([dict(rotulo=("Hoje" if d == dia else DIAS_CURTOS[d.weekday()]), data=d,
                         valor=r["minutos"] / 60, destaque=d == dia,
                         texto=regras.fmt_min(r["minutos"]), pessoas=r["pessoas"], atrasos=r["atrasos"])
                    for d, r in por_dia.items()])
    custo = None
    if resumo:
        custo = barras([dict(rotulo=("Hoje" if x["dia"] == dia else DIAS_CURTOS[x["dia"].weekday()]),
                             data=x["dia"], valor=x["custo"] / 100, destaque=x["dia"] == dia,
                             centavos=x["custo"], vendas=x["vendas"], pct=x["pct"],
                             rotulo_valor=(f"{x['pct']:.0f}%" if x["pct"] is not None else ""))
                        for x in reversed(resumo)])
    total_min = sum(r["minutos"] for r in por_dia.values())
    return dict(horas=horas, custo=custo, total_min=total_min,
                atrasos=sum(r["atrasos"] for r in por_dia.values()))


def situacao_agora(linhas: list[dict]) -> list[dict]:
    """Quantas pessoas em cada situação agora, na ordem da barra empilhada."""
    grupos = [("trabalhando", "Trabalhando", ("trabalhando",)),
              ("atrasado", "Chegaram atrasados", ()),
              ("sem_ponto", "Sem ponto", ("sem_ponto", "falta")),
              ("aguardando", "Ainda não começou", ("aguardando",)),
              ("encerrado", "Encerrou o turno", ("encerrado",)),
              ("sem_saida", "Sem saída registrada", ("sem_saida",)),
              ("folga", "Folga ou ausência", ("folga", "ausencia"))]
    cont = {g[0]: 0 for g in grupos}
    for l in linhas:
        cod, cor = l["sit"]["cod"], l["sit"]["cor"]
        if cor == "alerta" and cod not in ("sem_saida",):
            cont["atrasado"] += 1
            continue
        for chave, _nome, cods in grupos:
            if cod in cods:
                cont[chave] += 1
                break
        else:
            cont["aguardando"] += 1
    total = sum(cont.values()) or 1
    return [dict(chave=c, nome=n, qtd=cont[c], pct=cont[c] * 100 / total) for c, n, _ in grupos if cont[c]]


def saudacao(agora_l) -> str:
    h = agora_l.hour
    return "Bom dia" if 5 <= h < 12 else "Boa tarde" if 12 <= h < 18 else "Boa noite"


def sugestoes(db: Session, e, u, dia: date) -> list[dict]:
    """Próximos passos que fazem diferença para esta empresa (o card "Sugestões do sistema")."""
    s = []
    eua = e.pais == "US"
    if not e.triagem_em and core.pode(u, "dono") and not eua:   # configuração guiada: só Brasil
        s.append(dict(titulo="Configure em 2 minutos", link="/painel/comecar", botao="Começar",
                      texto="Responda 7 perguntas e deixamos funções, checklist, regras do seu estado e modelos de turno prontos."))
    tem_equipe = db.scalar(select(Usuario.id).where(Usuario.empresa_id == e.id,
                                                    Usuario.papel == "funcionario").limit(1))
    if not tem_equipe and core.pode(u, "equipe"):
        s.append(dict(titulo="Cadastre a sua equipe", link="/painel/equipe", botao="Cadastrar",
                      texto="Cada pessoa recebe um link para bater o ponto pelo celular."))
    if core.pode(u, "ausencias") and not db.scalar(select(func.count(Feriado.id)).where(
            Feriado.empresa_id == e.id, Feriado.data.between(date(dia.year, 1, 1), date(dia.year, 12, 31)))):
        s.append(dict(titulo=f"Cadastre os feriados de {dia.year}", link="/painel/ausencias#feriados",
                      botao="Cadastrar",
                      texto="Em feriado ninguém deve horas; o adicional de feriado segue a regra da empresa em Configurações."
                      if eua else "Em feriado ninguém deve horas, e quem trabalha recebe a hora como extra."))
    seg = dia - timedelta(days=dia.weekday()) + timedelta(days=7)
    if tem_equipe and core.pode(u, "escala") and dia.weekday() >= 3 and not db.scalar(
            select(Turno.id).where(Turno.empresa_id == e.id, Turno.data.between(seg, seg + timedelta(days=6))).limit(1)):
        s.append(dict(titulo="Monte a escala da próxima semana", link=f"/painel/escala?semana={seg.isoformat()}",
                      botao="Montar escala", texto="Com a escala publicada, a equipe vê os turnos no celular."))
    if e.lat is None and core.pode(u, "dono"):
        s.append(dict(titulo="Defina o local da empresa", link="/painel/config", botao="Definir",
                      texto="Sem o local, o ponto é aceito de qualquer lugar."))
    if not e.usa_faturamento and core.pode(u, "dono"):
        s.append(dict(titulo="Acompanhe o CMO pelo faturamento", link="/painel/config#faturamento", botao="Ligar",
                      texto="Se a empresa vende produtos ou serviços, informe o faturamento do mês e veja quanto a equipe pesa nele."))
    if core.pode(u, "ponto"):
        s.append(dict(titulo="Confira o Ajuste de ponto", link="/painel/ponto/ajuste", botao="Abrir",
                      texto="Corrija marcações com um clique, lance ausências e o motivo de cada ajuste." if eua
                      else "Corrija marcações com um clique, lance atestados, sobreaviso e o motivo de cada ajuste."))
    return s
