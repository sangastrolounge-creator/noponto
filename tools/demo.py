"""Cria uma empresa de demonstração no banco LOCAL, para ver as telas com dados.
Rodar da raiz: .venv/Scripts/python tools/demo.py   (não usar em produção)

Acesso do gestor: demo@exemplo.com.br / demo1234
Acesso dos funcionários: celular 98 90000-0001 a 0004 / demo1234
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import regras  # noqa: E402
from app.core import gerar_hash  # noqa: E402
from app.db import (Batida, Empresa, SessionLocal, Tarefa, TarefaFeita, Turno,  # noqa: E402
                    Usuario, agora, criar_tabelas)

criar_tabelas()
with SessionLocal() as db:
    if db.scalar(select(Usuario).where(Usuario.email == "demo@exemplo.com.br")):
        sys.exit("A demonstração já existe.")
    e = Empresa(nome="Padaria Bom Dia", endereco="Av. dos Holandeses, 100 — São Luís",
                lat=-2.4897, lng=-44.2866)
    db.add(e)
    db.flush()
    senha = gerar_hash("demo1234")
    db.add(Usuario(empresa_id=e.id, nome="Marina Gestora", email="demo@exemplo.com.br",
                   senha_hash=senha, papel="gestor", na_escala=False))
    nomes = [("Ana Souza", "Caixa"), ("Bruno Lima", "Padeiro"), ("Carla Dias", "Atendente"),
             ("Diego Rocha", "Atendente")]
    equipe = []
    for i, (nome, cargo) in enumerate(nomes, 1):
        f = Usuario(empresa_id=e.id, nome=nome, cargo=cargo, celular=f"989000000{i:02d}",
                    senha_hash=senha)
        db.add(f)
        equipe.append(f)
    db.flush()

    al = regras.agora_local(e)
    hoje = regras.dia_de(al)
    seg = hoje - timedelta(days=hoje.weekday())
    # Turno que começou há algumas horas, sempre dentro do dia de trabalho (vira às 4h).
    ini = max(al - timedelta(hours=5), datetime.combine(hoje, datetime.min.time())
              + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
    turno = (f"{ini:%H:%M}", f"{ini + timedelta(hours=8):%H:%M}")
    for i in range(7):
        d = seg + timedelta(days=i)
        for n, f in enumerate(equipe):
            folga = (i + n) % 7 == 6
            db.add(Turno(empresa_id=e.id, usuario_id=f.id, data=d, folga=folga,
                         inicio="" if folga else turno[0], fim="" if folga else turno[1]))
    db.flush()

    def bate(f, tipo, quando, atraso=0):
        t = db.scalar(select(Turno).where(Turno.usuario_id == f.id, Turno.data == hoje))
        db.add(Batida(empresa_id=e.id, usuario_id=f.id, tipo=tipo, dia=hoje,
                      momento=regras.para_utc(e, quando), lat=e.lat, lng=e.lng, precisao_m=14,
                      distancia_m=22, dentro=True, turno_id=t.id if t else None,
                      atraso_min=atraso))

    bate(equipe[0], "entrada", ini - timedelta(minutes=4))
    if al > ini + timedelta(minutes=26):
        bate(equipe[1], "entrada", ini + timedelta(minutes=25), 25)
    # Carla e Diego ainda não bateram: aparecem como "Não bateu o ponto".

    tarefas = [Tarefa(empresa_id=e.id, titulo=t, ordem=i) for i, t in enumerate(
        ["Conferir o troco do caixa", "Limpar a vitrine", "Repor os pães", "Fechar o caixa",
         "Enviar o relatório do dia"])]
    db.add_all(tarefas)
    db.flush()
    db.add(TarefaFeita(empresa_id=e.id, tarefa_id=tarefas[0].id, data=hoje,
                       usuario_id=equipe[0].id, feito_em=agora() - timedelta(minutes=90)))
    db.add(TarefaFeita(empresa_id=e.id, tarefa_id=tarefas[2].id, data=hoje,
                       usuario_id=equipe[1].id, feito_em=agora() - timedelta(minutes=40)))
    db.commit()
    regras.verificar_turnos(db)
print("Demonstração criada.")
