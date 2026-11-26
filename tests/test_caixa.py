"""Caixa de e-mail no Master (IMAP/SMTP simulados). Rodar da raiz: python tests/test_caixa.py"""
import os
import re
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import caixa, push  # noqa: E402
from app.db import CaixaEmail, Config, Lead, LeadAtividade, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

CAIXA = []          # e-mails "no servidor": (uid, bruto, lido)
ENVIADOS, PUSH, LIDOS = [], [], []


def bruto(uid, de, assunto, texto, html="", anexo=None, msgid=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = de, "Data Worke <contato@dataworke.com.br>", assunto
    m["Date"] = "Tue, 24 Nov 2026 14:00:00 -0300"
    m["Message-ID"] = msgid or f"<m{uid}@cliente.com>"
    m.set_content(texto)
    if html:
        m.add_alternative(html, subtype="html")
    if anexo:
        m.add_attachment(anexo[1], maintype="application", subtype="pdf", filename=anexo[0])
    return m.as_bytes()


class _Con:
    def logout(self):
        pass


def _conectar(d):
    if d["caixa_senha"] != "certa":
        raise OSError("login falhou")
    return _Con()


def _buscar(d, ultimo, validade):
    return "777", [(u, b, lido) for u, b, lido in CAIXA if u > ultimo]


caixa._conectar = _conectar
caixa._buscar = _buscar
caixa._baixar = lambda d, uid: [b for u, b, _ in CAIXA if u == uid][0]
caixa._enviar = lambda d, msg: ENVIADOS.append(msg)
caixa._marcar_lido = lambda d, uid, lido=True: LIDOS.append((uid, lido))
push.avisar_usuarios = lambda lembretes: PUSH.extend(lembretes)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def equipe(m, nome, email, areas):
    dados = {"csrf": csrf(m, "/master/equipe"), "nome": nome, "email": email}
    dados.update({f"area_{a}": "1" for a in areas})
    m.post("/master/equipe", data=dados)
    with SessionLocal() as db:
        token = db.scalar(select(Usuario).where(Usuario.email == email)).convite_token
    c = TestClient(app, follow_redirects=False)
    c.post(f"/convite/{token}", data={"csrf": csrf(c, f"/convite/{token}"), "senha": "segredo9", "consentimento": "1"})
    c = TestClient(app, follow_redirects=False)
    r = c.post("/entrar", data={"csrf": csrf(c, "/entrar"), "login": email, "senha": "segredo9"})
    return c, r


def main():
    m = TestClient(app, follow_redirects=False)
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    m.post("/master/crm/novo", data={"csrf": csrf(m, "/master/crm"), "nome": "Lia Cliente", "email": "lia@cliente.com"})
    with SessionLocal() as db:
        lid = db.scalar(select(Lead).where(Lead.email == "lia@cliente.com")).id

    # sem caixa ligada: o dono vai para a configuração
    assert m.get("/master/caixa").headers["location"] == "/master/caixa/config"
    CAIXA.append((10, bruto(10, "Lia <lia@cliente.com>", "Dúvida sobre o plano", "Vocês fazem banco de horas?",
                            html="<p>Vocês fazem <b>banco de horas</b>?</p><script>alert(1)</script>"), False))
    CAIXA.append((11, bruto(11, "Rui <rui@outra.com>", "Proposta assinada", "Segue a proposta.",
                            anexo=("proposta.pdf", b"%PDF-1.4 conteudo")), True))
    tok = csrf(m, "/master/caixa/config")
    r = m.post("/master/caixa/config", data={"csrf": tok, "usuario": "contato@dataworke.com.br", "senha": "errada",
                                             "host": "imap.hostinger.com", "porta": "993",
                                             "smtp_host": "smtp.hostinger.com", "smtp_porta": "465"})
    with SessionLocal() as db:
        assert not db.get(Config, "caixa_senha")                       # senha errada não fica guardada
    m.post("/master/caixa/config", data={"csrf": tok, "usuario": "contato@dataworke.com.br", "senha": "certa",
                                         "host": "imap.hostinger.com", "porta": "993",
                                         "smtp_host": "smtp.hostinger.com", "smtp_porta": "465"})
    assert "certa" not in m.get("/master/caixa/config").text           # a senha nunca volta para a tela
    with SessionLocal() as db:
        e1 = db.scalar(select(CaixaEmail).where(CaixaEmail.uid == 10))
        e2 = db.scalar(select(CaixaEmail).where(CaixaEmail.uid == 11))
        assert e1 and e2 and not e1.lido and e2.lido and e1.lead_id == lid and e2.lead_id is None
        assert "banco de horas" in e1.texto and "proposta.pdf" in e2.anexos
        assert any("Dúvida sobre o plano" in a.texto for a in
                   db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == lid, LeadAtividade.tipo == "email")))
    assert not PUSH                                                    # primeira carga não dispara avisos
    lista = m.get("/master/caixa").text
    assert "Dúvida sobre o plano" in lista and "Proposta assinada" in lista and "/master/caixa\"" in lista
    assert re.search(r'E-mail <b class="menu-selo">1</b>', lista)  # contador de não lidos no menu
    assert "Proposta" in m.get("/master/caixa?q=proposta").text and "Dúvida" not in m.get("/master/caixa?q=proposta").text

    # ler: marca como lido (aqui e no servidor); HTML isolado, sem scripts
    pag = m.get(f"/master/caixa/{e1.id}").text
    assert f"/master/caixa/{e1.id}/html" in pag and "Lia Cliente" in pag
    with SessionLocal() as db:
        assert db.get(CaixaEmail, e1.id).lido
    assert (10, True) in LIDOS
    h = m.get(f"/master/caixa/{e1.id}/html")
    assert "default-src 'none'" in h.headers["content-security-policy"] and "banco de horas" in h.text
    a = m.get(f"/master/caixa/{e2.id}")
    n = re.search(rf"/master/caixa/{e2.id}/anexo/(\d+)", a.text).group(1)
    arq = m.get(f"/master/caixa/{e2.id}/anexo/{n}")
    assert arq.content == b"%PDF-1.4 conteudo" and "proposta.pdf" in arq.headers["content-disposition"]

    # responder com anexo: vai com In-Reply-To, entra em Enviados e no histórico do lead
    assert "Re: Dúvida sobre o plano" in m.get(f"/master/caixa/novo?responder={e1.id}").text
    r = m.post("/master/caixa/enviar", data={"csrf": tok, "para": "lia@cliente.com", "cc": "", "responder": str(e1.id),
                                             "assunto": "Re: Dúvida sobre o plano", "texto": "Fazemos sim!"},
               files=[("anexos", ("tabela.txt", b"precos", "text/plain"))])
    assert r.status_code == 303
    env = ENVIADOS[-1]
    assert env["To"] == "lia@cliente.com" and env["In-Reply-To"] == "<m10@cliente.com>"
    assert "Fazemos sim!" in env.get_body(("plain",)).get_content() and "Dono" in env.get_body(("plain",)).get_content()
    assert [p.get_filename() for p in env.iter_attachments()] == ["tabela.txt"]
    with SessionLocal() as db:
        s = db.scalar(select(CaixaEmail).where(CaixaEmail.pasta == "enviados"))
        assert s and s.lead_id == lid
        assert any(a.texto.startswith("E-mail enviado") for a in
                   db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == lid)))
    assert "Re: Dúvida sobre o plano" in m.get("/master/caixa?pasta=enviados").text
    assert m.post("/master/caixa/enviar", data={"csrf": tok, "para": "não é e-mail", "assunto": "x",
                                                "texto": "y"}).status_code == 303 and len(ENVIADOS) == 1
    # encaminhar com os anexos originais
    m.post("/master/caixa/enviar", data={"csrf": tok, "para": "socio@dataworke.com.br", "encaminhar": str(e2.id),
                                         "manter_anexos": "1", "assunto": "Enc: Proposta assinada", "texto": "Veja"})
    assert [p.get_filename() for p in ENVIADOS[-1].iter_attachments()] == ["proposta.pdf"]

    # e-mail novo chega: aviso no celular para quem cuida da caixa
    CAIXA.append((12, bruto(12, "Rui <rui@outra.com>", "Mais uma dúvida", "Tem app?"), False))
    m.post("/master/caixa/atualizar", data={"csrf": tok})
    assert any("Mais uma dúvida" in p[2] for p in PUSH)

    # criar lead a partir do e-mail
    with SessionLocal() as db:
        e3 = db.scalar(select(CaixaEmail).where(CaixaEmail.uid == 12))
    r = m.post(f"/master/caixa/{e3.id}/lead", data={"csrf": tok})
    with SessionLocal() as db:
        novo = db.scalar(select(Lead).where(Lead.email == "rui@outra.com"))
        assert novo and novo.origem == "email" and r.headers["location"] == f"/master/crm/{novo.id}"
        assert db.get(CaixaEmail, e2.id).lead_id == novo.id                 # e-mails antigos dele também
    assert "Mais uma dúvida" in m.get(f"/master/crm/{novo.id}").text

    # acesso: só CRM não vê a caixa; só E-mail vê, mas não configura
    so_crm, _ = equipe(m, "Ana CRM", "ana@plataforma.com", ["crm"])
    assert so_crm.get("/master/caixa").headers["location"] == "/master/crm"
    assert "/master/caixa\"" not in so_crm.get("/master/crm").text
    so_mail, r = equipe(m, "Bia Mail", "bia@plataforma.com", ["email"])
    assert r.headers["location"] == "/master/caixa"
    assert so_mail.get("/master/caixa").status_code == 200
    assert so_mail.get("/master/caixa/config").headers["location"] == "/master/caixa"
    assert so_mail.get("/master/crm").headers["location"] == "/master/caixa"
    print("ok: caixa de e-mail")


if __name__ == "__main__":
    main()
