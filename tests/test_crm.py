"""CRM do Master, equipe com áreas de acesso e chat ao vivo. Rodar da raiz: python tests/test_crm.py"""
import os
import re
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})
os.environ.pop("ANTHROPIC_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import core, crm  # noqa: E402
from app.db import Config, ConversaVenda, Empresa, Lead, LeadAtividade, LeadTarefa, Plataforma, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

pedidos = []


class _Msgs:
    def create(self, **kw):
        pedidos.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Resposta do robô.")])


class _Cliente:
    def __init__(self, **kw):
        self.messages = _Msgs()


sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=_Cliente)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def entrar_equipe(nome, email, areas):
    """O dono cadastra a pessoa; ela cria a senha pelo link e entra pela tela de login normal."""
    m = DONO
    dados = {"csrf": csrf(m, "/master/equipe"), "nome": nome, "email": email}
    dados.update({f"area_{a}": "1" for a in areas})
    r = m.post("/master/equipe", data=dados)
    assert r.status_code == 303
    assert "/convite/" in m.get("/master/equipe").text
    with SessionLocal() as db:
        p = db.scalar(select(Usuario).where(Usuario.email == email))
        token, pid = p.convite_token, p.id
    c = TestClient(app, follow_redirects=False)
    c.post(f"/convite/{token}", data={"csrf": csrf(c, f"/convite/{token}"), "senha": "segredo9", "consentimento": "1"})
    c = TestClient(app, follow_redirects=False)
    r = c.post("/entrar", data={"csrf": csrf(c, "/entrar"), "login": email, "senha": "segredo9"})
    return c, r, pid


DONO = TestClient(app, follow_redirects=False)


def main():
    m = DONO
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    with SessionLocal() as db:
        db.get(Plataforma, 1).whatsapp = "98981004999"
        db.merge(Config(chave="anthropic_api_key", valor="sk-teste"))
        db.commit()
        assert not db.scalars(select(Lead)).all()        # a conta da própria plataforma não vira lead
    core._plat_cache = None

    # visitante conversa e deixa o WhatsApp: vira lead na fila
    v = TestClient(app, follow_redirects=False)
    r1 = v.post("/vendedor/mensagem", json={"texto": "Tenho uma padaria com 8 pessoas"}).json()
    ok = v.post("/vendedor/contato", json={"conversa": r1["conversa"], "nome": "Bia Lima", "empresa": "Padaria Sol",
                                           "telefone": "(98) 99999-0002", "autorizo": True}).json()
    assert ok["ok"]
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.telefone == "98999990002"))
        assert ld and ld.origem == "chat" and ld.etapa == "novo" and ld.dono_id is None and ld.autorizou
        assert ld.nome == "Bia Lima" and ld.empresa_nome == "Padaria Sol" and ld.conversa_id
        lid = ld.id
        assert "padaria" in db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == lid)).first().texto
    tela = m.get("/master/crm").text
    assert "Bia Lima" in tela and "Pegar o próximo da fila" in tela

    # atendente só com CRM: entra pela tela de login e cai direto no CRM
    ana, r, ana_id = entrar_equipe("Ana Atendente", "ana@plataforma.com", ["crm"])
    assert r.status_code == 303 and r.headers["location"] == "/master/crm", r.headers.get("location")
    assert ana.get("/master").headers["location"] == "/master/crm"              # sem suporte: não vê empresas
    assert ana.get("/master/planos").headers["location"] == "/master/crm"       # sem configurações
    assert ana.get("/master/cupons").headers["location"] == "/master/crm"       # sem financeiro
    assert ana.get("/master/crm").status_code == 200
    menu = ana.get("/master/crm").text
    assert "/master/equipe" not in menu and "/master/cupons" not in menu and "Chat ao vivo" in menu
    assert 'value="todos"' not in menu                                          # sem supervisão

    # pega da fila, muda a etapa arrastando, registra a ligação e marca o retorno
    ana.post("/master/crm/pegar", data={"csrf": csrf(ana, "/master/crm")})
    with SessionLocal() as db:
        assert db.get(Lead, lid).dono_id == ana_id
    tok = csrf(ana, f"/master/crm/{lid}")
    r = ana.post(f"/master/crm/{lid}/etapa", json={"etapa": "proposta"}, headers={"X-CSRF": tok})
    assert r.status_code == 200 and r.json()["etapa"] == "proposta"
    assert ana.post(f"/master/crm/{lid}/etapa", json={"etapa": "contato"}).status_code in (400, 403)  # sem CSRF
    ana.post(f"/master/crm/{lid}/atividade", data={"csrf": tok, "tipo": "ligacao", "texto": "Pediu proposta por e-mail"})
    ana.post(f"/master/crm/{lid}/tarefa", data={"csrf": tok, "titulo": "Mandar proposta", "quando": "2020-01-01T10:00"})
    ana.post(f"/master/crm/{lid}", data={"csrf": tok, "nome": "Bia Lima", "empresa_nome": "Padaria Sol",
                                          "telefone": "98999990002", "email": "bia@sol.com", "funcionarios": "8",
                                          "valor": "149,90", "dono_id": ""})
    with SessionLocal() as db:
        ld = db.get(Lead, lid)
        assert ld.valor_centavos == 14990 and ld.email == "bia@sol.com" and ld.dono_id == ana_id
        tipos = [a.tipo for a in db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == lid))]
        assert "etapa" in tipos and "ligacao" in tipos
        tarefa = db.scalar(select(LeadTarefa).where(LeadTarefa.lead_id == lid))
        assert tarefa and tarefa.dono_id == ana_id
    pag = ana.get(f"/master/crm/{lid}").text
    assert "Pediu proposta" in pag and "wa.me/5598999990002" in pag and "Padaria" in pag
    assert "Mandar proposta" in ana.get("/master/crm/tarefas").text
    ana.post(f"/master/crm/tarefa/{tarefa.id}/feita", data={"csrf": tok, "volta": "/master/crm/tarefas"})
    with SessionLocal() as db:
        assert db.get(LeadTarefa, tarefa.id).feita_em

    # outro atendente não vê o lead da Ana; o dono (supervisão) vê e pode passar para a fila
    beto, r, beto_id = entrar_equipe("Beto Vendas", "beto@plataforma.com", ["crm"])
    assert "Bia Lima" not in beto.get("/master/crm").text
    assert beto.get(f"/master/crm/{lid}").headers["location"] == "/master/crm"
    assert "Bia Lima" in m.get("/master/crm").text and "Ana Atendente" in m.get(f"/master/crm/{lid}").text

    # chat ao vivo: Ana assume a conversa, o robô para; a mensagem dela chega no chat do site
    n = len(pedidos)
    cid = ld.conversa_id
    dados = ana.get("/master/crm/ao-vivo.json").json()
    assert any(c["id"] == cid for c in dados["conversas"]) and "equipe" not in dados
    assert ana.post(f"/master/crm/conversa/{cid}/assumir", headers={"X-CSRF": tok}).json()["ok"]
    assert beto.post(f"/master/crm/conversa/{cid}/assumir",
                     headers={"X-CSRF": csrf(beto, "/master/crm")}).status_code == 409
    r = v.post("/vendedor/mensagem", json={"texto": "Tem desconto?", "conversa": r1["conversa"]}).json()
    assert r["humano"] and r["resposta"] == "" and len(pedidos) == n
    ana.post(f"/master/crm/conversa/{cid}/mensagem", json={"texto": "Oi Bia, tem sim: 10% no anual."},
             headers={"X-CSRF": tok})
    novas = v.get(f"/vendedor/novas?conversa={r1['conversa']}&desde=0").json()
    textos = [x["texto"] for x in novas["mensagens"]]
    assert novas["humano"] and "Oi Bia, tem sim: 10% no anual." in textos and any("entrou" in t for t in textos)
    assert [x for x in novas["mensagens"] if x["papel"] == "atendente"][0]["nome"] == "Ana"
    assert v.get(f"/vendedor/novas?conversa={r1['conversa']}&desde={novas['total']}").json()["mensagens"] == []
    sup = m.get("/master/crm/ao-vivo.json").json()
    linha = [p for p in sup["equipe"] if p["nome"] == "Ana Atendente"][0]
    assert linha["chats"] == 1 and linha["atividades_hoje"] >= 2
    assert "Equipe agora" in m.get("/master/crm/ao-vivo").text
    ana.post(f"/master/crm/conversa/{cid}/devolver", headers={"X-CSRF": tok})
    r = v.post("/vendedor/mensagem", json={"texto": "E para 20 pessoas?", "conversa": r1["conversa"]}).json()
    assert r["resposta"] == "Resposta do robô." and len(pedidos) == n + 1
    papeis = [x["role"] for x in pedidos[-1]["messages"]]
    assert papeis[0] == "user" and all(a != b for a, b in zip(papeis, papeis[1:]))   # alternância para a API

    # cadastro de teste vira lead "Em teste"; a assinatura vira "Cliente" no sincronismo
    c = TestClient(app, follow_redirects=False)
    c.post("/cadastro", data={"csrf": csrf(c, "/cadastro"), "aceito": "1", "empresa": "Mercado Bom", "nome": "Caio",
                              "email": "caio@mercado.com", "senha": "segredo1"})
    with SessionLocal() as db:
        lt = db.scalar(select(Lead).where(Lead.email == "caio@mercado.com"))
        assert lt and lt.origem == "teste" and lt.etapa == "teste" and lt.empresa_id
        from datetime import timedelta
        from app import db as banco
        e = db.get(Empresa, lt.empresa_id)
        e.pago_ate = banco.agora() + timedelta(days=30)
        db.commit()
        lt_id = lt.id
    crm.sincronizar()
    with SessionLocal() as db:
        assert db.get(Lead, lt_id).etapa == "cliente"

    # supervisor da equipe vê todos; tirar o acesso devolve os leads para a fila
    m.post(f"/master/equipe/{beto_id}", data={"csrf": csrf(m, "/master/equipe"), "area_supervisao": "1"})
    with SessionLocal() as db:
        assert db.get(Usuario, beto_id).areas_master == {"crm", "supervisao"}
    assert "Bia Lima" in beto.get("/master/crm").text
    m.post(f"/master/equipe/{ana_id}", data={"csrf": csrf(m, "/master/equipe"), "remover": "1"})
    with SessionLocal() as db:
        assert db.get(Lead, lid).dono_id is None and not db.get(Usuario, ana_id).acesso_master
    assert ana.get("/master/crm").status_code == 303 and "/master" not in ana.get("/master/crm").headers["location"]
    print("ok: CRM, equipe e chat ao vivo")


if __name__ == "__main__":
    main()
