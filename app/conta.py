"""Minha conta e exclusão de conta (exigência do Google Play e da App Store).

- Funcionário ou gerente: a exclusão vale na hora. Sem registros que a empresa precisa guardar, o
  cadastro é apagado. Com registros (ponto, ausências, folhas), o acesso é encerrado e os dados de
  login, rosto, PIN, aparelhos e avisos são apagados; o que a lei trabalhista manda guardar fica
  com a empresa (LGPD, art. 16, I). O gestor recebe um alerta e pode mandar um convite novo.
- Dono da conta: o pedido encerra a conta da empresa inteira (assinatura e dados de todos), então
  fica registrado e a equipe da plataforma conclui e confirma por e-mail.
- /excluir-conta: página pública que explica como pedir, para o link das lojas."""
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from . import email_envio, legal, regras
from .core import (avisar, conferir_senha, dentro_do_limite, exigir_csrf, ir, plataforma, render,
                   usuario_logado)
from .db import Alerta, Aparelho, PushSub, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
PRAZO_DIAS = 15


@rota.get("/conta")
def conta(request: Request, u: Usuario = Depends(usuario_logado)):
    return render(request, "conta.html", u=u, e=u.empresa, aba="conta", prazo=PRAZO_DIAS)


@rota.post("/conta/excluir", dependencies=CSRF)
def excluir(request: Request, tarefas_bg: BackgroundTasks, senha: str = Form(""), confirmo: str = Form(""),
            u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    if not dentro_do_limite(f"excluir-conta:{u.id}", 5, 600):
        avisar(request, "Muitas tentativas. Aguarde alguns minutos.", "erro")
        return ir("/conta")
    if not confirmo:
        avisar(request, "Marque a caixa confirmando que entendeu o que acontece.", "erro")
        return ir("/conta#excluir")
    if not conferir_senha(senha, u.senha_hash):
        avisar(request, "Senha incorreta. Nada foi excluído.", "erro")
        return ir("/conta#excluir")
    e = u.empresa
    if u.dono:
        if not e.exclusao_pedida_em:
            e.exclusao_pedida_em = banco.agora()
            regras.registrar(db, e.id, u.nome, "exclusão da conta pedida",
                             "O dono pediu o encerramento da conta da empresa e a exclusão dos dados.")
            db.commit()
            para = os.environ.get("ADMIN_EMAIL", "").strip() or (plataforma().get("email_legal") or "").strip()
            if para and email_envio.configurado():
                tarefas_bg.add_task(email_envio.enviar, para, f"Pedido de exclusão: {e.nome}",
                                    f"{u.nome} ({u.email}) pediu o encerramento da conta da empresa "
                                    f"{e.nome} (id {e.id}) e a exclusão dos dados. Prazo: {PRAZO_DIAS} dias.")
        avisar(request, f"Pedido registrado. A equipe do {plataforma()['nome']} encerra a conta da empresa e "
                        f"apaga os dados em até {PRAZO_DIAS} dias, e confirma por e-mail.")
        return ir("/conta")
    from .desligamento import apagar_cadastro, tem_registros
    nome, gid = u.nome, e.id
    if tem_registros(db, u.id):
        u.senha_hash, u.pin_hash, u.google_sub = "", None, None
        u.facial_descritor, u.convite_token, u.redefinir_hash = "", None, None
        u.conta_excluida_em = banco.agora()
        for m in (Aparelho, PushSub):
            db.execute(m.__table__.delete().where(m.usuario_id == u.id))
        db.add(Alerta(empresa_id=gid, usuario_id=u.id, tipo="conta_excluida", criado_em=banco.agora(),
                      texto=f"{nome} excluiu a própria conta de acesso. As marcações de ponto e os documentos "
                            "ficam guardados. Para ele(a) voltar a usar, mande um convite novo pela Equipe."))
        regras.registrar(db, gid, nome, "conta excluída pela pessoa",
                         f"{nome}: acesso encerrado; registros trabalhistas guardados.", usuario_id=u.id)
    else:
        apagar_cadastro(db, u)
        regras.registrar(db, gid, nome, "conta excluída pela pessoa", f"{nome}: cadastro apagado.")
    db.commit()
    request.session.clear()
    avisar(request, "Sua conta foi excluída.")
    return ir("/entrar")


@rota.get("/excluir-conta")
def pagina_publica(request: Request):
    return render(request, "legal/excluir_pt.html", L=legal.dados(), doc="excluir", cod="pt", prazo=PRAZO_DIAS)


def pedidos_pendentes(db: Session) -> list:
    from .db import Empresa
    return list(db.scalars(select(Empresa).where(Empresa.exclusao_pedida_em.is_not(None))
                           .order_by(Empresa.exclusao_pedida_em)))
