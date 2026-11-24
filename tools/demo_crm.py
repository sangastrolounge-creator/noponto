"""Leads de demonstração para fotografar o CRM (workflow "Capturas de tela")."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db as banco  # noqa: E402
from app.db import Lead, LeadAtividade, LeadTarefa, SessionLocal, Usuario  # noqa: E402
from sqlalchemy import select  # noqa: E402

banco.criar_tabelas()
with SessionLocal() as db:
    dono = db.scalar(select(Usuario).where(Usuario.is_admin.is_(True)))
    agora = banco.agora()
    dados = [("Bia Lima", "Padaria Sol", "98999990002", "chat", "novo", None, 0),
             ("Caio Rocha", "Mercado Bom", "98999990003", "teste", "teste", dono.id, 14990),
             ("Duda Alves", "Bar do Porto", "98999990004", "chat", "contato", dono.id, 9990),
             ("Edu Matos", "Clínica Vida", "98999990005", "indicacao", "proposta", dono.id, 29990),
             ("Fê Nunes", "Hotel Mar", "98999990006", "manual", "cliente", dono.id, 19990),
             ("Gil Souza", "Loja Gil", "98999990007", "chat", "perdido", dono.id, 0),
             ("Hugo Reis", "Oficina Reis", "98999990008", "chat", "novo", None, 0)]
    for i, (n, emp, tel, orig, et, d, v) in enumerate(dados):
        ld = Lead(nome=n, empresa_nome=emp, telefone=tel, origem=orig, etapa=et, dono_id=d, valor_centavos=v,
                  autorizou=True, criado_em=agora - timedelta(days=i), atualizado_em=agora - timedelta(hours=i))
        db.add(ld)
        db.flush()
        db.add(LeadAtividade(lead_id=ld.id, tipo="sistema", texto="Chegou pelo chat do site.", criado_em=ld.criado_em))
        if d:
            db.add(LeadAtividade(lead_id=ld.id, autor_id=d, tipo="ligacao", texto="Conversamos sobre o plano.",
                                 criado_em=agora - timedelta(hours=i)))
            db.add(LeadTarefa(lead_id=ld.id, dono_id=d, titulo="Ligar de novo", quando=agora + timedelta(hours=i - 2),
                              criado_em=agora))
    db.commit()
