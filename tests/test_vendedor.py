"""Assistente de vendas da página inicial (chat + WhatsApp). Rodar da raiz: python tests/test_vendedor.py"""
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

from app import core  # noqa: E402
from app.db import Config, ConversaVenda, Plataforma, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

pedidos = []


class _Msgs:
    def create(self, **kw):
        pedidos.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Temos plano para 10 pessoas.")])


class _Cliente:
    def __init__(self, **kw):
        self.messages = _Msgs()


sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=_Cliente)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    v = TestClient(app, follow_redirects=False)
    assert "vd-botao" not in v.get("/").text          # sem WhatsApp e sem chave: nada aparece
    with SessionLocal() as db:
        db.get(Plataforma, 1).whatsapp = "98981004999"
        db.commit()
    core._plat_cache = None
    pag = v.get("/").text
    assert "vd-botao" in pag and "wa.me/5598981004999" in pag and 'id="vd-form"' not in pag   # só WhatsApp
    # sem chave: resposta de fallback
    r = v.post("/vendedor/mensagem", json={"texto": "Quanto custa?"})
    assert r.status_code == 200 and "WhatsApp" in r.json()["resposta"]
    # com chave: chat ligado, histórico e planos vão para o modelo
    with SessionLocal() as db:
        db.merge(Config(chave="anthropic_api_key", valor="sk-teste"))
        db.commit()
    assert 'id="vd-form"' in v.get("/").text
    r1 = v.post("/vendedor/mensagem", json={"texto": "Tenho um restaurante com 10 funcionários"}).json()
    assert r1["resposta"] == "Temos plano para 10 pessoas." and r1["conversa"]
    r2 = v.post("/vendedor/mensagem", json={"texto": "E o preço?", "conversa": r1["conversa"]}).json()
    assert r2["conversa"] == r1["conversa"]
    ult = pedidos[-1]
    assert [x["role"] for x in ult["messages"]] == ["user", "assistant", "user"]
    assert "Planos e preços" in ult["system"] and "Portaria" in ult["system"]
    assert v.post("/vendedor/mensagem", json={"texto": "   "}).status_code == 400
    v.post("/vendedor/whatsapp", json={"conversa": r1["conversa"]})
    with SessionLocal() as db:
        c = db.scalar(select(ConversaVenda).where(ConversaVenda.chave == r1["conversa"]))
        assert c.foi_whatsapp and "restaurante" in c.mensagens
    # contato pelo formulário: validação e autorização
    assert v.post("/vendedor/contato", json={"conversa": r1["conversa"], "nome": "Ana", "telefone": "123",
                                             "autorizo": True}).status_code == 400
    assert "autorização" in v.post("/vendedor/contato", json={"conversa": r1["conversa"], "nome": "Ana",
                                                             "telefone": "(98) 99999-0001"}).json()["erro"]
    ok = v.post("/vendedor/contato", json={"conversa": r1["conversa"], "nome": "Ana Souza", "empresa": "Bar Mar",
                                           "telefone": "(98) 99999-0001", "autorizo": True}).json()
    assert ok["ok"] and "Ana" in ok["mensagem"]
    # número escrito no próprio chat também vira contato
    r3 = v.post("/vendedor/mensagem", json={"texto": "me chama no 98 98888-7777 por favor"}).json()
    assert r3["contato"]
    with SessionLocal() as db:
        c3 = db.scalar(select(ConversaVenda).where(ConversaVenda.chave == r3["conversa"]))
        assert c3.telefone == "98988887777" and not c3.autorizou
    tela = m.get("/master/conversas").text
    assert "restaurante" in tela and "foi para o WhatsApp" in tela
    assert "Contatos para retornar (2)" in tela and "wa.me/5598999990001?text=" in tela and "Bar Mar" in tela
    cid = re.search(r'/master/conversas/(\d+)/situacao', tela).group(1)
    m.post(f"/master/conversas/{cid}/situacao", data={"csrf": csrf(m, "/master/conversas"), "situacao": "fechou"})
    with SessionLocal() as db:
        assert db.get(ConversaVenda, int(cid)).situacao == "fechou"
    assert v.get("/master/conversas").status_code in (303, 307, 403, 404)
    print("ok: assistente de vendas")


if __name__ == "__main__":
    main()
