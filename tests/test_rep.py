"""Portaria 671 (REP-P): ARP com NSR e hash encadeado, cadastro (tipos 2 e 5), eventos (tipo 6),
comprovante PDF assinado, AFD (leiaute 004 + .p7s), AEJ, Atestado Técnico, página pública e Termos.
Rodar da raiz: python tests/test_rep.py"""
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1",
                   "ADMIN_EMAIL": "dono@plataforma.com"})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as banco  # noqa: E402
from app import regras, rep  # noqa: E402
from app.db import DadosRH, Empresa, RegistroRep, SessionLocal, Usuario  # noqa: E402
from app.main import app  # noqa: E402

relogio = {"agora": datetime(2026, 11, 20, 12, 0)}
banco.agora = lambda: relogio["agora"]


def novo():
    return TestClient(app, follow_redirects=False)


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def certificado(nome: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    agora = datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(sujeito).issuer_name(sujeito).public_key(chave.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(agora - timedelta(days=1))
            .not_valid_after(agora + timedelta(days=365))
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(chave, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(b"cert", chave, cert, None,
                                                 serialization.BestAvailableEncryption(b"senha123"))


def main():
    assert rep.crc16_kermit("123456789") == "2189"
    m = novo()
    m.post("/cadastro", data={"csrf": csrf(m, "/cadastro"), "aceito": "1", "empresa": "Plataforma", "nome": "Dono",
                              "email": "dono@plataforma.com", "senha": "segredo1"})
    g = novo()
    g.post("/cadastro", data={"csrf": csrf(g, "/cadastro"), "aceito": "1", "empresa": "Bar Mar", "nome": "Gil",
                              "email": "gil@bar.com", "senha": "segredo1"})
    t = csrf(g, "/painel/portaria671")
    assert "Registro no INPI em andamento" in novo().get("/portaria-671").text
    assert "ainda estão sendo concluídos" in novo().get("/terms").text
    r = g.post("/painel/portaria671", data={"csrf": t, "cpf_cnpj": "11.444.777/0001-61", "rep_cno": "",
                                            "rep_cpf_resp": "529.982.247-25", "rep_local": "Rua A, 10"})
    assert r.status_code == 303
    g.post("/painel/equipe", data={"csrf": t, "nome": "Ana Souza", "celular": "98999990001"})
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Bar Mar"))
        eid = e.id
        assert e.cpf_cnpj == "11444777000161" and e.rep_cpf_resp == "52998224725"
        ana = db.scalar(select(Usuario).where(Usuario.nome == "Ana Souza"))
        aid = ana.id
        dr = db.scalar(select(DadosRH).where(DadosRH.usuario_id == aid))
        if dr:
            dr.cpf = "39053344705"
        else:
            db.add(DadosRH(empresa_id=eid, usuario_id=aid, cpf="39053344705"))
        db.commit()
        regs = list(db.scalars(select(RegistroRep).where(RegistroRep.empresa_id == eid).order_by(RegistroRep.nsr)))
        assert [(x.nsr, x.tipo) for x in regs] == [(1, 2), (2, 5)], [(x.nsr, x.tipo) for x in regs]
        assert json.loads(regs[1].dados)["op"] == "I" and regs[1].cpf == "39053344705"

    # ---- duas marcações: NSR seguido e hash encadeado
    with SessionLocal() as db:
        ana = db.get(Usuario, aid)
        b1, _ = regras.bater(db, ana, None, None, None, aparelho="aparelho-ana-123", ua="teste")
        relogio["agora"] += timedelta(hours=4)
        b2, _ = regras.bater(db, db.get(Usuario, aid), None, None, None, aparelho="aparelho-ana-123", ua="teste")
        m7 = list(db.scalars(select(RegistroRep).where(RegistroRep.empresa_id == eid, RegistroRep.tipo == 7)
                             .order_by(RegistroRep.nsr)))
        assert [x.nsr for x in m7] == [3, 4] and m7[0].batida_id == b1.id and m7[1].batida_id == b2.id
        e = db.get(Empresa, eid)
        h1 = rep._hash7(3, rep.dh(e, m7[0].marcacao_em), "39053344705", rep.dh(e, m7[0].gravado_em), "02", "0", "")
        h2 = rep._hash7(4, rep.dh(e, m7[1].marcacao_em), "39053344705", rep.dh(e, m7[1].gravado_em), "02", "0", h1)
        assert (m7[0].hash, m7[1].hash) == (h1, h2)
        assert rep.dh(e, datetime(2026, 11, 20, 12, 0)) == "2026-11-20T09:00:00-0300"

    # ---- cadastro: renomear (A), desligar (E), mudar a empresa (2); eventos de serviço (6)
    with SessionLocal() as db:
        ana = db.get(Usuario, aid)
        ana.nome = "Ana Souza Lima"
        db.commit()
        ana = db.get(Usuario, aid)
        ana.ativo = False
        db.commit()
        ana = db.get(Usuario, aid)
        ana.ativo = True
        db.commit()
        db.get(Empresa, eid).nome = "Bar do Mar"
        db.commit()
    rep.evento_servico("07")
    with SessionLocal() as db:
        regs = list(db.scalars(select(RegistroRep).where(RegistroRep.empresa_id == eid).order_by(RegistroRep.nsr)))
        tipos = [(x.tipo, json.loads(x.dados).get("op") or json.loads(x.dados).get("evento")) for x in regs[4:]]
        assert tipos == [(5, "A"), (5, "E"), (5, "I"), (2, None), (6, "07")], tipos
        assert [x.nsr for x in regs] == list(range(1, len(regs) + 1))

    # ---- comprovante (sem certificado ainda: PDF sem assinatura)
    r = g.get("/app/comprovante/3.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and b"Comprovante de Registro de Ponto" in r.content
    assert b"/ByteRange" not in r.content
    assert g.get("/app/comprovante/999.pdf").status_code == 404

    # ---- AFD sem assinatura
    ini = "2026-11-01"
    r = g.get(f"/painel/portaria671/afd?ini={ini}&fim=2026-11-20")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(z.namelist()) == 1
    linhas = z.read(z.namelist()[0]).decode("latin-1").split("\r\n")
    assert linhas[-1] == ""
    linhas = linhas[:-1]
    cab = linhas[0]
    assert len(cab) == 302 and cab[:10] == "0000000001" and rep.crc16_kermit(cab[:-4]) == cab[-4:]
    assert cab[250:253] == "004" and cab[11:25] == "11444777000161"
    t7 = [x for x in linhas if x[9:10] == "7"]
    assert len(t7) == 2 and all(len(x) == 137 for x in t7) and t7[0][34:46] == "039053344705"
    t5 = [x for x in linhas if x[9:10] == "5" and not x.startswith("999")]
    assert all(len(x) == 118 and rep.crc16_kermit(x[:-4]) == x[-4:] for x in t5)
    t2 = [x for x in linhas if x[9:10] == "2" and not x.startswith("999")]
    assert all(len(x) == 331 for x in t2)
    trailer = linhas[-2]
    assert trailer.startswith("999999999") and len(trailer) == 64 and trailer[-1] == "9"
    assert int(trailer[54:63]) == 2 and int(trailer[36:45]) == 4      # 2 marcações, 4 cadastros
    assert linhas[-1].rstrip() == "ASSINATURA_DIGITAL_EM_ARQUIVO_P7S" and len(linhas[-1]) == 100

    # ---- AEJ
    r = g.get(f"/painel/portaria671/aej?ini={ini}&fim=2026-11-20")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    aej = z.read(z.namelist()[0]).decode("latin-1").split("\r\n")
    tipos = [x.split("|")[0] for x in aej if x and not x.startswith("ASSINATURA")]
    assert tipos[0] == "01" and "02" in tipos and "03" in tipos and tipos.count("05") == 2 and tipos[-2:] == ["08", "99"], tipos
    marc = [x.split("|") for x in aej if x.startswith("05|")]
    assert marc[0][4] == "E" and marc[0][6] == "O" and marc[1][4] == "S"

    # ---- master: dados do desenvolvedor e certificados
    tm = csrf(m, "/master/portaria671")
    m.post("/master/portaria671", data={"csrf": tm, "programa": "Data Worke", "versao": "1.0", "inpi": "BR 51 2026 000123-4",
                                        "dev_doc": "11.222.333/0001-81", "dev_razao": "Data Worke Brasil Ltda",
                                        "dev_email": "contato@dataworke.com.br", "dev_endereco": "São Luís - MA",
                                        "legal_nome": "Dono da Silva", "legal_cpf": "529.982.247-25",
                                        "tec_nome": "Dono da Silva", "tec_cpf": "529.982.247-25"})
    r = m.post("/master/portaria671/certificado", data={"csrf": tm, "tipo": "empresa", "senha": "errada"},
               files={"arquivo": ("cert.pfx", certificado("DATA WORKE BRASIL LTDA:11222333000181"), "application/x-pkcs12")})
    assert "Não consegui abrir o certificado" in m.get("/master/portaria671").text
    for tipo, nome in (("empresa", "DATA WORKE BRASIL LTDA:11222333000181"), ("legal", "DONO DA SILVA:52998224725")):
        m.post("/master/portaria671/certificado", data={"csrf": tm, "tipo": tipo, "senha": "senha123"},
               files={"arquivo": ("cert.pfx", certificado(nome), "application/x-pkcs12")})
    t = m.get("/master/portaria671").text
    assert "Tudo pronto" in t and "DATA WORKE BRASIL LTDA" in t
    assert "REP-P registrado no INPI · nº 5120260001234" in novo().get("/portaria-671").text
    assert "registrado no INPI sob o nº 5120260001234" in novo().get("/terms").text

    # ---- comprovante assinado (PAdES), AFD com .p7s (CAdES destacado), atestado assinado
    r = g.get("/app/comprovante/4.pdf")
    assert b"/ByteRange" in r.content and b"ETSI.CAdES.detached" in r.content
    r = g.get(f"/painel/portaria671/afd?ini={ini}&fim=2026-11-20")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    nomes = sorted(z.namelist())
    assert nomes[0].startswith("AFD5120260001234") and nomes[0].endswith("REP_P.txt") and nomes[1] == nomes[0] + ".p7s"
    from asn1crypto import cms
    info = cms.ContentInfo.load(z.read(nomes[1]))
    assert info["content_type"].native == "signed_data"
    assert info["content"]["encap_content_info"]["content"].native is None     # destacada (detached)
    r = g.get("/painel/portaria671/atestado.pdf")
    assert r.status_code == 200 and b"/ByteRange" in r.content and b"ATESTADO T" in r.content
    assert "Tudo pronto" not in g.get("/painel/portaria671").text and "registrado no INPI como REP-P" in g.get("/painel/portaria671").text
    print("OK: Portaria 671 (ARP, NSR, hash, AFD, AEJ, comprovante, atestado, assinaturas) passou")


if __name__ == "__main__":
    main()
