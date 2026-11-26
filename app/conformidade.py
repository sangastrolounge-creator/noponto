"""Central de conformidade: junta numa tela só as conferências trabalhistas (CLT) que o sistema
já faz, com o que fazer em cada uma e uma nota geral.

- salário abaixo do mínimo proporcional à jornada (ou do piso informado);
- quem está sem salário cadastrado;
- menores de idade (CLT art. 403, 404, 411, 413 e 432);
- hora extra acima de 2 horas no dia (art. 59) no período aberto;
- escala acima de 44 horas na semana (art. 58);
- intervalo para refeição não batido ou não concedido (art. 71 e §4º);
- férias vencidas ou perto de vencer (arts. 134 e 137);
- descanso de 11 horas entre jornadas (art. 66);
- folga semanal: mais de 6 dias seguidos sem folga na escala (art. 67 e Lei 605/49);
- batidas incompletas;
- cadastro do local e autorização de localização (LGPD).

Cada item: {"chave", "titulo", "situacao": ok | atencao | problema | info, "resumo", "detalhes": [...],
"acao": (rótulo, link) ou None}. A nota é a parte dos itens aplicáveis que estão ok
(atenção conta meio ponto). É uma conferência de apoio, não substitui o contador."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import apuracao, paises, regras
from .core import gestor_logado, pode, render
from .db import Turno, Usuario, get_db

rota = APIRouter()
LIMITE_EXTRA_DIA = 120


def _item(chave, titulo, situacao, resumo, detalhes=None, acao=None):
    return {"chave": chave, "titulo": titulo, "situacao": situacao, "resumo": resumo,
            "detalhes": detalhes or [], "acao": acao}


def _equipe(db, e):
    return list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                 Usuario.na_escala.is_(True)).order_by(Usuario.nome)))


def verificar(db: Session, e, u) -> dict:
    from . import escala_justa, menores, salario_minimo
    from .ponto_problemas import problemas
    din = salario_minimo.dinheiro
    hoje = regras.hoje(e)
    equipe = _equipe(db, e)
    empregados = [f for f in equipe if (f.classificacao or "clt") != "pj"]
    itens = []
    ver_pag = pode(u, "pagamento") or pode(u, "equipe")

    # 1. salário mínimo / piso
    if ver_pag:
        sm = salario_minimo.situacao(db, e, hoje)
        if sm["abaixo"]:
            itens.append(_item("minimo", "Salário mínimo", "problema",
                               f"{len(sm['abaixo'])} salário(s) abaixo do mínimo proporcional à jornada ({sm['normal']['fonte']}: {din(sm['normal']['centavos'])} por 220 h).",
                               [f"{x['u'].nome}: {din(x['valor'])} (mínimo {din(x['minimo'])})" for x in sm["abaixo"]],
                               ("Corrigir na ficha", "/painel/equipe")))
        elif sm["abaixo_depois"]:
            p = sm["proxima"]
            itens.append(_item("minimo", "Salário mínimo", "atencao",
                               f"Em {p['data']:%d/%m/%Y} o mínimo sobe para {din(p['normal']['centavos'])}: {len(sm['abaixo_depois'])} pessoa(s) ficam abaixo.",
                               [f"{x['u'].nome}: {din(x['valor'])}" for x in sm["abaixo_depois"]],
                               ("Ajustar salários", "/painel/equipe")))
        else:
            itens.append(_item("minimo", "Salário mínimo", "ok",
                               f"Todos os salários cadastrados estão acima do mínimo ({sm['normal']['fonte']}: {din(sm['normal']['centavos'])} por 220 h)."))
        sem_valor = [f.nome for f in empregados if not f.salario_centavos]
        if sem_valor:
            itens.append(_item("sem_valor", "Salário cadastrado", "atencao",
                               f"{len(sem_valor)} pessoa(s) sem salário: o sistema não confere o mínimo nem calcula o custo e a folha.",
                               sem_valor[:10], ("Completar as fichas", "/painel/equipe")))

    # 2. menores de idade (esta semana e a próxima)
    seg = hoje - timedelta(days=hoje.weekday())
    dias = [seg + timedelta(days=i) for i in range(14)]
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(dias[0] - timedelta(days=7), dias[-1])))}
    nomes = {f.id: f.nome for f in equipe}
    if any(f.nascimento for f in equipe):
        det = []
        for bloco in (dias[:7], dias[7:]):
            m = menores.conferir_grade(db, e, equipe, turnos, bloco)
            det += [f"{nomes[uid]}, {d:%d/%m}: " + "; ".join(v) for (uid, d), v in sorted(m["dia"].items(), key=lambda x: x[0][1])]
            det += [f"{nomes[uid]}: {v}" for uid, v in m["semana"].items()]
        det = list(dict.fromkeys(det))
        itens.append(_item("menores", "Trabalho de menores", "problema" if det else "ok",
                           f"{len(det)} ponto(s) da escala passam dos limites da CLT para menores (esta semana e a próxima)."
                           if det else "A escala desta semana e da próxima respeita os limites da CLT para menores.",
                           det[:12], ("Abrir a escala", "/painel/escala") if det else None))

    # 3. período aberto: hora extra acima de 2 h no dia e intervalo não batido
    ini, fim = paises.regras_de(e).periodo_aberto(db, e)
    resultado = paises.regras_de(e).apurar(db, e, ini, fim)
    acima = []
    for r in resultado:
        if (r["u"].classificacao or "clt") == "pj":
            continue
        for d in r["dias"]:
            if d["extra"] > LIMITE_EXTRA_DIA:
                acima.append(f"{r['u'].nome}, {d['dia']:%d/%m}: {regras.fmt_min(d['extra'])} de extra")
    itens.append(_item("extra", "Hora extra no dia", "atencao" if acima else "ok",
                       f"{len(acima)} dia(s) com mais de 2 horas extras no período aberto ({ini:%d/%m}–{fim:%d/%m}). A CLT permite até 2 por dia, salvo força maior."
                       if acima else f"Ninguém passou de 2 horas extras no dia no período aberto ({ini:%d/%m}–{fim:%d/%m}).",
                       acima[:12], ("Ver o ponto", "/painel/ponto") if acima else None))
    sem_int = [(r["u"].nome, r["sem_intervalo"]) for r in resultado if r["sem_intervalo"]] if e.intervalo_obrigatorio else []
    suprimido = [(r["u"].nome, r["intervalo_suprimido"]) for r in resultado if r["intervalo_suprimido"]]
    if sem_int or suprimido:
        resumo = []
        if suprimido:
            resumo.append(f"{len(suprimido)} pessoa(s) ficaram sem parte do intervalo para refeição no período aberto: "
                          "o tempo que faltou é pago com 50% a mais (CLT art. 71, §4º).")
        if sem_int:
            resumo.append(f"{len(sem_int)} pessoa(s) trabalharam mais de 6 horas sem bater o intervalo.")
        itens.append(_item("refeicao", "Intervalo para refeição", "atencao", " ".join(resumo),
                           ([f"{n}: {regras.fmt_min(m)} de intervalo não concedido" for n, m in suprimido]
                            + [f"{n}: {q} dia(s) sem bater o intervalo" for n, q in sem_int])[:12],
                           ("Ver o fechamento", "/painel/fechamento")))
    else:
        itens.append(_item("refeicao", "Intervalo para refeição", "ok",
                           f"Intervalos concedidos no período aberto ({ini:%d/%m}–{fim:%d/%m})."))

    # 4. escala: 44 h na semana, 11 h entre jornadas, folga semanal
    semana44, seguidos = [], []
    for f in empregados:
        for bloco in (dias[:7], dias[7:]):
            total = sum(regras.minutos_turno(turnos[(f.id, d)]) for d in bloco if (f.id, d) in turnos)
            if total > apuracao.SEMANA_CLT_MIN:
                semana44.append(f"{f.nome}, semana de {bloco[0]:%d/%m}: {regras.fmt_min(total)}")
        corrida = 0
        for d in [dias[0] - timedelta(days=7 - i) for i in range(7)] + dias:
            t = turnos.get((f.id, d))
            corrida = corrida + 1 if t and not t.folga else 0
            if corrida == 7 and d >= dias[0]:
                seguidos.append(f"{f.nome}: 7º dia seguido em {d:%d/%m}")
    curtos = escala_justa.descansos_curtos_na_grade(e, turnos, empregados, dias)
    det = semana44 + [f"{nomes[uid]}, {d:%d/%m}: menos de 11 h depois do turno anterior"
                      for uid, d in sorted(curtos, key=lambda x: x[1])] + seguidos
    itens.append(_item("escala", "Jornada na escala", "problema" if det else "ok",
                       "A escala desta semana ou da próxima passa de 44 horas, tem menos de 11 horas entre jornadas ou mais de 6 dias seguidos sem folga."
                       if det else "Escala dentro de 44 horas por semana, com 11 horas entre jornadas e folga semanal.",
                       det[:12], ("Abrir a escala", "/painel/escala") if det else None))

    # 5. férias (CLT arts. 130, 134, 137)
    if pode(u, "ausencias"):
        from . import ferias
        itens.append(ferias.item_conformidade(db, e))

    # 6. ponto incompleto
    pend = problemas(db, e)
    itens.append(_item("ponto", "Batidas completas", "atencao" if pend else "ok",
                       f"{len(pend)} saída(s) esquecida(s) ou automática(s) para conferir." if pend
                       else "Nenhuma batida faltando nos últimos 14 dias.",
                       [f"{p['u'].nome}, {p['dia']:%d/%m}" for p in pend][:12],
                       ("Resolver", "/painel/ponto/problemas") if pend else None))

    # 7. cadastro e LGPD
    det = []
    if e.lat is None:
        det.append("Local da empresa não definido: o ponto é aceito de qualquer lugar.")
    sem_consent = [f.nome for f in equipe if f.papel == "funcionario" and not f.consentimento_em and not f.convite_token]
    if sem_consent:
        det.append(f"{len(sem_consent)} funcionário(s) sem autorização de localização registrada (LGPD).")
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
