"""Áudio no WhatsApp da plataforma: ouvir o áudio que a pessoa manda e responder com voz.

- Áudio que chega: a Meta manda só o id da mídia. Baixamos o arquivo (.ogg/opus) com o WA_TOKEN, mandamos para
  a transcrição da ElevenLabs e gravamos na conversa como "[áudio] <o que a pessoa disse>". O arquivo não é
  guardado: fica só o texto, que o time lê no CRM e o robô usa para responder.
- Resposta em voz: se a última mensagem da pessoa foi áudio e a voz estiver ligada (Master -> Rastreamento),
  o texto do robô vira fala na ElevenLabs, já em Ogg/Opus, sobe para a Meta e vai como mensagem de voz
  (o microfone, não um arquivo de música). Qualquer falha no caminho: a resposta vai em texto.

Configuração, só no .env do servidor:
  ELEVENLABS_API_KEY  chave da ElevenLabs (elevenlabs.io -> Developers -> API Keys)
  VOZ_ID              id da voz escolhida para a marca (Voices -> a voz -> ID). Sem ele não fala, só transcreve.
  VOZ_MODELO          (opcional) modelo de fala, padrão eleven_multilingual_v2
  VOZ_TRANSCRICAO     (opcional) modelo de transcrição, padrão scribe_v2
"""
import logging
import os

import httpx
from sqlalchemy.orm import Session

from .db import Config

log = logging.getLogger("whatsapp_voz")
ELEVEN = "https://api.elevenlabs.io/v1"
CHAVE = "wa_voz"
MAX_AUDIO = 16 * 1024 * 1024        # limite da Meta para áudio
LONGO = 1500                        # caracteres transcritos (~2 minutos de fala): acima disso responde em texto
MAX_FALA = 600                      # caracteres que viram voz; resposta maior vai em texto


def _env(nome: str, padrao: str = "") -> str:
    return os.environ.get(nome, "").strip() or padrao


def transcreve() -> bool:
    return bool(_env("ELEVENLABS_API_KEY"))


def fala() -> bool:
    return bool(_env("ELEVENLABS_API_KEY") and _env("VOZ_ID"))


def ligado(db: Session) -> bool:
    c = db.get(Config, CHAVE)
    return bool(c and c.valor == "1")


def ligar(db: Session, sim: bool) -> None:
    c = db.get(Config, CHAVE) or Config(chave=CHAVE, valor="")
    c.valor = "1" if sim else ""
    db.merge(c)


# ------------------------------------------------------------------ Meta
def baixar(midia_id: str) -> tuple[bytes, str]:
    """Baixa a mídia recebida. Devolve (bytes, tipo) ou (b"", "") se não der."""
    from . import whatsapp
    token = whatsapp._env("WA_TOKEN")
    if not (midia_id and token):
        return b"", ""
    cab = {"Authorization": f"Bearer {token}"}
    try:
        r = httpx.get(f"{whatsapp.API}/{midia_id}", headers=cab, timeout=15)
        d = r.json() if r.content else {}
        url = d.get("url") if r.status_code == 200 else ""
        if not url or int(d.get("file_size") or 0) > MAX_AUDIO:
            log.warning("WhatsApp: mídia %s sem endereço ou grande demais (%s)", midia_id, r.status_code)
            return b"", ""
        a = httpx.get(url, headers=cab, timeout=30)
        if a.status_code != 200 or not a.content or len(a.content) > MAX_AUDIO:
            return b"", ""
        tipo = (d.get("mime_type") or a.headers.get("content-type") or "audio/ogg").split(";")[0].strip()
        return a.content, tipo
    except (httpx.HTTPError, ValueError) as e:
        log.warning("WhatsApp: erro ao baixar a mídia %s: %s", midia_id, e)
        return b"", ""


def subir(audio: bytes) -> str:
    """Sobe o áudio para a Meta e devolve o id da mídia (ou "")."""
    from . import whatsapp
    try:
        r = httpx.post(f"{whatsapp.API}/{whatsapp._env('WA_PHONE_ID')}/media", timeout=30,
                       headers={"Authorization": f"Bearer {whatsapp._env('WA_TOKEN')}"},
                       data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                       files={"file": ("resposta.ogg", audio, "audio/ogg")})
        d = r.json() if r.content else {}
        if r.status_code == 200 and d.get("id"):
            return str(d["id"])
        log.warning("WhatsApp: subida do áudio recusada (%s): %s", r.status_code, d.get("error"))
    except (httpx.HTTPError, ValueError) as e:
        log.warning("WhatsApp: erro ao subir o áudio: %s", e)
    return ""


# ------------------------------------------------------------------ ElevenLabs
def transcrever(audio: bytes, tipo: str = "audio/ogg") -> str:
    """Áudio -> texto em português. "" se não der."""
    if not (audio and transcreve()):
        return ""
    ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
           "audio/amr": "amr"}.get(tipo, "ogg")
    try:
        r = httpx.post(f"{ELEVEN}/speech-to-text", timeout=60,
                       headers={"xi-api-key": _env("ELEVENLABS_API_KEY")},
                       data={"model_id": _env("VOZ_TRANSCRICAO", "scribe_v2"), "language_code": "por",
                             "tag_audio_events": "false"},
                       files={"file": (f"audio.{ext}", audio, tipo)})
        if r.status_code != 200:
            log.warning("ElevenLabs: transcrição recusada (%s): %s", r.status_code, r.text[:200])
            return ""
        return " ".join(str((r.json() or {}).get("text") or "").split())[:3000]
    except (httpx.HTTPError, ValueError) as e:
        log.warning("ElevenLabs: erro na transcrição: %s", e)
        return ""


def falar(texto: str) -> bytes:
    """Texto -> mensagem de voz (Ogg/Opus, mono, 48 kHz). b"" se não der."""
    texto = (texto or "").strip()
    if not (texto and fala()) or len(texto) > MAX_FALA:
        return b""
    try:
        r = httpx.post(f"{ELEVEN}/text-to-speech/{_env('VOZ_ID')}", timeout=60,
                       params={"output_format": "opus_48000_64"},
                       headers={"xi-api-key": _env("ELEVENLABS_API_KEY")},
                       json={"text": texto, "model_id": _env("VOZ_MODELO", "eleven_multilingual_v2")})
        if r.status_code != 200 or not r.content:
            log.warning("ElevenLabs: fala recusada (%s): %s", r.status_code, r.text[:200])
            return b""
        if not r.content.startswith(b"OggS"):
            log.warning("ElevenLabs: a fala não veio em Ogg; vai em texto")
            return b""
        return r.content
    except httpx.HTTPError as e:
        log.warning("ElevenLabs: erro na fala: %s", e)
        return b""


# ------------------------------------------------------------------ juntando
def ouvir(midia_id: str) -> str:
    """Baixa e transcreve. "" = não deu para ouvir."""
    audio, tipo = baixar(midia_id)
    return transcrever(audio, tipo) if audio else ""


def transcrever_pendentes(ids: list[int]) -> None:
    """Troca o "[áudio]" das mensagens recebidas pelo que a pessoa disse."""
    from .db import SessionLocal, WhatsMensagem
    if not (ids and transcreve()):
        return
    with SessionLocal() as db:
        for i in ids:
            m = db.get(WhatsMensagem, i)
            if not m or not m.midia_id:
                continue
            texto = ouvir(m.midia_id)
            if texto:
                legenda = m.texto.removeprefix("[áudio]").strip()
                m.texto = ("[áudio] " + texto + (f" ({legenda})" if legenda else ""))[:4096]
            m.midia_id = ""
            db.commit()


def responder_com_voz(m) -> bool:
    """A resposta a esta mensagem deve ir em voz? (áudio curto, entendido, e voz ligada)"""
    texto = (m.texto or "").removeprefix("[áudio]").strip()
    return bool(m.audio and m.entrada and texto and len(texto) <= LONGO and fala())


def responder_em_voz(para: str, texto: str) -> tuple[str, str]:
    """Fala o texto e manda como mensagem de voz. Devolve (id da mensagem, erro); erro = mande em texto."""
    from . import whatsapp
    audio = falar(texto)
    if not audio:
        return "", "sem_voz"
    mid = subir(audio)
    if not mid:
        return "", "sem_subida"
    return whatsapp.enviar_api(para, {"type": "audio", "audio": {"id": mid, "voice": True}})
