"""Central de conformidade: junta numa tela só as conferências trabalhistas que o sistema já faz
(salário mínimo, menores, hora extra, refeição, pausas da Califórnia, escala justa, exempt,
ponto incompleto e cadastro), com o que fazer em cada uma e uma nota geral.

Cada item: {"chave", "titulo", "situacao": ok | atencao | problema | info, "resumo", "detalhes": [...],
"acao": (rótulo, link) ou None}. A nota é a parte dos itens aplicáveis que estão ok
(atenção conta meio ponto)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import regras
from . import apuracao
from ...core import gestor_logado, pode, render
from ...db import Turno, Usuario, get_db

rota = APIRouter()
EXEMPT_FEDERAL = 68400   # US$ 684 por semana (regra federal em vigor)
SEMANAS = {"weekly": 1, "biweekly": 2, "semimonthly": 52 / 24, "monthly": 52 / 12}


def _item(chave, titulo, situacao, resumo, detalhes=None, acao=None):
    return {"chave": chave, "titulo": titulo, "situacao": situacao, "resumo": resumo,
            "detalhes": detalhes or [], "acao": acao}


def _dinheiro(c):
    return f"${c / 100:,.2f}"


def _equipe(db, e):
    return list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                 Usuario.na_escala.is_(True)).order_by(Usuario.nome)))


def verificar(db: Session, e, u) -> dict:
    from . import descanso, escala_justa, menores, salario_minimo
    from ...custos import minutos_trabalhados
    from ...ponto_problemas import problemas
    hoje = regras.hoje(e)
    equipe = _equipe(db, e)
    itens = []
    ver_pag = pode(u, "pagamento") or pode(u, "equipe")

    # 1. salário mínimo
    if ver_pag:
        sm = salario_minimo.situacao(db, e, hoje)
        if sm["abaixo"]:
            itens.append(_item("minimo", "Salário mínimo", "problema",
                               f"{len(sm['abaixo'])} valor(es) da hora abaixo do mínimo ({sm['normal']['fonte']}: {_dinheiro(sm['normal']['centavos'])}).",
                               [f"{x['u'].nome}: {_dinheiro(x['valor'])}" for x in sm["abaixo"]],
                               ("Corrigir valores", "/painel/pagamento#dados")))
        elif sm["abaixo_depois"]:
            p = sm["proxima"]
            itens.append(_item("minimo", "Salário mínimo", "atencao",
                               f"Em {p['data']:%m/%d/%Y} o mínimo sobe para {_dinheiro(p['normal']['centavos'])}: {len(sm['abaixo_depois'])} pessoa(s) ficam abaixo.",
                               [f"{x['u'].nome}: {_dinheiro(x['valor'])}" for x in sm["abaixo_depois"]],
                               ("Ajustar valores", "/painel/pagamento#dados")))
        else:
            itens.append(_item("minimo", "Salário mínimo", "ok",
                               f"Todos acima do mínimo ({sm['normal']['fonte']}: {_dinheiro(sm['normal']['centavos'])})."))
        sem_valor = [f.nome for f in equipe if not f.isento and not f.taxa_hora_centavos]
        if sem_valor:
            itens.append(_item("sem_valor", "Valor da hora cadastrado", "atencao",
                               f"{len(sem_valor)} pessoa(s) sem valor da hora: o sistema não confere o mínimo nem calcula o custo.",
                               sem_valor[:10], ("Cadastrar valores", "/painel/pagamento#dados")))

    # 2. exempt abaixo do salário mínimo de exempt
    if ver_pag:
        limite = EXEMPT_FEDERAL
        fonte = "federal"
        if e.estado == "CA":
            est = salario_minimo._vigente(salario_minimo.ESTADOS["CA"], hoje)   # 2x o mínimo do ESTADO
            limite = max(limite, 2 * est[1] * 40)
            fonte = "Califórnia"
        baixos = []
        for f in equipe:
            if f.isento and f.salario_periodo_centavos:
                semana = int(f.salario_periodo_centavos / SEMANAS.get(e.frequencia, 2))
                if semana < limite:
                    baixos.append(f"{f.nome}: {_dinheiro(semana)}/semana")
        if any(f.isento for f in equipe):
            itens.append(_item("exempt", "Salário de exempt", "problema" if baixos else "ok",
                               f"{len(baixos)} exempt com salário abaixo do mínimo para exempt ({fonte}: {_dinheiro(limite)} por semana)."
                               if baixos else f"Salários de exempt acima do mínimo ({fonte}: {_dinheiro(limite)} por semana).",
                               baixos, ("Ver dados de pagamento", "/painel/pagamento#dados") if baixos else None))

    # 3. menores de idade
    seg = hoje - timedelta(days=hoje.weekday())
    dias = [seg + timedelta(days=i) for i in range(14)]
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(dias[0], dias[-1])))}
    if any(f.nascimento for f in equipe):
        det = []
        for bloco in (dias[:7], dias[7:]):
            m = menores.conferir_grade(db, e, equipe, turnos, bloco)
            nomes = {f.id: f.nome for f in equipe}
            det += [f"{nomes[uid]}, {d:%m/%d}: " + "; ".join(v) for (uid, d), v in sorted(m["dia"].items(), key=lambda x: x[0][1])]
            det += [f"{nomes[uid]}: {v}" for uid, v in m["semana"].items() if not v.startswith("16 ou 17")]
        det = list(dict.fromkeys(det))
        itens.append(_item("menores", "Trabalho de menores", "problema" if det else "ok",
                           f"{len(det)} ponto(s) da escala passam dos limites da lei para menores (esta semana e a próxima)."
                           if det else "A escala desta semana e da próxima respeita os limites para menores.",
                           det[:12], ("Abrir a escala", "/painel/escala") if det else None))

    # 4. hora extra da semana
    if e.ot_semana_min:
        ini = apuracao.inicio_semana(e, hoje)
        semana = {}
        d = ini
        while d <= hoje:
            for uid, m in minutos_trabalhados(db, e, d).items():
                semana[uid] = semana.get(uid, 0) + m
            d += timedelta(days=1)
        nomes = {f.id: f for f in equipe}
        acima = [(nomes[uid].nome, m) for uid, m in semana.items() if uid in nomes and not nomes[uid].isento
                 and m >= e.ot_semana_min]
        perto = [(nomes[uid].nome, m) for uid, m in semana.items() if uid in nomes and not nomes[uid].isento
                 and e.ot_semana_min - 240 <= m < e.ot_semana_min]
        sit = "atencao" if acima or perto else "ok"
        resumo = (f"{len(acima)} pessoa(s) em hora extra nesta semana e {len(perto)} a menos de 4 h dela."
                  if sit != "ok" else "Ninguém perto da hora extra nesta semana.")
        itens.append(_item("extra", "Hora extra", sit, resumo,
                           [f"{n}: {regras.fmt_min(m)}" for n, m in sorted(acima + perto, key=lambda x: -x[1])],
                           ("Ver o ponto", "/painel/ponto") if sit != "ok" else None))

    # 5. refeição e intervalo no período aberto
    ini, fim = apuracao.periodo_aberto(db, e)
    resultado = apuracao.apurar(db, e, ini, fim)
    sem_int = [(r["u"].nome, r["sem_intervalo"]) for r in resultado if r["sem_intervalo"]]
    meal = [(r["u"].nome, r["meal_premios"] // 60) for r in resultado if r["meal_premios"]]
    if e.intervalo_obrigatorio or e.meal_premium:
        det = [f"{n}: {q} dia(s) sem bater a refeição" for n, q in sem_int] + \
              [f"{n}: {q} meal premium" for n, q in meal]
        itens.append(_item("refeicao", "Refeição e intervalo", "atencao" if det else "ok",
                           f"No período aberto ({ini:%m/%d}–{fim:%m/%d}): {len(sem_int)} pessoa(s) com refeição não batida e {sum(q for _, q in meal)} meal premium."
                           if det else f"Refeições em dia no período aberto ({ini:%m/%d}–{fim:%m/%d}).",
                           det[:12], ("Ver o ponto", "/painel/ponto") if det else None))

    # 6. pausas da Califórnia
    if descanso.ativo(e):
        lista = descanso.para_revisar(db, e)
        sem = [x for x in lista if not x["r"]]
        nao = [x for x in lista if x["r"]]
        itens.append(_item("pausas", "Pausas de descanso (Califórnia)", "problema" if sem else "atencao" if nao else "ok",
                           f"{len(sem)} dia(s) sem resposta e {len(nao)} com pausa não liberada (rest premium)." if lista
                           else "Pausas de 10 minutos em dia nos últimos 14 dias.",
                           [f"{x['u'].nome}, {x['dia']:%m/%d}" for x in lista][:12],
                           ("Conferir pausas", "/painel/ponto/problemas#descansos") if lista else None))
    elif e.estado == "CA":
        itens.append(_item("pausas", "Pausas de descanso (Califórnia)", "problema",
                           "A pergunta sobre as pausas de 10 minutos está desligada. Na Califórnia ela ajuda a provar que as pausas foram liberadas.",
                           acao=("Ligar", "/painel/ponto/problemas#descansos")))

    # 7. escala justa
    fw = escala_justa.regra(e)
    if fw:
        det, sit = [], "ok"
        for s in (seg, seg + timedelta(days=7), seg + timedelta(days=14)):
            st = escala_justa.situacao_semana(db, e, s)
            if st["atrasada"]:
                sit = "problema"
                det.append(f"Semana de {s:%m/%d}: prazo era {st['prazo']:%m/%d}" if st["publicada"]
                           else f"Semana de {s:%m/%d}: prazo era {st['prazo']:%m/%d} e ainda não foi publicada")
            elif not st["publicada"] and st["prazo"] - hoje <= timedelta(days=3):
                sit = "atencao" if sit == "ok" else sit
                det.append(f"Semana de {s:%m/%d}: publicar até {st['prazo']:%m/%d}")
        prem = escala_justa.premios_por_dia(db, e, ini, fim)
        if prem:
            sit = "atencao" if sit == "ok" else sit
            det.append(f"{len(prem)} mudança(s) com predictability pay no período aberto")
        itens.append(_item("escala_justa", f"Escala justa ({fw['nome']})", sit,
                           "Prazos de publicação e mudanças em cima da hora." if det else "Escalas publicadas no prazo.",
                           det, ("Abrir a escala", "/painel/escala") if det else None))

    # 8. ponto incompleto
    pend = problemas(db, e)
    itens.append(_item("ponto", "Batidas completas", "atencao" if pend else "ok",
                       f"{len(pend)} saída(s) esquecida(s) ou automática(s) para conferir." if pend
                       else "Nenhuma batida faltando nos últimos 14 dias.",
                       [f"{p['u'].nome}, {p['dia']:%m/%d}" for p in pend][:12],
                       ("Resolver", "/painel/ponto/problemas") if pend else None))

    # 9. cadastro da empresa
    det = []
    if e.lat is None:
        det.append("Local da empresa não definido: o ponto é aceito de qualquer lugar.")
    sem_consent = [f.nome for f in equipe if f.papel == "funcionario" and not f.consentimento_em and not f.convite_token]
    if sem_consent:
        det.append(f"{len(sem_consent)} funcionário(s) sem autorização de localização registrada.")
    itens.append(_item("cadastro", "Cadastro e consentimento", "atencao" if det else "ok",
                       "Pontos do cadastro para completar." if det else "Local definido e autorizações registradas.",
                       det, ("Configurações", "/painel/config") if det else None))

    validos = [i for i in itens if i["situacao"] != "info"]
    pontos = sum(1 if i["situacao"] == "ok" else 0.5 if i["situacao"] == "atencao" else 0 for i in validos)
    nota = round(100 * pontos / len(validos)) if validos else 100
    ordem = {"problema": 0, "atencao": 1, "ok": 2, "info": 3}
    itens.sort(key=lambda i: ordem[i["situacao"]])
    return {"itens": itens, "nota": nota,
            "problemas": sum(i["situacao"] == "problema" for i in itens),
            "atencao": sum(i["situacao"] == "atencao" for i in itens)}


@rota.get("/painel/conformidade")
def tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    return render(request, "painel_conformidade.html", u=u, e=e, aba="conformidade", c=verificar(db, e, u))
