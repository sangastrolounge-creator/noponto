"""Áudio no WhatsApp: transcrição, resposta em voz, cliente que já usa o sistema e problema virando tarefa.
Rodar da raiz: python tests/test_whatsapp_voz.py"""
import hashlib
import hmac
import json
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
                   "ADMIN_EMAIL": "dono@plataforma.com",
                   "META_APP_SECRET": "segredo-do-app", "META_VERIFY_TOKEN": "palavra",
                   "WA_TOKEN": "token-de-teste", "WA_PHONE_ID": "1234567890",
                   "ELEVENLABS_API_KEY": "chave-eleven"})
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("VOZ_ID", None)

roteiro = []
pedidos = []


class _Msgs:
    def create(self, **kw):
        pedidos.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text",
                                                                    text=roteiro.pop(0) if roteiro else "Oi!")])


sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: types.SimpleNamespace(messages=_Msgs()))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import revisor, whatsapp, whatsapp_voz  # noqa: E402
from app.db import Config, Lead, LeadTarefa, SessionLocal, Usuario, WhatsMensagem  # noqa: E402
from app.main import app  # noqa: E402

enviados = []
whatsapp.enviar_api = lambda para, corpo: (enviados.append((para, corpo)) or (f"wamid.V{len(enviados)}", ""))


# ---------------------------------------------------------------- Meta e ElevenLabs de mentira
class Resp:
    def __init__(self, status=200, dados=None, conteudo=b"", tipo=""):
        self.status_code, self._d = status, dados
        self.content = conteudo if dados is None else json.dumps(dados).encode()
        self.text = self.content.decode("latin-1")
        self.headers = {"content-type": tipo}

    def json(self):
        return self._d if self._d is not None else json.loads(self.content)


chamadas = []
estado = {"fala": b"OggS\x00voz-de-teste", "transcricao": "Quanto custa para doze funcionários?"}


def _get(url, **kw):
    chamadas.append(("GET", url))
    if url.endswith("/midia-1") or url.endswith("/midia-2") or url.endswith("/midia-3"):
        return Resp(dados={"url": "https://lookaside.fbsbx.com/arquivo", "mime_type": "audio/ogg; codecs=opus",
                           "file_size": 9000})
    if "lookaside" in url:
        assert kw["headers"]["Authorization"] == "Bearer token-de-teste"
        return Resp(conteudo=b"OggS-audio-da-pessoa", tipo="audio/ogg")
    return Resp(status=404, dados={})


def _post(url, **kw):
    chamadas.append(("POST", url))
    if url.endswith("/speech-to-text"):
        assert kw["headers"]["xi-api-key"] == "chave-eleven" and kw["data"]["language_code"] == "por"
        return Resp(dados={"text": estado["transcricao"]})
    if "/text-to-speech/" in url:
        assert kw["params"]["output_format"].startswith("opus_")
        chamadas.append(("FALA", kw["json"]["text"]))
        return Resp(conteudo=estado["fala"], tipo="audio/ogg")
    if url.endswith("/media"):
        assert kw["data"]["messaging_product"] == "whatsapp"
        return Resp(dados={"id": "midia-resposta"})
    return Resp(status=500, dados={})


whatsapp_voz.httpx = types.SimpleNamespace(get=_get, post=_post, HTTPError=RuntimeError)


def audio(wa, nome, midia, mid):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "1234567890"}, "contacts": [{"wa_id": wa, "profile": {"name": nome}}],
        "messages": [{"from": wa, "id": mid, "type": "audio",
                      "audio": {"id": midia, "mime_type": "audio/ogg; codecs=opus", "voice": True}}]}}]}]}


def texto(wa, nome, corpo, mid):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "1234567890"}, "contacts": [{"wa_id": wa, "profile": {"name": nome}}],
        "messages": [{"from": wa, "id": mid, "type": "text", "text": {"body": corpo}}]}}]}]}


def postar(cli, dados):
    corpo = json.dumps(dados).encode()
    sig = "sha256=" + hmac.new(b"segredo-do-app", corpo, hashlib.sha256).hexdigest()
    return cli.post("/webhooks/whatsapp", content=corpo,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    meta = TestClient(app, follow_redirects=False)
    with SessionLocal() as db:
        db.merge(Config(chave=revisor.CHAVE_CONFIG, valor="sk-teste"))
        db.commit()

    # robô desligado: o áudio chega e vira texto na conversa mesmo assim (o time lê no CRM)
    postar(meta, audio("5598999990002", "Bia", "midia-1", "v1"))
    with SessionLocal() as db:
        w = db.scalar(select(WhatsMensagem).where(WhatsMensagem.mid == "v1"))
        assert w.audio and w.midia_id == "" and w.texto == "[áudio] Quanto custa para doze funcionários?", w.texto
    assert enviados == [] and pedidos == []

    # sem VOZ_ID a voz não liga (botão travado e rota recusa)
    t = m.get("/master/rastreamento").text
    assert "Transcrevendo áudios" in t and "Voz desligada" in t
    m.post("/master/rastreamento/whatsapp-voz", data={"csrf": csrf(m, "/master/rastreamento"), "ligar": "1"})
    with SessionLocal() as db:
        assert not whatsapp_voz.ligado(db)

    # robô ligado, voz desligada: responde ao que foi dito, em texto
    m.post("/master/rastreamento/whatsapp-robo", data={"csrf": csrf(m, "/master/rastreamento"), "ligar": "1"})
    roteiro.append("Olá! Sou o assistente virtual. Para 12 pessoas o plano certo é o Essencial. [TESTE]")
    postar(meta, audio("5598999990002", "Bia", "midia-2", "v2"))
    assert len(enviados) == 1 and enviados[0][1]["type"] == "text"
    assert "Essencial" in enviados[0][1]["text"]["body"] and "/cadastro?utm_source=whatsapp" in enviados[0][1]["text"]["body"]
    sistema = pedidos[-1]["system"]
    assert "já transcritos" in sistema and "VAI SER FALADA" not in sistema and "QUEM ESTÁ ESCREVENDO" not in sistema
    assert "doze funcionários" in pedidos[-1]["messages"][-1]["content"]

    # liga a voz: áudio curto é respondido com mensagem de voz, e o link segue em texto
    os.environ["VOZ_ID"] = "voz-da-marca"
    m.post("/master/rastreamento/whatsapp-voz", data={"csrf": csrf(m, "/master/rastreamento"), "ligar": "1"})
    assert "Respondendo em voz" in m.get("/master/rastreamento").text
    roteiro.append("Claro! O Essencial custa cinquenta e nove por mês, e você pode testar de graça. [TESTE]")
    postar(meta, audio("5598999990002", "Bia", "midia-3", "v3"))
    assert "VAI SER FALADA" in pedidos[-1]["system"]
    assert enviados[1][1] == {"type": "audio", "audio": {"id": "midia-resposta", "voice": True}}
    assert enviados[2][1]["type"] == "text" and enviados[2][1]["text"]["body"].startswith("Comece o teste grátis")
    falado = [c[1] for c in chamadas if c[0] == "FALA"][-1]
    assert "cadastro" not in falado and "[TESTE]" not in falado
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.telefone == "98999990002"))
        lid = ld.id
        voz = db.scalar(select(WhatsMensagem).where(WhatsMensagem.mid == "wamid.V2"))
        assert voz.robo and voz.audio and "cinquenta e nove" in voz.texto
    d = m.get(f"/master/crm/{lid}/whatsapp.json").json()
    assert [x["quem"] for x in d["mensagens"][-2:]] == ["robô, em voz", "robô"]

    # texto continua sendo respondido em texto, mesmo com a voz ligada
    roteiro.append("Pode perguntar!")
    postar(meta, texto("5598999990002", "Bia", "Mais uma dúvida", "v4"))
    assert enviados[-1][1]["type"] == "text" and "VAI SER FALADA" not in pedidos[-1]["system"]

    # a fala falhou (não veio em Ogg): a resposta vai em texto, sem perder a conversa
    estado["fala"] = b"ID3-mp3"
    roteiro.append("Funciona no celular de cada funcionário.")
    n = len(enviados)
    postar(meta, audio("5598999990002", "Bia", "midia-1", "v5"))
    assert len(enviados) == n + 1 and enviados[-1][1]["text"]["body"] == "Funciona no celular de cada funcionário."
    estado["fala"] = b"OggS\x00voz-de-teste"

    # cliente que já usa o sistema: o robô sabe quem é e um problema vira tarefa para o time
    with SessionLocal() as db:
        dono = db.scalar(select(Usuario).where(Usuario.email == "dono@plataforma.com"))
        db.add(Usuario(empresa_id=dono.empresa_id, nome="Carla Souza", celular="98999990005", papel="gestor"))
        db.commit()
    estado["transcricao"] = "O ponto da Júlia de ontem não apareceu no fechamento"
    roteiro.append("Entendi, Carla. Vou passar para o time agora. [HUMANO]\n"
                   "#problema Batida da Júlia de ontem não aparece no fechamento")
    postar(meta, audio("5598999990005", "Carla", "midia-2", "c1"))
    s = pedidos[-1]["system"]
    assert "QUEM ESTÁ ESCREVENDO" in s and "Carla, gestor da empresa Plataforma" in s and "Ajuste de ponto" in s
    assert "#problema" not in json.dumps(enviados[-2:])
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.telefone == "98999990005"))
        assert ld.robo_pausado
        titulos = list(db.scalars(select(LeadTarefa.titulo).where(LeadTarefa.lead_id == ld.id)))
        assert "Problema no sistema: Batida da Júlia de ontem não aparece no fechamento" in titulos, titulos

    # áudio que não deu para baixar: fica "[áudio]" e o robô pede para repetir (não inventa)
    roteiro.append("Não consegui ouvir seu áudio. Pode mandar de novo ou escrever?")
    postar(meta, audio("5598999990007", "Rui", "midia-sumiu", "r1"))
    with SessionLocal() as db:
        assert db.scalar(select(WhatsMensagem).where(WhatsMensagem.mid == "r1")).texto == "[áudio]"
    assert enviados[-1][1]["type"] == "text"
    print("ok: áudio no WhatsApp")


if __name__ == "__main__":
    main()
