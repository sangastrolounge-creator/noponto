"""Avisos para o dono da plataforma (Master): nova empresa, pagamento confirmado ou recusado,
assinatura cancelada e o resumo diário dos testes grátis terminando. Chegam por e-mail (para o
e-mail de cada conta Master) e no celular (se ele ativou os avisos no painel). Em português:
são só para o dono. Cada tipo liga e desliga em Master -> Avisos."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from . import email_envio, push
from .core import (PUBLIC_URL, admin_logado, avisar, dinheiro, exigir_csrf, ir, log,
                   plataforma, render, situacao)
from .db import Config, Empresa, SessionLocal, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
TIPOS = {
    "cadastro": "Nova empresa cadastrada",
    "pago": "Pagamento confirmado",
    "recusado": "Cobrança vencida",
    "cancelado": "Assinatura cancelada",
    "teste_fim": "Resumo diário: testes grátis terminando sem assinatura",
}
CFG = "avisos_master"            # tipos desligados, separados por vírgula
CFG_DIA = "avisos_master_resumo"  # último dia em que o resumo foi enviado
HORA_RESUMO_UTC = 12              # 09:00 em Brasília


def desligados(db: Session) -> set[str]:
    c = db.get(Config, CFG)
    return {t for t in (c.valor if c else "").split(",") if t in TIPOS}


def _contas_master(db: Session) -> list[Usuario]:
    return list(db.scalars(select(Usuario).where(Usuario.is_admin.is_(True), Usuario.ativo.is_(True))))


def notificar(tipo: str, assunto: str, texto: str) -> int:
    """Em segundo plano. Devolve quantas contas Master foram avisadas."""
    with SessionLocal() as db:
        if tipo in desligados(db):
            return 0
        contas = _contas_master(db)
        nome = plataforma()["nome"]
        corpo = (f"{texto}\n\n---\n{nome} · {PUBLIC_URL or ''}/master\n"
                 "Para deixar de receber este tipo de aviso: Master → Avisos.")
        for u in contas:
            if u.email:
                email_envio.enviar(u.email, f"[{nome}] {assunto}", corpo)
            try:
                if push.DISPONIVEL:
                    push.enviar_usuario(db, u.id, assunto, texto.split("\n")[0][:180], "/master",
                                        traduzir=False)
            except Exception:
                log.exception("Falha no aviso push para o Master")
        return len(contas)


def _empresa(db: Session, empresa_id: int) -> tuple[Empresa | None, Usuario | None]:
    e = db.get(Empresa, empresa_id)
    dono = db.scalar(select(Usuario).where(Usuario.empresa_id == empresa_id, Usuario.dono.is_(True))
                     .order_by(Usuario.id).limit(1)) if e else None
    return e, dono


# ------------------------------------------------------------------ eventos
def nova_empresa(empresa_id: int) -> None:
    with SessionLocal() as db:
        e, dono = _empresa(db, empresa_id)
        if not e or e.isenta:
            return
        teste = f"{e.teste_ate:%d/%m/%Y}" if e.teste_ate else "—"
        texto = (f"{e.nome} acabou de se cadastrar.\n\n"
                 f"Responsável: {dono.nome if dono else '—'} ({dono.email if dono else '—'})\n"
                 f"Plano escolhido: {e.plano.nome if e.plano else 'nenhum'}\n"
                 f"Teste grátis até: {teste}")
        nome = e.nome
    notificar("cadastro", f"Nova empresa: {nome}", texto)


def pagamento(empresa_id: int, valor_centavos: int, ok: bool) -> None:
    with SessionLocal() as db:
        e, dono = _empresa(db, empresa_id)
        if not e:
            return
        plano = e.plano.nome if e.plano else "—"
        contato = f"{dono.nome} ({dono.email})" if dono else "—"
        val = dinheiro(valor_centavos)
    if ok:
        notificar("pago", f"Pagamento confirmado: {e.nome} ({val})",
                  f"{e.nome} pagou {val} pelo plano {plano}.\nResponsável: {contato}")
    else:
        notificar("recusado", f"Cobrança vencida: {e.nome} ({val})",
                  f"A cobrança de {val} de {e.nome} (plano {plano}) venceu sem pagamento.\n"
                  f"O acesso continua durante a carência. Vale falar com o responsável: {contato}")


def cancelada(empresa_id: int) -> None:
    with SessionLocal() as db:
        e, dono = _empresa(db, empresa_id)
        if not e:
            return
        ate = f"{e.pago_ate:%d/%m/%Y}" if e.pago_ate else "—"
        contato = f"{dono.nome} ({dono.email})" if dono else "—"
    notificar("cancelado", f"Assinatura cancelada: {e.nome}",
              f"{e.nome} cancelou a assinatura. O acesso pago vale até {ate}.\nResponsável: {contato}")


def resumo_testes(forcar: bool = False) -> int:
    """Uma vez por dia (depois das 09:00 de Brasília): empresas cujo teste termina em até 2 dias
    ou terminou nas últimas 24 h, sem assinatura. Devolve quantas entraram no resumo."""
    agora = banco.agora()
    with SessionLocal() as db:
        c = db.get(Config, CFG_DIA)
        hoje = agora.date().isoformat()
        if not forcar and (agora.hour < HORA_RESUMO_UTC or (c and c.valor == hoje)):
            return 0
        if not c:
            c = Config(chave=CFG_DIA, valor="")
            db.add(c)
        c.valor = hoje
        db.commit()
        linhas = []
        for e in db.scalars(select(Empresa).where(
                Empresa.teste_ate.is_not(None), Empresa.isenta.is_(False), Empresa.ativo.is_(True),
                Empresa.teste_ate.between(agora - timedelta(days=1), agora + timedelta(days=2)))):
            if e.asaas_assinatura_id or situacao(e) in ("ativa", "cortesia", "gratis"):
                continue
            _, dono = _empresa(db, e.id)
            quando = "terminou" if e.teste_ate < agora else "termina"
            linhas.append(f"- {e.nome}: {quando} em {e.teste_ate:%d/%m %H:%M} (UTC) · "
                          f"{dono.nome + ' ' + dono.email if dono else '—'}")
    if linhas:
        notificar("teste_fim", f"{len(linhas)} teste(s) grátis terminando sem assinatura",
                  "Estas empresas estão no fim do teste grátis e ainda não assinaram:\n\n"
                  + "\n".join(linhas) + "\n\nUm contato agora ajuda a não perder o cliente.")
    return len(linhas)


# ------------------------------------------------------------------ Master -> Avisos
@rota.get("/master/avisos")
def master_avisos(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/avisos.html", u=u, e=u.empresa, aba="master", sub="avisos",
                  TIPOS_AVISO=TIPOS, desligados=desligados(db), email_ok=email_envio.configurado(),
                  contas=_contas_master(db))


@rota.post("/master/avisos", dependencies=CSRF)
async def master_avisos_salvar(request: Request, u: Usuario = Depends(admin_logado),
                               db: Session = Depends(get_db)):
    form = await request.form()
    if form.get("acao") == "testar":
        n = notificar("cadastro", "Teste de aviso", "Este é um aviso de teste do Master. "
                      "Se chegou, os avisos estão funcionando.") if "cadastro" not in desligados(db) else 0
        avisar(request, f"Aviso de teste enviado para {n} conta(s) Master." if n
               else "Ligue “Nova empresa cadastrada” para enviar o teste.", "ok" if n else "erro")
        return ir("/master/avisos")
    off = ",".join(t for t in TIPOS if not form.get(f"t_{t}"))
    c = db.get(Config, CFG) or Config(chave=CFG, valor="")
    c.valor = off
    db.add(c)
    db.commit()
    avisar(request, "Avisos salvos.")
    return ir("/master/avisos")
