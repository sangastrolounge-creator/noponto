"""Página de vendas editável. Rodar da raiz: .venv/Scripts/python tests/test_vendas.py"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "ADMIN_EMAIL": "master@a.com", "SEM_AGENDADOR": "1"})
for k in ("ASAAS_API_KEY", "PUBLIC_URL", "MIDIA_DIR"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

from app import vendas  # noqa: E402
from app.main import app  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def cadastrar(empresa, email):
    cli = TestClient(app, follow_redirects=False)
    r = cli.post("/cadastro", data={"csrf": csrf(cli, "/cadastro"), "aceito": "1", "empresa": empresa, "nome": "Fulano",
                                    "email": email, "senha": "segredo123", "plano": "1"})
    assert r.status_code == 303, r.text
    return cli


def formulario(c: dict, token: str, acao: str) -> list:
    """Monta os campos como o editor envia."""
    campos = [("csrf", token), ("acao", acao)]
    for sec, esq in vendas.ESQUEMA.items():
        s = c[sec]
        if s.get("ativo"):
            campos.append((f"{sec}__ativo", "1"))
        for campo in esq.get("campos", {}):
            campos.append((f"{sec}__{campo}", s.get(campo, "")))
        for campo in esq.get("linhas", ()):
            campos.append((f"{sec}__{campo}", "\n".join(s.get(campo, []))))
        for m in esq.get("midia", ()):
            v = s.get(m, "")
            if v.startswith("/midia/"):
                campos.append((f"{sec}__{m}_atual", v))
            elif v and m == "video":
                campos.append((f"{sec}__video_link", v))
        for i, item in enumerate(s.get("itens", [])):
            ch = f"k{i}"
            campos.append((f"{sec}__chave", ch))
            for k, v in item.items():
                if k == "foto":
                    if v:
                        campos.append((f"{sec}__foto__{ch}_atual", v))
                else:
                    campos.append((f"{sec}__{k}__{ch}", v))
    return campos


def enviar(cli, campos, files=None):
    """O httpx só monta multipart com dicionário: chaves repetidas viram lista."""
    d = {}
    for k, v in campos:
        d.setdefault(k, []).append(v)
    d = {k: (v if len(v) > 1 else v[0]) for k, v in d.items()}
    # sem arquivo real, manda um campo de arquivo vazio para forçar multipart, como o navegador
    return cli.post("/master/pagina", data=d, files=files or [("topo__imagem", ("", b"", "application/octet-stream"))])


def publicado():
    from app.db import SessionLocal
    with SessionLocal() as db:
        return vendas.conteudo_publicado(db)


def main():
    assert vendas.tipo_do_arquivo(PNG) == "png" and vendas.tipo_do_arquivo(MP4) == "mp4"
    assert vendas.tipo_do_arquivo(b"<html><script>") is None
    assert vendas.video_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")["src"].endswith("/embed/dQw4w9WgXcQ")
    assert vendas.video_info("https://youtu.be/dQw4w9WgXcQ?t=3")["tipo"] == "embutido"
    assert vendas.video_info("https://vimeo.com/123456789")["src"] == "https://player.vimeo.com/video/123456789"
    assert vendas.video_info("https://malicioso.example/x.mp4") is None
    assert vendas.video_info("javascript:alert(1)//youtu.be/dQw4w9WgXcQ") is None

    from sqlalchemy import select
    from app.db import Plano, SessionLocal
    with SessionLocal() as db:   # os planos de exemplo nascem fora de venda
        for p in db.scalars(select(Plano)):
            p.ativo = True
        db.commit()
    m = cadastrar("Plataforma", "master@a.com")
    b = cadastrar("Clínica B", "dono@b.com")
    anon = TestClient(app, follow_redirects=False)
    simples = anon.get("/").text
    assert "v-heroi" not in simples and "Essencial" in simples, "antes de publicar: página simples"
    assert b.get("/master/pagina").headers["location"] == "/painel"
    assert b.get("/master/pagina/previa").headers["location"] == "/painel"
    assert anon.get("/master/pagina").status_code == 303

    ed = m.get("/master/pagina")
    assert ed.status_code == 200 and "Ainda não publicada" in ed.text and "A dor do cliente" in ed.text
    tm = csrf(m, "/master/pagina")
    assert b.post("/master/pagina", data={"csrf": csrf(b, "/painel/config"), "acao": "publicar"}).headers["location"] == "/painel"

    # --- salvar rascunho com textos, imagem, vídeo por link e depoimento com foto
    c = vendas.conteudo_padrao()
    c["topo"]["titulo"] = "Título novo <script>alert(1)</script>"
    c["demo"].update(ativo=True, video="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    c["depoimentos"].update(ativo=True, itens=[
        {"nome": "Maria Souza", "empresa": "Padaria Central", "texto": "Acabou a planilha de escala.", "nota": "5", "foto": ""}])
    c["faq"]["itens"].append({"pergunta": "Tem fidelidade?", "resposta": "Não."})
    r = enviar(m, formulario(c, tm, "salvar"),
               [("topo__imagem", ("capa.png", PNG, "image/png")),
                      ("depoimentos__foto__k0", ("maria.png", PNG, "image/png"))])
    assert r.status_code == 303 and r.headers["location"] == "/master/pagina?lang=pt", (r.status_code, r.text[:600])
    ed = m.get("/master/pagina").text
    assert "Rascunho salvo" in ed and "Ainda não publicada" in ed
    assert len(re.findall(r'src="/midia/[a-f0-9]{24}\.png"', ed)) == 2
    assert "Maria Souza" in ed and "Tem fidelidade?" in ed
    assert "v-heroi" not in anon.get("/").text, "rascunho não vai ao ar"
    previa = m.get("/master/pagina/previa").text
    assert "Prévia do rascunho" in previa and "Título novo &lt;script&gt;" in previa
    assert "<script>alert(1)" not in previa

    # --- publicar
    rasc = vendas._carregar(__import__("app.db", fromlist=["x"]).SessionLocal().get(vendas.PaginaVendas, 1).rascunho)
    r = enviar(m, formulario(rasc, tm, "publicar"))
    assert "publicada" in m.get("/master/pagina").text
    pag = anon.get("/").text
    assert "v-heroi" in pag and "Título novo &lt;script&gt;" in pag and "Prévia do rascunho" not in pag
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ" in pag
    assert "Maria Souza" in pag and "Padaria Central" in pag and "Tem fidelidade?" in pag
    assert "Essencial" in pag and "R$ 49,90" in pag and 'href="/cadastro' in pag
    imgs = re.findall(r'src="(/midia/[a-f0-9]{24}\.png)"', pag)
    assert len(imgs) == 2, "imagens mantidas ao salvar de novo sem reenviar"
    r = anon.get(imgs[0])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert anon.get("/midia/../turno.db").status_code == 404

    # --- arquivo que não é imagem é recusado; vídeo enviado funciona; link inválido é recusado
    c2 = publicado()
    r = enviar(m, formulario(c2, tm, "publicar"),
               [("solucao__imagem", ("falso.png", b"<html><script>alert(1)</script>", "image/png")),
                      ("solucao__video", ("demo.mp4", MP4, "video/mp4"))])
    ed = m.get("/master/pagina").text
    assert "não é um arquivo aceito" in ed and "No ar, com alterações não publicadas" in ed, "erro salva rascunho mas não publica"
    assert not publicado()["solucao"]["video"]
    assert re.search(r'<video src="/midia/[a-f0-9]{24}\.mp4"', ed)
    # --- vídeo grande em pedaços (o proxy corta envios longos de uma vez só)
    grande = MP4 + bytes(range(256)) * 40000          # ~10 MB: 2 pedaços
    PED = 8 * 1024 * 1024
    partes = [grande[:PED], grande[PED:]]
    url = "/master/midia/pedaco?envio=" + "ab" * 12
    assert m.post(url + "&i=0&n=2", content=partes[0], headers={"content-type": "application/octet-stream"}).status_code == 403  # sem CSRF
    h = {"X-CSRF": tm, "content-type": "application/octet-stream"}
    assert m.post("/master/midia/pedaco?envio=" + "cd" * 12 + "&i=0&n=1", content=b"<html>nao sou video</html>" * 10, headers=h).status_code == 415
    assert m.post("/master/midia/pedaco?envio=" + "ef" * 12 + "&i=1&n=2", content=partes[1], headers=h).status_code == 409
    assert m.post("/master/midia/pedaco?envio=../x&i=0&n=1", content=partes[0], headers=h).status_code in (400, 422)
    assert anon.post(url + "&i=0&n=2", content=partes[0], headers=h).status_code in (303, 403)
    assert m.post(url + "&i=0&n=2", content=partes[0], headers=h).json() == {"ok": True}
    fim = m.post(url + "&i=1&n=2", content=partes[1], headers=h).json()
    assert re.fullmatch(r"/midia/[a-f0-9]{24}\.mp4", fim["url"]), fim
    assert anon.get(fim["url"]).content == grande
    c4 = publicado()
    form4 = formulario(c4, tm, "salvar") + [("topo__video_enviado", fim["url"])]
    enviar(m, form4)
    assert fim["url"] in m.get("/master/pagina").text
    form5 = formulario(c4, tm, "salvar") + [("topo__video_enviado", "/midia/" + "0" * 24 + ".mp4")]   # não existe
    enviar(m, form5)
    assert "0" * 24 not in m.get("/master/pagina").text
    c3 = publicado()
    c3["demo"]["video"] = "https://malicioso.example/v"
    enviar(m, formulario(c3, tm, "salvar"))
    assert "Link de vídeo não reconhecido" in m.get("/master/pagina").text

    # --- descartar volta ao publicado; caminho forjado de mídia é ignorado
    m.post("/master/pagina", data={"csrf": tm, "acao": "descartar"})
    assert "No ar desde" in m.get("/master/pagina").text
    c4 = publicado()
    campos = formulario(c4, tm, "publicar") + [("garantia__titulo", "x")]
    campos = [(k, v) for k, v in campos if k != "topo__imagem_atual"] + [("topo__imagem_atual", "/static/style.css")]
    enviar(m, campos)
    assert publicado()["topo"]["imagem"] == ""

    # --- esconder seção, remover mídia e limpeza de arquivos sem uso
    c5 = publicado()
    c5["dor"]["ativo"] = False
    campos = formulario(c5, tm, "publicar") + [("depoimentos__foto__k0_remover", "1")]
    enviar(m, campos)
    pag = anon.get("/").text
    assert "v-escura" not in pag and not re.findall(r'src="/midia/', pag)
    for f in vendas.MIDIA_DIR.iterdir():
        os.utime(f, (time.time() - 7200, time.time() - 7200))
    enviar(m, formulario(publicado(), tm, "publicar"))
    assert not [f for f in vendas.MIDIA_DIR.iterdir() if f.is_file()], "arquivos sem uso são apagados"

    # --- cadastro fechado: o botão leva ao WhatsApp do suporte
    tp = csrf(m, "/master/plataforma")
    m.post("/master/plataforma", data={"csrf": tp, "nome": "Turno+", "dias_teste": "7",
                                       "whatsapp": "98981000000"})
    pag = anon.get("/").text
    assert 'href="https://wa.me/5598981000000"' in pag and 'href="/cadastro' not in pag

    # --- tirar do ar
    m.post("/master/pagina", data={"csrf": tm, "acao": "tirar"})
    assert "v-heroi" not in anon.get("/").text and "Ainda não publicada" in m.get("/master/pagina").text
    print("OK: testes da página de vendas passaram")


if __name__ == "__main__":
    main()
