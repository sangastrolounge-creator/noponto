"""Desligamento de funcionário (demissão) e exclusão do cadastro, na tela Equipe.

Desligar mantém tudo o que a lei manda guardar (marcações de ponto, espelhos, folhas:
Portaria MTP 671/2021 e prazos da CLT) e só tira a pessoa da escala e do acesso.
Excluir apaga o cadastro de vez, e só vale para quem não tem nenhum desses registros
(cadastro feito por engano, pessoa que nunca trabalhou).
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import regras
from .core import avisar, da_empresa, exigir_csrf, gestor_logado, ir
from .db import (Ajuste, AjusteBanco, Alerta, Aparelho, Ausencia, AvaliacaoTurno, Batida, DadosRH,
                 Disponibilidade, EscalaFixa, FechamentoLinha, FeriasAjuste, FolhaItem,
                 FuncaoUsuario, LeituraMensagem, Mensagem, MotivoAjuste, Oferta, PushSub,
                 RespostaTurno, Sobreaviso, Tarefa, TarefaFeita, Turno, Usuario, get_db)

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]

TIPOS = {
    "sem_justa_causa": "Dispensa sem justa causa",
    "pedido": "Pedido de demissão",
    "justa_causa": "Dispensa por justa causa",
    "acordo": "Acordo entre as partes (art. 484-A)",
    "fim_contrato": "Fim do contrato (experiência ou prazo)",
    "outro": "Outro motivo",
}
AVISOS = {
    "trabalhado": "Trabalhado",
    "indenizado": "Indenizado",
    "dispensado": "Dispensado",
    "nao_se_aplica": "Não se aplica",
}
# Registros que a empresa precisa guardar: com qualquer um deles, o cadastro não é apagado.
GUARDADOS = (Batida, FechamentoLinha, FolhaItem, Ajuste, Ausencia, Sobreaviso, MotivoAjuste,
             FeriasAjuste, AjusteBanco)


def tem_registros(db: Session, uid: int) -> bool:
    return any(db.scalar(select(m.id).where(m.usuario_id == uid).limit(1)) for m in GUARDADOS)


def com_registros(db: Session, ids: list[int]) -> set[int]:
    """Quais dessas pessoas têm registros que precisam ser guardados."""
    achados: set[int] = set()
    if ids:
        for m in GUARDADOS:
            achados.update(db.scalars(select(m.usuario_id).where(m.usuario_id.in_(ids)).distinct()))
    return achados


def _tirar_da_escala(db: Session, f: Usuario, depois_de: date):
    """Apaga os turnos depois do último dia (os que não têm batida) e a escala fixa."""
    com_batida = select(Batida.turno_id).where(Batida.turno_id.is_not(None))
    db.execute(delete(Turno).where(Turno.usuario_id == f.id, Turno.data > depois_de,
                                   Turno.id.not_in(com_batida)))
    db.execute(delete(EscalaFixa).where(EscalaFixa.usuario_id == f.id))


def _bloquear_acesso(db: Session, f: Usuario):
    f.ativo, f.na_escala, f.convite_token = False, False, None
    f.facial_descritor = ""          # biometria não fica guardada depois do vínculo
    db.execute(delete(Aparelho).where(Aparelho.usuario_id == f.id))
    db.execute(delete(PushSub).where(PushSub.usuario_id == f.id))


def encerrar_vencidos(db: Session, empresa_id: int, hoje: date) -> int:
    """Chamado pelo agendador: quem teve o último dia antes de hoje perde o acesso."""
    n = 0
    for f in db.scalars(select(Usuario).where(Usuario.empresa_id == empresa_id,
                                              Usuario.ativo.is_(True),
                                              Usuario.desligado_em.is_not(None),
                                              Usuario.desligado_em < hoje)):
        _bloquear_acesso(db, f)
        n += 1
    if n:
        db.commit()
    return n


@rota.post("/painel/equipe/{fid}/desligar", dependencies=CSRF)
def desligar(request: Request, fid: int, data: str = Form(""), tipo: str = Form(""),
             aviso: str = Form(""), obs: str = Form(""), u: Usuario = Depends(gestor_logado),
             db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, fid, u)
    volta = f"/painel/equipe#p{f.id}"
    if f.id == u.id or f.dono:
        avisar(request, "O dono da conta não pode ser desligado por aqui.", "erro")
        return ir(volta)
    if f.gestor and not u.dono:
        avisar(request, "Só o dono da conta desliga um gestor.", "erro")
        return ir(volta)
    hoje = regras.hoje(u.empresa)
    try:
        dia = date.fromisoformat(data)
    except ValueError:
        avisar(request, "Informe o último dia de trabalho.", "erro")
        return ir(volta)
    if dia > hoje + timedelta(days=90) or dia < hoje - timedelta(days=365):
        avisar(request, "O último dia de trabalho precisa estar entre 1 ano atrás e 90 dias à frente.", "erro")
        return ir(volta)
    if f.data_admissao and dia < f.data_admissao:
        avisar(request, "O último dia de trabalho não pode ser antes da admissão.", "erro")
        return ir(volta)
    if tipo not in TIPOS or aviso not in AVISOS:
        avisar(request, "Escolha o tipo de desligamento e o aviso prévio.", "erro")
        return ir(volta)
    f.desligado_em, f.desligamento_tipo, f.aviso_previo = dia, tipo, aviso
    f.desligamento_obs = obs.strip()[:300]
    _tirar_da_escala(db, f, dia)
    if dia < hoje:
        _bloquear_acesso(db, f)
    regras.registrar(db, u.empresa_id, u.nome, "desligamento",
                     f"{f.nome}: último dia {dia:%d/%m/%Y}; {TIPOS[tipo]}; aviso prévio "
                     f"{AVISOS[aviso].lower()}." + (f" {f.desligamento_obs}" if f.desligamento_obs else ""),
                     usuario_id=f.id, dia=dia)
    db.commit()
    if dia < hoje:
        avisar(request, f"{f.primeiro_nome} foi desligado(a). Os registros de ponto continuam guardados.")
    else:
        avisar(request, f"Desligamento de {f.primeiro_nome} registrado. O acesso fica liberado até "
                        f"{dia:%d/%m/%Y} e é encerrado no dia seguinte.")
    return ir(volta)


@rota.post("/painel/equipe/{fid}/desfazer-desligamento", dependencies=CSRF)
def desfazer(request: Request, fid: int, u: Usuario = Depends(gestor_logado),
             db: Session = Depends(get_db)):
    from . import master
    f = da_empresa(db, Usuario, fid, u)
    if f.gestor and not u.dono:
        avisar(request, "Só o dono da conta muda os dados de outro gestor.", "erro")
        return ir("/painel/equipe")
    if not f.ativo and f.papel == "funcionario" and master.sem_vaga(db, u.empresa):
        avisar(request, master.sem_vaga(db, u.empresa), "erro")
        return ir("/painel/equipe")
    f.desligado_em, f.desligamento_tipo, f.aviso_previo, f.desligamento_obs = None, "", "", ""
    f.ativo = f.na_escala = True
    regras.registrar(db, u.empresa_id, u.nome, "desligamento desfeito", f"{f.nome} voltou para a equipe.",
                     usuario_id=f.id)
    db.commit()
    avisar(request, f"{f.primeiro_nome} voltou para a equipe. Monte a escala dele(a) de novo.")
    return ir(f"/painel/equipe#p{f.id}")


def apagar_cadastro(db: Session, f: Usuario) -> None:
    """Apaga o cadastro e tudo o que é só dele. Use só para quem não tem registros a guardar."""
    for m in (Turno, EscalaFixa, TarefaFeita, Alerta, Aparelho, PushSub, FuncaoUsuario,
              Disponibilidade, AvaliacaoTurno, LeituraMensagem, RespostaTurno, DadosRH):
        db.execute(delete(m).where(m.usuario_id == f.id))
    db.execute(update(Tarefa).where(Tarefa.usuario_id == f.id).values(usuario_id=None))
    db.execute(update(Oferta).where(Oferta.de_id == f.id).values(de_id=None))
    db.execute(update(Oferta).where(Oferta.candidato_id == f.id).values(candidato_id=None))
    for msg in db.scalars(select(Mensagem).where(Mensagem.autor_id == f.id)):
        db.execute(delete(LeituraMensagem).where(LeituraMensagem.mensagem_id == msg.id))
        db.delete(msg)
    db.delete(f)


@rota.post("/painel/equipe/{fid}/excluir", dependencies=CSRF)
def excluir(request: Request, fid: int, confirmar: str = Form(""), u: Usuario = Depends(gestor_logado),
            db: Session = Depends(get_db)):
    f = da_empresa(db, Usuario, fid, u)
    if f.id == u.id or f.dono or (f.gestor and not u.dono):
        avisar(request, "Este cadastro não pode ser excluído por aqui.", "erro")
        return ir("/painel/equipe")
    if tem_registros(db, f.id):
        avisar(request, "Este cadastro tem marcações de ponto, ausências ou folhas, que a empresa "
                        "precisa guardar. Use o desligamento.", "erro")
        return ir(f"/painel/equipe#p{f.id}")
    if " ".join(confirmar.split()).casefold() != " ".join(f.nome.split()).casefold():
        avisar(request, "Para excluir, digite o nome completo exatamente como está no cadastro.", "erro")
        return ir(f"/painel/equipe#p{f.id}")
    nome = f.nome
    apagar_cadastro(db, f)
    regras.registrar(db, u.empresa_id, u.nome, "cadastro excluído",
                     f"{nome}: cadastro apagado (não tinha registros de ponto).")
    db.commit()
    avisar(request, f"Cadastro de {nome} excluído.")
    return ir("/painel/equipe")
