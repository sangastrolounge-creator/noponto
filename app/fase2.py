"""Fase 2: ausências (atestados, férias), feriados, banco de horas, fechamento do período
e folha de ponto com aceite do funcionário."""
import csv
import io
import re
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import apuracao, paises, push, regras, setores
from . import db as banco
from .apuracao import MSG_FECHADO, TIPOS_AUSENCIA
from .core import (avisar, conferir_senha, da_empresa, data_iso, exigir_csrf, gestor_logado,
                   ip, ir, render, usuario_logado)
from .db import (PASTA_ANEXOS, Ajuste, AjusteBanco, Alerta, Ausencia, Fechamento,
                 FechamentoLinha, Feriado, Usuario, get_db)

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
# tipo de arquivo aceito -> (extensão, começo do arquivo)
ANEXOS = {"image/jpeg": ("jpg", b"\xff\xd8"), "image/png": ("png", b"\x89PNG"),
          "image/webp": ("webp", b"RIFF"), "application/pdf": ("pdf", b"%PDF")}
MIME = {ext: mime for mime, (ext, _) in ANEXOS.items()}


async def guardar_anexo(empresa_id: int, arquivo: UploadFile | None) -> str:
    """Salva a foto/PDF do atestado e devolve o nome do arquivo ('' se não veio nada)."""
    if not arquivo or not arquivo.filename:
        return ""
    dados = await arquivo.read(5_000_001)
    if not dados:
        return ""
    tipo = ANEXOS.get((arquivo.content_type or "").lower())
    if len(dados) > 5_000_000:
        raise ValueError("O arquivo passa de 5 MB. Tire uma foto menor.")
    if not tipo or not dados.startswith(tipo[1]):
        raise ValueError("Envie uma foto (JPG ou PNG) ou um PDF.")
    pasta = PASTA_ANEXOS / str(empresa_id)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"{secrets.token_hex(10)}.{tipo[0]}"
    (pasta / nome).write_bytes(dados)
    return nome


def datas_da_ausencia(inicio: str, fim: str) -> tuple[date, date] | None:
    ini = data_iso(inicio, None)
    ate = data_iso(fim, None) or ini
    if not ini or ate < ini or (ate - ini).days > 366:
        return None
    return ini, ate


def rotulo_ausencia(a: Ausencia) -> str:
    dias = f"{a.inicio:%d/%m/%Y}" if a.inicio == a.fim else f"{a.inicio:%d/%m} a {a.fim:%d/%m/%Y}"
    return f"{TIPOS_AUSENCIA.get(a.tipo, 'Ausência')} de {a.usuario.nome}, {dias}"


# ============================================================ ausências e feriados (gestor)
@rota.get("/painel/ausencias")
def ausencias(request: Request, ano: int = 0, u: Usuario = Depends(gestor_logado),
              db: Session = Depends(get_db)):
    e = u.empresa
    hoje = regras.hoje(e)
    ano = ano if 2020 <= ano <= 2100 else hoje.year
    lista = list(db.scalars(select(Ausencia).where(
        Ausencia.empresa_id == e.id, Ausencia.fim >= hoje - timedelta(days=120))
        .order_by(Ausencia.situacao != "pendente", Ausencia.inicio.desc()).limit(200)))
    feriados = list(db.scalars(select(Feriado).where(
        Feriado.empresa_id == e.id, Feriado.data.between(date(ano, 1, 1), date(ano, 12, 31)))
        .order_by(Feriado.data)))
    equipe = list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True)).order_by(Usuario.nome)))
    from . import ferias
    avisos_ferias = {a.id: ferias.conferir(db, a.usuario, a.inicio, a.fim, a.abono_dias or 0)
                     for a in lista if a.tipo == "ferias" and a.situacao == "pendente"}
    return render(request, "painel_ausencias.html", u=u, e=e, aba="ausencias", lista=lista,
                  feriados=feriados, equipe=equipe, hoje=hoje, ano=ano,
                  fechado=apuracao.fechado_ate(db, e), avisos_ferias=avisos_ferias)


@rota.post("/painel/ausencias", dependencies=CSRF)
async def ausencia_nova(request: Request, usuario_id: int = Form(...), tipo: str = Form(...),
                        inicio: str = Form(""), fim: str = Form(""), obs: str = Form(""),
                        anexo: UploadFile | None = File(None), volta: str = Form(""),
                        u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    destino = volta if volta.startswith("/painel/ponto/ajuste") else "/painel/ausencias"
    datas = datas_da_ausencia(inicio, fim)
    if tipo not in TIPOS_AUSENCIA or not datas:
        avisar(request, "Confira o tipo e as datas (o fim não pode ser antes do início).", "erro")
        return ir(destino)
    if apuracao.esta_fechado(db, u.empresa, datas[0]):
        avisar(request, MSG_FECHADO, "erro")
        return ir(destino)
    try:
        arquivo = await guardar_anexo(u.empresa_id, anexo)
    except ValueError as ex:
        avisar(request, str(ex), "erro")
        return ir(destino)
    a = Ausencia(empresa_id=u.empresa_id, usuario_id=f.id, tipo=tipo, inicio=datas[0],
                 fim=datas[1], obs=obs.strip()[:300], anexo=arquivo, situacao="aprovado",
                 criado_por=u.nome, decidido_por=u.nome, criado_em=banco.agora())
    db.add(a)
    db.flush()
    regras.registrar(db, u.empresa_id, u.nome, "ausência lançada", rotulo_ausencia(a) + ".",
                     usuario_id=f.id, dia=a.inicio)
    db.commit()
    avisar(request, "Ausência lançada. Nesses dias o ponto não é cobrado.")
    return ir(destino)


@rota.post("/painel/ausencias/{aid}", dependencies=CSRF)
def ausencia_decidir(request: Request, tarefas_bg: BackgroundTasks, aid: int,
                     acao: str = Form(...), u: Usuario = Depends(gestor_logado),
                     db: Session = Depends(get_db)):
    a = da_empresa(db, Ausencia, aid, u)
    if apuracao.esta_fechado(db, u.empresa, a.inicio) and a.situacao == "aprovado":
        avisar(request, MSG_FECHADO, "erro")
        return ir("/painel/ausencias")
    rotulo = rotulo_ausencia(a)
    if acao == "remover":
        if a.anexo:
            (PASTA_ANEXOS / str(a.empresa_id) / a.anexo).unlink(missing_ok=True)
        db.delete(a)
        regras.registrar(db, u.empresa_id, u.nome, "ausência removida", rotulo + ".")
        avisar(request, "Ausência removida.")
    elif acao in ("aprovar", "recusar") and a.situacao == "pendente":
        if acao == "aprovar" and apuracao.esta_fechado(db, u.empresa, a.inicio):
            avisar(request, MSG_FECHADO, "erro")
            return ir("/painel/ausencias")
        a.situacao = "aprovado" if acao == "aprovar" else "recusado"
        a.decidido_por = u.nome
        regras.registrar(db, u.empresa_id, u.nome, f"ausência {a.situacao[:-1]}a", rotulo + ".")
        tarefas_bg.add_task(push.avisar_usuarios, [(
            a.usuario_id, ("Férias aprovadas" if acao == "aprovar" else "Férias recusadas")
            if a.tipo == "ferias" else "Atestado " + ("aceito" if acao == "aprovar" else "recusado"),
            f"O gestor {'aceitou' if acao == 'aprovar' else 'recusou'} o que você enviou.")])
        avisar(request, f"Pedido de {a.usuario.primeiro_nome} {a.situacao}.")
    db.commit()
    return ir("/painel/ausencias")


@rota.get("/anexo/{aid}")
def anexo(aid: int, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    a = db.get(Ausencia, aid)
    if (not a or a.empresa_id != u.empresa_id or not a.anexo
            or not (u.gestor or a.usuario_id == u.id)):
        return Response("Arquivo não encontrado.", 404)
    arquivo = PASTA_ANEXOS / str(a.empresa_id) / a.anexo
    if not arquivo.is_file():
        return Response("Arquivo não encontrado.", 404)
    return FileResponse(arquivo, media_type=MIME.get(a.anexo.rsplit(".", 1)[-1], "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=3600",
                                 "Content-Disposition": "inline"})


@rota.post("/painel/feriados", dependencies=CSRF)
def feriado_novo(request: Request, data: str = Form(""), nome: str = Form(""),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    d, nome = data_iso(data, None), nome.strip()[:80]
    if not d or not nome:
        avisar(request, "Informe a data e o nome do feriado.", "erro")
    elif apuracao.esta_fechado(db, u.empresa, d):
        avisar(request, MSG_FECHADO, "erro")
    elif db.scalar(select(Feriado.id).where(Feriado.empresa_id == u.empresa_id, Feriado.data == d)):
        avisar(request, "Já existe um feriado nessa data.", "erro")
    else:
        db.add(Feriado(empresa_id=u.empresa_id, data=d, nome=nome))
        db.commit()
        return ir(f"/painel/ausencias?ano={d.year}#feriados")
    return ir("/painel/ausencias#feriados")


@rota.post("/painel/feriados/nacionais", dependencies=CSRF)
def feriados_nacionais(request: Request, ano: int = Form(...),
                       u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    if not 2020 <= ano <= 2100:
        return ir("/painel/ausencias")
    ja = set(db.scalars(select(Feriado.data).where(Feriado.empresa_id == u.empresa_id)))
    n = 0
    for d, nome in paises.regras_de(u.empresa).feriados(ano):
        if d not in ja and not apuracao.esta_fechado(db, u.empresa, d):
            db.add(Feriado(empresa_id=u.empresa_id, data=d, nome=nome))
            n += 1
    db.commit()
    avisar(request, f"{n} feriado(s) nacional(is) de {ano} adicionado(s). "
                    "Acrescente os estaduais e municipais da sua cidade.")
    return ir(f"/painel/ausencias?ano={ano}#feriados")


@rota.post("/painel/feriados/{fid}/remover", dependencies=CSRF)
def feriado_remover(request: Request, fid: int, u: Usuario = Depends(gestor_logado),
                    db: Session = Depends(get_db)):
    f = da_empresa(db, Feriado, fid, u)
    if apuracao.esta_fechado(db, u.empresa, f.data):
        avisar(request, MSG_FECHADO, "erro")
    else:
        db.delete(f)
        db.commit()
    return ir(f"/painel/ausencias?ano={f.data.year}#feriados")


# ============================================================ fechamento e banco de horas
def periodo_sugerido(db: Session, e) -> tuple[date, date]:
    """Do início do período aberto até o fim do período do país (mês no Brasil, período de
    pagamento nos EUA), ou até hoje se ele ainda não acabou."""
    return paises.regras_de(e).periodo_sugerido(db, e)


def _do_setor(db: Session, e, resultado: list[dict], setor: str) -> tuple[list[dict], int, str]:
    """Só as pessoas do setor escolhido (o fechamento em si continua valendo para todos)."""
    sid = setores.ler_filtro(db, e.id, setor)
    nome = ("Sem setor" if sid == setores.SEM_SETOR else setores.nomes(db, e.id).get(sid, "")) if sid else ""
    return [r for r in resultado if setores.no_setor(r["u"], sid)], sid, nome


@rota.get("/painel/fechamento")
def fechamento(request: Request, de: str = "", ate: str = "", setor: str = "",
               u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    hoje = regras.hoje(e)
    sug = periodo_sugerido(db, e)
    ini, fim = data_iso(de, sug[0]), data_iso(ate, sug[1])
    if ini > fim or (fim - ini).days > 100:
        ini, fim = sug
    resultado, sid, _nome = _do_setor(db, e, paises.regras_de(e).apurar(db, e, ini, fim), setor)
    fechados = list(db.scalars(select(Fechamento).where(Fechamento.empresa_id == e.id)
                               .order_by(Fechamento.fim.desc()).limit(24)))
    limite = apuracao.fechado_ate(db, e)
    pendentes = (db.scalar(select(func.count()).select_from(Ajuste).where(
        Ajuste.empresa_id == e.id, Ajuste.situacao == "pendente",
        Ajuste.data.between(ini, fim))) or 0) + (db.scalar(
            select(func.count()).select_from(Ausencia).where(
                Ausencia.empresa_id == e.id, Ausencia.situacao == "pendente",
                Ausencia.inicio <= fim, Ausencia.fim >= ini)) or 0)
    motivo = None
    if fim >= hoje:
        motivo = "O período só pode ser fechado depois que o último dia terminar."
    elif limite and ini <= limite:
        motivo = f"O período começa antes de {limite:%d/%m/%Y}, que já está fechado."
    equipe = list(db.scalars(select(Usuario).where(
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True)).order_by(Usuario.nome)))
    return render(request, "painel_fechamento.html", u=u, e=e, aba="fechamento", ini=ini,
                  fim=fim, resultado=resultado, fechados=fechados, motivo=motivo, hoje=hoje,
                  pendentes=pendentes, equipe=equipe, setores=setores.setores_da(db, e.id), setor=sid,
                  incompletos=sum(r["incompletos"] for r in resultado),
                  sem_escala=sum(r.get("sem_escala", 0) for r in resultado))


@rota.post("/painel/fechamento", dependencies=CSRF)
def fechamento_criar(request: Request, tarefas_bg: BackgroundTasks, de: str = Form(""),
                     ate: str = Form(""), u: Usuario = Depends(gestor_logado),
                     db: Session = Depends(get_db)):
    e = u.empresa
    ini, fim = data_iso(de, None), data_iso(ate, None)
    limite = apuracao.fechado_ate(db, e)
    if not ini or not fim or ini > fim or (fim - ini).days > 100:
        avisar(request, "Confira as datas do período.", "erro")
    elif fim >= regras.hoje(e):
        avisar(request, "O período só pode ser fechado depois que o último dia terminar.", "erro")
    elif limite and ini <= limite:
        avisar(request, f"O período começa antes de {limite:%d/%m/%Y}, que já está fechado.", "erro")
    else:
        f = paises.regras_de(e).fechar(db, e, ini, fim, u.nome)
        regras.registrar(db, e.id, u.nome, "período fechado",
                         f"Ponto de {ini:%d/%m/%Y} a {fim:%d/%m/%Y} fechado, "
                         f"{len(f.linhas)} funcionário(s).")
        db.commit()
        tarefas_bg.add_task(push.avisar_usuarios, [
            (l.usuario_id, "Folha de ponto para conferir",
             f"Sua folha de {ini:%d/%m} a {fim:%d/%m} está pronta. Confira e assine.")
            for l in f.linhas])
        avisar(request, "Período fechado. Cada funcionário já pode conferir e assinar a folha no app.")
        return ir("/painel/fechamento")
    return ir(f"/painel/fechamento?de={de}&ate={ate}")


@rota.post("/painel/fechamento/{fid}/reabrir", dependencies=CSRF)
def fechamento_reabrir(request: Request, fid: int, u: Usuario = Depends(gestor_logado),
                       db: Session = Depends(get_db)):
    f = da_empresa(db, Fechamento, fid, u)
    if f.fim != apuracao.fechado_ate(db, u.empresa):
        avisar(request, "Só dá para reabrir o período fechado mais recente.", "erro")
        return ir("/painel/fechamento")
    from . import pagamento
    if pagamento.periodo_tem_folha_finalizada(db, f.id):
        avisar(request, "Este período já tem folha de pagamento finalizada. Desfaça a "
                        "finalização da folha (em Pagamento) antes de reabrir o ponto.", "erro")
        return ir("/painel/fechamento")
    pagamento.apagar_folhas_do_periodo(db, f.id)
    paises.regras_de(u.empresa).ao_reabrir(db, f)
    assinadas = sum(1 for l in f.linhas if l.aceite_em)
    regras.registrar(db, u.empresa_id, u.nome, "período reaberto",
                     f"Ponto de {f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y} reaberto. "
                     f"{assinadas} assinatura(s) descartada(s).")
    ini, fim = f.inicio, f.fim
    db.delete(f)
    db.commit()
    avisar(request, "Período reaberto. As assinaturas dele foram descartadas; feche de novo depois de corrigir.")
    return ir(f"/painel/fechamento?de={ini}&ate={fim}")


@rota.get("/painel/fechamento/{fid}/planilha.csv")
def fechamento_csv(fid: int, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Fechamento, fid, u)
    if u.empresa.pais == "US":   # horas para o provedor de folha americano
        from .paises.us import telas as telas_us
        return telas_us.planilha_fechamento(u, f)
    banco_h = u.empresa.controle == "banco"
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Funcionário", "Função", "Período", "Horas previstas", "Horas trabalhadas",
                f"Extras em dia normal ({f.pct_normal}%)",
                f"Extras em folga/feriado ({f.pct_especial}%)",
                "Extras com acréscimo (em horas normais)",
                f"Horas noturnas (adicional de {f.pct_noturno}%)", "Atrasos e saídas antecipadas",
                "Faltas (dias)", "Faltas (horas)", "Dias de atestado/afastamento",
                "Ajustes manuais"] + (["Saldo anterior", "Saldo final do banco"] if banco_h else [])
               + ["Folha assinada em", "DSR descontado (dias)", "DSR descontado (horas)",
                  "DSR sobre horas extras (em horas normais)",
                  "Intervalo não concedido (art. 71, §4º, indenizatório)"])
    h = regras.fmt_min
    for l in sorted(f.linhas, key=lambda l: l.usuario.nome.lower()):
        w.writerow([l.usuario.nome, l.usuario.cargo, f"{f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y}",
                    h(l.previsto), h(l.trabalhado), h(l.extra_normal), h(l.extra_especial),
                    h(apuracao.com_acrescimo(l.extra_normal, l.extra_especial,
                                             f.pct_normal, f.pct_especial)),
                    h(l.noturno), h(l.debito), l.faltas, h(l.falta_min), l.dias_ausencia,
                    apuracao.fmt_saldo(l.ajustes).replace("−", "-")]
                   + ([apuracao.fmt_saldo(l.saldo_anterior).replace("−", "-"),
                       apuracao.fmt_saldo(l.saldo_final).replace("−", "-")] if banco_h else [])
                   + [regras.para_local(u.empresa, l.aceite_em).strftime("%d/%m/%Y %H:%M")
                      if l.aceite_em else "não assinada",
                      l.dsr_perdidos, h(l.dsr_perdido_min), h(l.dsr_extras),
                      h(l.intervalo_suprimido)])
    return Response("﻿" + saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="fechamento-{f.inicio}-a-{f.fim}.csv"'})


@rota.get("/painel/fechamento/resumo")
def fechamento_resumo(request: Request, de: str = "", ate: str = "", setor: str = "",
                      u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    """Resumo da folha de toda a equipe numa página só, para imprimir ou salvar em PDF."""
    e = u.empresa
    sug = periodo_sugerido(db, e)
    ini, fim = data_iso(de, sug[0]), data_iso(ate, sug[1])
    if ini > fim or (fim - ini).days > 100:
        ini, fim = sug
    resultado, sid, nome_setor = _do_setor(db, e, paises.regras_de(e).apurar(db, e, ini, fim), setor)
    fechado = db.scalar(select(Fechamento).where(
        Fechamento.empresa_id == e.id, Fechamento.inicio == ini, Fechamento.fim == fim))
    chaves = paises.regras_de(e).CHAVES_RESUMO
    total = {k: sum(r.get(k, 0) for r in resultado) for k in chaves}
    return render(request, "resumo.html", u=u, e=e, ini=ini, fim=fim, resultado=resultado,
                  total=total, fechado=fechado, emitido=regras.agora_local(e),
                  pn=fechado.pct_normal if fechado else e.pct_extra_normal,
                  pe=fechado.pct_especial if fechado else e.pct_extra_especial,
                  pnot=fechado.pct_noturno if fechado else e.pct_noturno,
                  volta=f"/painel/fechamento?de={ini}&ate={fim}" + (f"&setor={sid}" if sid else ""),
                  nome_setor=nome_setor)


@rota.get("/painel/fechamento/horas")
def fechamento_horas(request: Request, de: str = "", ate: str = "", setor: str = "",
                     u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    """Tabela curta para imprimir: extras, extras em folga/feriado, noturnas e faltas em horas."""
    e = u.empresa
    if e.pais != "BR":   # nos EUA o resumo já traz as horas no formato de lá
        return ir(f"/painel/fechamento/resumo?de={de}&ate={ate}")
    sug = periodo_sugerido(db, e)
    ini, fim = data_iso(de, sug[0]), data_iso(ate, sug[1])
    if ini > fim or (fim - ini).days > 100:
        ini, fim = sug
    resultado, sid, nome_setor = _do_setor(db, e, paises.regras_de(e).apurar(db, e, ini, fim), setor)
    fechado = db.scalar(select(Fechamento).where(
        Fechamento.empresa_id == e.id, Fechamento.inicio == ini, Fechamento.fim == fim))
    total = {k: sum(r[k] for r in resultado) for k in ("extra_normal", "extra_especial", "noturno", "falta_min")}
    return render(request, "fechamento_horas.html", u=u, e=e, ini=ini, fim=fim, resultado=resultado,
                  total=total, fechado=fechado, emitido=regras.agora_local(e),
                  pn=fechado.pct_normal if fechado else e.pct_extra_normal,
                  pe=fechado.pct_especial if fechado else e.pct_extra_especial,
                  pnot=fechado.pct_noturno if fechado else e.pct_noturno,
                  volta=f"/painel/fechamento?de={ini}&ate={fim}" + (f"&setor={sid}" if sid else ""),
                  nome_setor=nome_setor)


def linha_do_periodo(db: Session, usuario_id: int, ini: date, fim: date) -> FechamentoLinha | None:
    return db.scalar(select(FechamentoLinha).join(Fechamento).where(
        FechamentoLinha.usuario_id == usuario_id, Fechamento.inicio == ini, Fechamento.fim == fim))


@rota.get("/painel/folha")
def folha(request: Request, fid: int = Query(0, alias="func"), de: str = "", ate: str = "",
          u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    f = da_empresa(db, Usuario, fid, u)
    sug = periodo_sugerido(db, e)
    ini, fim = data_iso(de, sug[0]), data_iso(ate, sug[1])
    if ini > fim or (fim - ini).days > 100:
        ini, fim = sug
    r = paises.regras_de(e).apurar(db, e, ini, fim, f.id)
    if not r:
        avisar(request, f"{f.primeiro_nome} não tem escala nem batidas nesse período.", "erro")
        return ir(f"/painel/fechamento?de={ini}&ate={fim}")
    return render(request, "folha.html", u=u, e=e, r=r[0], ini=ini, fim=fim,
                  linha=linha_do_periodo(db, f.id, ini, fim), modo="gestor",
                  volta=f"/painel/fechamento?de={ini}&ate={fim}")


HORAS_RE = re.compile(r"^(\d{1,3}):([0-5]\d)$")


@rota.post("/painel/banco", dependencies=CSRF)
def banco_ajuste(request: Request, usuario_id: int = Form(...), data: str = Form(""),
                 sentido: str = Form("credito"), horas: str = Form(""), motivo: str = Form(""),
                 volta: str = Form("/painel/fechamento"),
                 u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, usuario_id, u)
    volta = volta if volta.startswith("/painel/fechamento") else "/painel/fechamento"
    d, m, motivo = data_iso(data, None), HORAS_RE.match(horas.strip()), motivo.strip()[:200]
    minutos = int(m.group(1)) * 60 + int(m.group(2)) if m else 0
    if not d or not minutos or len(motivo) < 3 or sentido not in ("credito", "debito"):
        avisar(request, "Informe a data, as horas no formato 2:30 e o motivo.", "erro")
    elif d > regras.hoje(u.empresa):
        avisar(request, "A data do ajuste não pode ser no futuro.", "erro")
    elif apuracao.esta_fechado(db, u.empresa, d):
        avisar(request, MSG_FECHADO, "erro")
    else:
        minutos = minutos if sentido == "credito" else -minutos
        db.add(AjusteBanco(empresa_id=u.empresa_id, usuario_id=f.id, data=d, minutos=minutos,
                           motivo=motivo, autor=u.nome, criado_em=banco.agora()))
        regras.registrar(db, u.empresa_id, u.nome, "ajuste no banco de horas",
                         f"{apuracao.fmt_saldo(minutos)} para {f.nome} em {d:%d/%m/%Y}. Motivo: {motivo}")
        db.commit()
        avisar(request, f"Ajuste de {apuracao.fmt_saldo(minutos)} lançado para {f.primeiro_nome}.")
    return ir(volta)


# ============================================================ funcionário: folha e atestado
@rota.get("/app/folha")
def app_folha(request: Request, u: Usuario = Depends(usuario_logado),
              db: Session = Depends(get_db)):
    e = u.empresa
    ini, fim = paises.regras_de(e).periodo_aberto(db, e)
    r = paises.regras_de(e).apurar(db, e, ini, fim, u.id)
    linhas = list(db.scalars(select(FechamentoLinha).join(Fechamento).where(
        FechamentoLinha.usuario_id == u.id).order_by(Fechamento.fim.desc()).limit(24)))
    from . import pagamento
    return render(request, "app_folha.html", u=u, e=e, aba="folha", ini=ini, fim=fim,
                  r=r[0] if r else None, linhas=linhas,
                  holerites=pagamento.holerites_do_funcionario(db, u))


def _minha_linha(db: Session, lid: int, u: Usuario) -> FechamentoLinha:
    linha = db.get(FechamentoLinha, lid)
    if not linha or linha.usuario_id != u.id:
        from .core import Redirecionar
        raise Redirecionar("/app/folha")
    return linha


@rota.get("/app/folha/{lid}")
def app_folha_ver(request: Request, lid: int, u: Usuario = Depends(usuario_logado),
                  db: Session = Depends(get_db)):
    linha = _minha_linha(db, lid, u)
    f = linha.fechamento
    r = paises.regras_de(u.empresa).apurar(db, u.empresa, f.inicio, f.fim, u.id)
    if not r:
        return ir("/app/folha")
    return render(request, "folha.html", u=u, e=u.empresa, r=r[0], ini=f.inicio, fim=f.fim,
                  linha=linha, modo="app", volta="/app/folha")


@rota.post("/app/folha/{lid}", dependencies=CSRF)
def app_folha_assinar(request: Request, lid: int, acao: str = Form(...),
                      senha: str = Form(""), texto: str = Form(""),
                      u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    linha = _minha_linha(db, lid, u)
    f = linha.fechamento
    if linha.aceite_em:
        return ir(f"/app/folha/{lid}")
    if acao == "aceitar":
        if not conferir_senha(senha, u.senha_hash):
            avisar(request, "Senha incorreta. A assinatura não foi registrada.", "erro")
            return ir(f"/app/folha/{lid}")
        linha.aceite_em, linha.aceite_ip, linha.contestacao = banco.agora(), ip(request)[:60], ""
        regras.registrar(db, u.empresa_id, u.nome, "folha assinada",
                         f"Assinou a folha de {f.inicio:%d/%m/%Y} a {f.fim:%d/%m/%Y}.")
        avisar(request, "Folha assinada. Obrigado!")
    elif acao == "contestar" and len(texto.strip()) >= 5:
        linha.contestacao = texto.strip()[:500]
        db.add(Alerta(empresa_id=u.empresa_id, usuario_id=u.id, tipo="folha",
                      criado_em=banco.agora(),
                      texto=f"{u.nome} não concordou com a folha de {f.inicio:%d/%m} a "
                            f"{f.fim:%d/%m}: {linha.contestacao[:150]}"))
        avisar(request, "Enviamos sua observação ao gestor.")
    else:
        avisar(request, "Escreva o que está errado na folha.", "erro")
    db.commit()
    return ir(f"/app/folha/{lid}")


@rota.post("/app/ausencias", dependencies=CSRF)
async def app_ausencia(request: Request, tarefas_bg: BackgroundTasks, tipo: str = Form("atestado"),
                       inicio: str = Form(""), fim: str = Form(""), obs: str = Form(""),
                       anexo: UploadFile | None = File(None),
                       u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    e = u.empresa
    datas = datas_da_ausencia(inicio, fim)
    erro = None
    if tipo not in paises.regras_de(e).TIPOS_PEDIDO_AUSENCIA or not datas:
        erro = "Confira as datas (o último dia não pode ser antes do primeiro)."
    elif apuracao.esta_fechado(db, e, datas[0]):
        erro = "Esses dias já estão num período de ponto fechado. Fale com o gestor."
    elif (db.scalar(select(func.count()).select_from(Ausencia).where(
            Ausencia.usuario_id == u.id, Ausencia.situacao == "pendente")) or 0) >= 5:
        erro = "Você já tem 5 envios aguardando resposta."
    arquivo = ""
    if not erro:
        try:
            arquivo = await guardar_anexo(e.id, anexo)
        except ValueError as ex:
            erro = str(ex)
    if not erro and tipo == "atestado" and not arquivo and not (e.pais == "US" and e.sick_por_horas):
        erro = "Anexe a foto do atestado."
    if erro:
        avisar(request, erro, "erro")
        return ir("/app/ajustes")
    a = Ausencia(empresa_id=e.id, usuario_id=u.id, tipo=tipo, inicio=datas[0], fim=datas[1],
                 obs=obs.strip()[:300], anexo=arquivo, situacao="pendente", criado_por=u.nome,
                 criado_em=banco.agora())
    alerta = Alerta(empresa_id=e.id, usuario_id=u.id, tipo="ausencia", criado_em=banco.agora(),
                    texto=f"{u.nome} enviou {TIPOS_AUSENCIA[tipo].lower()} de "
                          f"{datas[0]:%d/%m} a {datas[1]:%d/%m} para você conferir.")
    db.add_all([a, alerta])
    db.commit()
    tarefas_bg.add_task(push.avisar_alertas, [alerta.id])
    avisar(request, "Enviado ao gestor. Você será avisado da resposta.")
    return ir("/app/ajustes")
