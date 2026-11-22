"""REP-P e Programa de Tratamento de Registro de Ponto (Portaria MTP nº 671/2021).

- ARP (armazenamento de registro de ponto): tabela rep_registros, nunca alterada nem apagada,
  com NSR sequencial por empresa e, nas marcações (tipo 7), hash SHA-256 encadeado.
- Comprovante de Registro de Ponto do Trabalhador (PDF, assinatura PAdES do desenvolvedor),
  disponível ao trabalhador logo depois de cada marcação.
- AFD (Arquivo Fonte de Dados, leiaute 004, REP-P) e AEJ (Arquivo Eletrônico de Jornada, leiaute
  002), com a assinatura CAdES em arquivo .p7s destacado.
- Atestado Técnico e Termo de Responsabilidade (PDF, PAdES dos responsáveis legal e técnico).

Os dados do desenvolvedor (número no INPI, CNPJ, responsáveis) e os certificados ICP-Brasil A1
ficam no Painel Master → Portaria 671. Sem certificado os arquivos saem sem assinatura, e a tela
avisa.
"""
import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from . import db as banco
from . import regras
from .core import (Redirecionar, admin_logado, avisar, exigir_csrf, gestor_logado, ir, log, pode, restrito,
                   render, usuario_logado)
from .db import (Batida, Config, DadosRH, Empresa, RegistroRep, SessionLocal, Turno, Usuario,
                 get_db)

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
PASTA_CERT = banco.PASTA_DADOS / "certificados"
CERTS = {"empresa": "e-CNPJ do desenvolvedor (assina comprovantes, AFD e AEJ)",
         "legal": "e-CPF do responsável legal (assina o Atestado Técnico)",
         "tecnico": "e-CPF do responsável técnico (assina o Atestado Técnico)"}
CAMPOS_CONFIG = ("programa", "versao", "inpi", "dev_doc", "dev_razao", "dev_email", "dev_endereco",
                 "legal_nome", "legal_cpf", "tec_nome", "tec_cpf")
COLETOR_WEB, COLETOR_DISPOSITIVO = "02", "04"   # navegador; tablet do totem


def digitos(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


# ------------------------------------------------------------------ configuração do desenvolvedor
def config(db: Session) -> dict:
    c = db.get(Config, "rep")
    dados = json.loads(c.valor) if c and c.valor else {}
    base = {k: "" for k in CAMPOS_CONFIG}
    base.update({"programa": "Data Worke", "versao": "1.0"})
    base.update({k: v for k, v in dados.items() if k in CAMPOS_CONFIG})
    return base


def _senha(db: Session, tipo: str) -> str:
    c = db.get(Config, f"rep_senha_{tipo}")
    return c.valor if c else ""


def _carregar(db: Session, tipo: str):
    """Assinador do pyHanko a partir do .pfx guardado (ou None)."""
    arq = PASTA_CERT / f"{tipo}.pfx"
    if not arq.is_file():
        return None
    try:
        from pyhanko.sign import signers
        return signers.SimpleSigner.load_pkcs12(str(arq), passphrase=_senha(db, tipo).encode() or None)
    except Exception:  # noqa: BLE001
        log.exception("Certificado %s não carregou", tipo)
        return None


def info_certificado(db: Session, tipo: str) -> dict | None:
    s = _carregar(db, tipo)
    if not s:
        return None
    cert = s.signing_cert
    return {"nome": cert.subject.human_friendly, "valido_ate": cert.not_valid_after.date(),
            "vencido": cert.not_valid_after < datetime.now(timezone.utc)}


def situacao(db: Session) -> dict:
    c = config(db)
    certs = {t: info_certificado(db, t) for t in CERTS}
    faltam = []
    if not digitos(c["inpi"]):
        faltam.append("número do registro do programa no INPI")
    if not digitos(c["dev_doc"]) or not c["dev_razao"]:
        faltam.append("CNPJ e razão social do desenvolvedor")
    if not certs["empresa"] or certs["empresa"]["vencido"]:
        faltam.append("certificado e-CNPJ A1 do desenvolvedor")
    if not c["legal_nome"] or not digitos(c["legal_cpf"]) or not certs["legal"]:
        faltam.append("responsável legal (nome, CPF e e-CPF A1)")
    if not c["tec_nome"] or not digitos(c["tec_cpf"]):
        faltam.append("responsável técnico (nome e CPF)")
    return {"config": c, "certs": certs, "faltam": faltam, "completo": not faltam}


def completo() -> bool:
    with SessionLocal() as db:
        return situacao(db)["completo"]


# ------------------------------------------------------------------ assinaturas
def _rodar(coro_ou_valor):
    import asyncio
    import inspect
    return asyncio.run(coro_ou_valor) if inspect.iscoroutine(coro_ou_valor) else coro_ou_valor


def assinar_cades(db: Session, dados: bytes) -> bytes | None:
    """Assinatura CAdES destacada (.p7s) com o e-CNPJ do desenvolvedor."""
    s = _carregar(db, "empresa")
    if not s:
        return None
    if hasattr(s, "async_sign_general_data"):
        cms = _rodar(s.async_sign_general_data(dados, "sha256", detached=True))
    else:
        cms = s.sign_general_data(dados, "sha256", detached=True)
    return cms.dump()


def assinar_pades(db: Session, pdf: bytes, tipos: tuple[str, ...] = ("empresa",)) -> tuple[bytes, bool]:
    """Assina o PDF (PAdES) com cada certificado disponível. Devolve (pdf, assinado?)."""
    assinadores = [(i, s) for i, s in enumerate(_carregar(db, t) for t in tipos) if s]
    if not assinadores:
        return pdf, False
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    assinado = False
    for i, s in assinadores:
        w = IncrementalPdfFileWriter(io.BytesIO(pdf))
        meta = signers.PdfSignatureMetadata(field_name=f"Assinatura{i + 1}",
                                            subfilter=fields.SigSeedSubFilter.PADES)
        saida = signers.sign_pdf(w, meta, signer=s)
        pdf = _rodar(saida).getvalue() if not isinstance(saida, io.BytesIO) else saida.getvalue()
        assinado = True
    return pdf, assinado


# ------------------------------------------------------------------ PDF simples (texto)
def _pdf_txt(s: str) -> str:
    s = s.encode("cp1252", "replace").decode("cp1252")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _quebrar(texto: str, largura: int) -> list[str]:
    linhas = []
    for par in texto.split("\n"):
        atual = ""
        for palavra in par.split(" "):
            if len(atual) + len(palavra) + 1 > largura and atual:
                linhas.append(atual)
                atual = palavra
            else:
                atual = (atual + " " + palavra).strip()
        linhas.append(atual)
    return linhas


def pdf(blocos: list[tuple[str, int, bool]], titulo: str = "") -> bytes:
    """PDF A4 de uma ou mais páginas com texto (Helvetica). blocos: (texto, tamanho, negrito)."""
    largura_pt, altura_pt, margem = 595, 842, 56
    paginas, atual, y = [], [], altura_pt - margem
    for texto, tam, negrito in blocos:
        cabe = int((largura_pt - 2 * margem) / (tam * 0.5))
        for linha in (_quebrar(texto, cabe) if texto else [""]):
            if y - tam * 1.45 < margem:
                paginas.append(atual)
                atual, y = [], altura_pt - margem
            y -= tam * 1.45
            atual.append(f"BT /{'F2' if negrito else 'F1'} {tam} Tf {margem} {y:.1f} Td ({_pdf_txt(linha)}) Tj ET")
    paginas.append(atual)
    objs: list[bytes] = []
    fontes = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
              b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    n_pag = len(paginas)
    # 1 catálogo, 2 páginas, 3-4 fontes, depois (página, conteúdo) por página, por fim info
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n_pag))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pag} >>".encode())
    objs.extend(fontes)
    for i, linhas in enumerate(paginas):
        conteudo = "\n".join(linhas).encode("cp1252", "replace")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {largura_pt} {altura_pt}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(conteudo) + conteudo + b"\nendstream")
    objs.append(f"<< /Title ({_pdf_txt(titulo)}) /Producer (Data Worke) >>".encode("cp1252", "replace"))
    saida = io.BytesIO()
    saida.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    pos = []
    for i, o in enumerate(objs, start=1):
        pos.append(saida.tell())
        saida.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = saida.tell()
    saida.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for p in pos:
        saida.write(f"{p:010d} 00000 n \n".encode())
    ident = hashlib.md5(saida.getvalue()).hexdigest()
    saida.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R "
                f"/ID [<{ident}> <{ident}>] >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return saida.getvalue()


# ------------------------------------------------------------------ ARP: gravação dos registros
def cpf_de(db: Session, usuario_id: int) -> str:
    r = db.scalar(select(DadosRH.cpf).where(DadosRH.usuario_id == usuario_id)
                  .execution_options(alcance_livre=True))
    return digitos(r)


def _ultimo_nsr(db: Session, empresa_id: int) -> int:
    pend = db.info.setdefault("rep_nsr", {})
    with db.no_autoflush:
        atual = db.scalar(select(func.max(RegistroRep.nsr)).where(RegistroRep.empresa_id == empresa_id)) or 0
    return max(atual, pend.get(empresa_id, 0))


def _novo(db: Session, empresa_id: int, tipo: int, **campos) -> RegistroRep:
    nsr = _ultimo_nsr(db, empresa_id) + 1
    db.info["rep_nsr"][empresa_id] = nsr
    r = RegistroRep(empresa_id=empresa_id, nsr=nsr, tipo=tipo, gravado_em=banco.agora(), **campos)
    db.add(r)
    return r


def _dados_empregador(e: Empresa) -> dict:
    doc = digitos(e.cpf_cnpj)
    return {"doc_tipo": "2" if len(doc) == 11 else "1", "doc": doc, "cno": digitos(e.rep_cno),
            "nome": e.nome, "local": (e.rep_local or e.endereco or "")[:100], "resp": digitos(e.rep_cpf_resp)}


def _garantir_inicio(db: Session, e: Empresa):
    """O primeiro registro de uma empresa é a identificação do empregador (tipo 2)."""
    if _ultimo_nsr(db, e.id) == 0:
        _novo(db, e.id, 2, dados=json.dumps(_dados_empregador(e), ensure_ascii=False))


def _hash7(nsr: int, marc: str, cpf: str, grav: str, coletor: str, offline: str, anterior: str) -> str:
    texto = f"{nsr:09d}7{marc}{_n(cpf, 12)}{grav}{coletor}{offline}{anterior}"
    return hashlib.sha256(texto.encode("latin-1")).hexdigest()


def registrar_marcacao(db: Session, b: Batida, coletor: str = COLETOR_WEB) -> RegistroRep:
    """Grava a marcação original do trabalhador no ARP (tipo 7). Chamado ao bater o ponto."""
    e = db.get(Empresa, b.empresa_id)
    if b.id is None:
        db.flush()
    _garantir_inicio(db, e)
    db.flush()
    anterior = db.scalar(select(RegistroRep.hash).where(
        RegistroRep.empresa_id == e.id, RegistroRep.tipo == 7).order_by(RegistroRep.nsr.desc()).limit(1)) or ""
    cpf = cpf_de(db, b.usuario_id)
    agora = banco.agora()
    nsr = _ultimo_nsr(db, e.id) + 1
    h = _hash7(nsr, dh(e, b.momento), cpf, dh(e, agora), coletor, "0", anterior)
    db.info["rep_nsr"][e.id] = nsr
    r = RegistroRep(empresa_id=e.id, nsr=nsr, tipo=7, gravado_em=agora, marcacao_em=b.momento, cpf=cpf,
                    coletor=coletor, offline="0", hash=h, batida_id=b.id)
    db.add(r)
    db.flush()
    return r


def evento_servico(codigo: str):
    """Evento sensível 07 (serviço disponível) ou 08 (indisponível) em toda empresa que já usa o ARP."""
    try:
        with SessionLocal() as db:
            ids = [i for (i,) in db.execute(select(RegistroRep.empresa_id).distinct())]
            for eid in ids:
                _novo(db, eid, 6, dados=json.dumps({"evento": codigo}))
            db.commit()
    except Exception:  # noqa: BLE001 — o app sobe mesmo assim
        log.exception("Falha ao gravar o evento %s do REP-P", codigo)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _limpar(session):
    session.info.pop("rep_nsr", None)


@event.listens_for(Session, "before_flush")
def _cadastro(session, _ctx, _inst):
    """Inclusão, alteração e exclusão de empregado (tipo 5) e mudança do empregador (tipo 2)."""
    try:
        with session.no_autoflush:
            _cadastro_interno(session)
    except Exception:  # noqa: BLE001 — nunca impede o cadastro de ser salvo
        log.exception("Falha ao registrar o cadastro no ARP")


def _mudou(obj, campo) -> tuple[bool, object]:
    from sqlalchemy import inspect as insp
    h = insp(obj).attrs[campo].history
    return bool(h.added or h.deleted), (h.deleted[0] if h.deleted else None)


def _tem_arp(session, empresa_id: int) -> bool:
    return bool(session.info.get("rep_nsr", {}).get(empresa_id)) or bool(session.scalar(
        select(RegistroRep.id).where(RegistroRep.empresa_id == empresa_id).limit(1)))


def _cadastro_interno(session):
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, DadosRH) and obj.empresa_id:
            mudou, antes = _mudou(obj, "cpf")
            novo = digitos(obj.cpf)
            if not mudou or digitos(antes) == novo:
                continue
            u = session.get(Usuario, obj.usuario_id) if obj.usuario_id else None
            if not u:
                continue
            e = session.get(Empresa, obj.empresa_id)
            _garantir_inicio(session, e)
            if digitos(antes):
                _novo(session, e.id, 5, cpf=digitos(antes), dados=json.dumps(
                    {"op": "E", "nome": u.nome, "resp": digitos(e.rep_cpf_resp)}, ensure_ascii=False))
            if novo:
                _novo(session, e.id, 5, cpf=novo, dados=json.dumps(
                    {"op": "I", "nome": u.nome, "resp": digitos(e.rep_cpf_resp)}, ensure_ascii=False))
        elif isinstance(obj, Usuario) and obj in session.dirty and obj.empresa_id:
            mudou_nome, _ = _mudou(obj, "nome")
            mudou_ativo, _ = _mudou(obj, "ativo")
            if not (mudou_nome or mudou_ativo) or not _tem_arp(session, obj.empresa_id):
                continue
            cpf = digitos(session.scalar(select(DadosRH.cpf).where(DadosRH.usuario_id == obj.id)
                                         .execution_options(alcance_livre=True)))
            if not cpf:
                continue
            e = session.get(Empresa, obj.empresa_id)
            op = "E" if mudou_ativo and not obj.ativo else ("I" if mudou_ativo else "A")
            _novo(session, e.id, 5, cpf=cpf, dados=json.dumps(
                {"op": op, "nome": obj.nome, "resp": digitos(e.rep_cpf_resp)}, ensure_ascii=False))
        elif isinstance(obj, Empresa) and obj in session.dirty:
            if not any(_mudou(obj, c)[0] for c in ("nome", "cpf_cnpj", "rep_cno", "endereco", "rep_local")):
                continue
            if _tem_arp(session, obj.id):
                _novo(session, obj.id, 2, dados=json.dumps(_dados_empregador(obj), ensure_ascii=False))


# ------------------------------------------------------------------ formatos
def dh(e: Empresa, momento_utc: datetime) -> str:
    """Data e hora no padrão AAAA-MM-ddThh:mm:00ZZZZZ, no fuso da empresa."""
    local = momento_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(e.fuso or "America/Sao_Paulo"))
    off = local.utcoffset() or timedelta(0)
    minutos = int(off.total_seconds() // 60)
    sinal = "-" if minutos < 0 else "+"
    minutos = abs(minutos)
    return local.strftime("%Y-%m-%dT%H:%M:00") + f"{sinal}{minutos // 60:02d}{minutos % 60:02d}"


def crc16_kermit(texto: str) -> str:
    crc = 0
    for byte in texto.encode("latin-1", "replace"):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return f"{crc:04X}"


def _n(v, tam: int) -> str:
    """Numérico: alinhado à direita com zeros; vazio vira espaços."""
    d = digitos(str(v))
    return d.zfill(tam)[-tam:] if d else " " * tam


def _a(v, tam: int) -> str:
    return str(v or "")[:tam].ljust(tam)


# ------------------------------------------------------------------ AFD
def gerar_afd(db: Session, e: Empresa, ini: date, fim: date) -> tuple[str, bytes]:
    c = config(db)
    fuso = ZoneInfo(e.fuso or "America/Sao_Paulo")
    a, b = (datetime.combine(ini, datetime.min.time(), fuso).astimezone(timezone.utc).replace(tzinfo=None),
            datetime.combine(fim + timedelta(days=1), datetime.min.time(), fuso).astimezone(timezone.utc).replace(tzinfo=None))
    regs = list(db.scalars(select(RegistroRep).where(
        RegistroRep.empresa_id == e.id, RegistroRep.gravado_em >= a, RegistroRep.gravado_em < b)
        .order_by(RegistroRep.nsr)))
    doc = digitos(e.cpf_cnpj)
    dev = digitos(c["dev_doc"])
    linhas = []
    cab = ("0" * 9 + "1" + ("2" if len(doc) == 11 else "1") + _a(doc, 14) + _n(e.rep_cno, 14)
           + _a(e.nome, 150) + _a(digitos(c["inpi"]), 17) + ini.isoformat() + fim.isoformat()
           + dh(e, banco.agora()) + "004" + ("2" if len(dev) == 11 else "1") + _a(dev, 14) + _a("", 30))
    linhas.append(cab + crc16_kermit(cab))
    conta = {2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0}
    for r in regs:
        d = json.loads(r.dados or "{}")
        if r.tipo == 2:
            s = (f"{r.nsr:09d}2" + dh(e, r.gravado_em) + _n(d.get("resp"), 14) + (d.get("doc_tipo") or "1")
                 + _a(d.get("doc"), 14) + _n(d.get("cno"), 14) + _a(d.get("nome"), 150) + _a(d.get("local"), 100))
            linhas.append(s + crc16_kermit(s))
        elif r.tipo == 5:
            s = (f"{r.nsr:09d}5" + dh(e, r.gravado_em) + (d.get("op") or "A") + _n(r.cpf, 12)
                 + _a(d.get("nome"), 52) + _a("", 4) + _n(d.get("resp"), 11))
            linhas.append(s + crc16_kermit(s))
        elif r.tipo == 6:
            linhas.append(f"{r.nsr:09d}6" + dh(e, r.gravado_em) + _n(d.get("evento"), 2))
        elif r.tipo == 7:
            linhas.append(f"{r.nsr:09d}7" + dh(e, r.marcacao_em) + _n(r.cpf, 12) + dh(e, r.gravado_em)
                          + r.coletor + r.offline + r.hash)
        else:
            continue
        conta[r.tipo] += 1
    linhas.append("999999999" + "".join(f"{conta[t]:09d}" for t in (2, 3, 4, 5, 6, 7)) + "9")
    linhas.append(_a("ASSINATURA_DIGITAL_EM_ARQUIVO_P7S", 100))
    nome = f"AFD{digitos(c['inpi'])}{doc}REP_P.txt"
    return nome, ("\r\n".join(linhas) + "\r\n").encode("latin-1", "replace")


# ------------------------------------------------------------------ AEJ
def gerar_aej(db: Session, e: Empresa, ini: date, fim: date) -> tuple[str, bytes]:
    from . import apuracao
    c = config(db)
    doc = digitos(e.cpf_cnpj)
    fuso = ZoneInfo(e.fuso or "America/Sao_Paulo")
    a, b = (datetime.combine(ini, datetime.min.time(), fuso).astimezone(timezone.utc).replace(tzinfo=None),
            datetime.combine(fim + timedelta(days=1), datetime.min.time(), fuso).astimezone(timezone.utc).replace(tzinfo=None))
    pessoas = {u.id: u for u in db.scalars(select(Usuario).where(Usuario.empresa_id == e.id)
                                            .execution_options(alcance_livre=True))}
    cpfs = {u: digitos(c_) for u, c_ in db.execute(select(DadosRH.usuario_id, DadosRH.cpf).where(
        DadosRH.empresa_id == e.id).execution_options(alcance_livre=True))}
    bats = list(db.scalars(select(Batida).where(Batida.empresa_id == e.id, Batida.momento >= a,
                                                Batida.momento < b).order_by(Batida.momento)))
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(ini, fim)))}
    vinculos = {}
    for bt in bats:
        if cpfs.get(bt.usuario_id) and bt.usuario_id not in vinculos:
            vinculos[bt.usuario_id] = len(vinculos) + 1
    horarios: dict[tuple, str] = {}

    def cod_horario(t: Turno | None) -> str:
        if not t or t.folga or not t.inicio or not t.fim:
            chave = ("", "", 0)
        else:
            chave = (t.inicio.replace(":", ""), t.fim.replace(":", ""), regras.minutos_turno(t))
        if chave not in horarios:
            horarios[chave] = f"H{len(horarios) + 1}"
        return horarios[chave]

    reg05 = []
    seq: dict[tuple, int] = {}
    for bt in bats:
        vid = vinculos.get(bt.usuario_id)
        if not vid:
            continue
        t = turnos.get((bt.usuario_id, bt.dia))
        original = not bt.manual and bt.origem != "auto"
        tp = "D" if bt.anulada else ("E" if bt.tipo == "entrada" else "S")
        chave = (bt.usuario_id, bt.dia)
        if tp == "E":
            seq[chave] = seq.get(chave, 0) + 1
        n = max(1, seq.get(chave, 1))
        fonte = "O" if original else "I"
        motivo = ""
        if tp == "D":
            motivo = f"Anulada por {bt.anulada_por}".strip() if bt.anulada_por else "Marcação desconsiderada"
        elif fonte == "I":
            motivo = (bt.obs or ("Saída automática do sistema" if bt.origem == "auto" else "Lançada pelo gestor"))
        cod = cod_horario(t) if tp == "E" and n == 1 else ""
        reg05.append(["05", str(vid), dh(e, bt.momento), "1" if original else "", tp, f"{n:03d}", fonte,
                      cod, motivo[:150].replace("|", "/")])
    reg07 = []
    if vinculos:
        for ap in apuracao.apurar(db, e, ini, fim):
            vid = vinculos.get(ap["u"].id)
            if not vid:
                continue
            for dia in ap["dias"]:
                if dia["turno"] is not None and dia["turno"].folga:
                    reg07.append(["07", str(vid), "1", dia["dia"].isoformat(), "", ""])
                if dia["falta"]:
                    reg07.append(["07", str(vid), "2", dia["dia"].isoformat(), "", ""])
                if e.controle == "banco":
                    if dia["extra"]:
                        reg07.append(["07", str(vid), "3", dia["dia"].isoformat(), str(dia["extra"]), "1"])
                    if dia["debito"]:
                        reg07.append(["07", str(vid), "3", dia["dia"].isoformat(), str(dia["debito"]), "2"])
    dev = digitos(c["dev_doc"])
    linhas = [["01", "2" if len(doc) == 11 else "1", doc, "", digitos(e.rep_cno)[:12], e.nome[:150],
               ini.isoformat(), fim.isoformat(), dh(e, banco.agora()), "002"],
              ["02", "1", "3", digitos(c["inpi"])]]
    linhas += [["03", str(vid), cpfs[uid], pessoas[uid].nome[:150]] for uid, vid in vinculos.items()]
    reg04 = []
    for (ent, sai, dur), cod in horarios.items():
        reg04.append(["04", cod, str(dur), ent or "0000", sai or "0000"])
    linhas += reg04 + reg05 + reg07
    linhas.append(["08", c["programa"][:150], c["versao"][:8], "2" if len(dev) == 11 else "1", dev,
                   c["dev_razao"][:150], c["dev_email"][:50]])
    linhas.append(["99", "1", "1", str(len(vinculos)), str(len(reg04)), str(len(reg05)), "0",
                   str(len(reg07)), "1"])
    texto = "\r\n".join("|".join(x) for x in linhas) + "\r\n" + _a("ASSINATURA_DIGITAL_EM_ARQUIVO_P7S", 100) + "\r\n"
    return f"AEJ{doc}{ini:%Y%m%d}{fim:%Y%m%d}.txt", texto.encode("latin-1", "replace")


def pacote(db: Session, nome: str, conteudo: bytes) -> tuple[bytes, bool]:
    """ZIP com o arquivo e a assinatura .p7s (se houver certificado)."""
    p7s = assinar_cades(db, conteudo)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(nome, conteudo)
        if p7s:
            z.writestr(nome + ".p7s", p7s)
    return buf.getvalue(), bool(p7s)


# ------------------------------------------------------------------ comprovante e atestado
def comprovante_pdf(db: Session, r: RegistroRep) -> bytes:
    e = db.get(Empresa, r.empresa_id)
    c = config(db)
    b = db.get(Batida, r.batida_id) if r.batida_id else None
    u = db.get(Usuario, b.usuario_id) if b else None
    local = regras.para_local(e, r.marcacao_em)
    doc = digitos(e.cpf_cnpj)
    blocos = [("Comprovante de Registro de Ponto do Trabalhador", 16, True), ("", 8, False),
              (f"NSR: {r.nsr:09d}", 11, True),
              ("Empregador", 10, True),
              (f"{e.nome}", 10, False),
              (f"{'CPF' if len(doc) == 11 else 'CNPJ'}: {fmt_doc(doc) or 'não informado'}"
               + (f"    CNO/CAEPF: {digitos(e.rep_cno)}" if digitos(e.rep_cno) else ""), 10, False),
              (f"Local da prestação do serviço: {(e.rep_local or e.endereco or 'não informado')}", 10, False),
              ("", 6, False), ("Trabalhador", 10, True),
              (f"{u.nome if u else ''}", 10, False), (f"CPF: {fmt_doc(r.cpf) or 'não informado'}", 10, False),
              ("", 6, False), ("Marcação", 10, True),
              (f"Data e hora: {local:%d/%m/%Y %H:%M} ({dh(e, r.marcacao_em)[-5:]})", 12, True),
              ("", 6, False), ("Registrador", 10, True),
              (f"REP-P {c['programa']} · registro INPI nº {digitos(c['inpi']) or 'em andamento'}", 10, False),
              (f"Código de verificação (SHA-256): {r.hash}", 8, False)]
    conteudo = pdf(blocos, "Comprovante de Registro de Ponto do Trabalhador")
    return assinar_pades(db, conteudo, ("empresa",))[0]


def fmt_doc(d: str) -> str:
    d = digitos(d)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return d


def atestado_pdf(db: Session, e: Empresa) -> tuple[bytes, bool]:
    c = config(db)
    hoje = regras.hoje(e)
    blocos = [("ATESTADO TÉCNICO E TERMO DE RESPONSABILIDADE", 15, True),
              ("Portaria MTP nº 671, de 8 de novembro de 2021, art. 89", 9, False), ("", 8, False),
              ("Tipo: REP-P (Registrador Eletrônico de Ponto via Programa) e PTRP (Programa de Tratamento de Registro de Ponto)", 10, False),
              ("Marca e modelo do equipamento: N/A · Número de fabricação: N/A", 10, False),
              (f"Identificador do programa: {c['programa']}", 10, False),
              (f"Versão do programa: {c['versao']}", 10, False),
              (f"Número do registro do programa no INPI: {digitos(c['inpi']) or 'não informado'}", 10, False),
              ("", 6, False), ("Desenvolvedor", 10, True),
              (f"{c['dev_razao']} · {'CPF' if len(digitos(c['dev_doc'])) == 11 else 'CNPJ'} {fmt_doc(c['dev_doc'])}", 10, False),
              (f"{c['dev_endereco']}", 10, False),
              ("", 6, False), ("Empresa destinatária", 10, True),
              (f"{e.nome} · {'CPF' if len(digitos(e.cpf_cnpj)) == 11 else 'CNPJ'} {fmt_doc(e.cpf_cnpj) or 'não informado'}", 10, False),
              ("", 6, False), ("Declaração", 10, True),
              (f"Na qualidade de responsável legal e de responsável técnico do desenvolvedor acima "
               f"identificado, os signatários atestam e declaram, sob as penas da lei, que o programa "
               f"{c['programa']}, na versão indicada, atende aos requisitos da Portaria MTP nº 671/2021 "
               f"aplicáveis ao REP-P (Anexo IX) e ao programa de tratamento de registro de ponto: não "
               f"permite alterar nem apagar as marcações registradas, grava cada marcação com NSR e "
               f"código hash SHA-256, disponibiliza ao trabalhador o Comprovante de Registro de Ponto "
               f"logo após cada marcação, gera o Arquivo Fonte de Dados (AFD) e o Arquivo Eletrônico de "
               f"Jornada (AEJ) nos leiautes oficiais, com assinatura eletrônica, e mantém as marcações "
               f"originais identificadas separadamente das inclusões e desconsiderações feitas pelo "
               f"empregador, com o motivo de cada uma. Os signatários se responsabilizam pelas informações "
               f"deste atestado.", 10, False),
              ("", 10, False),
              (f"{hoje:%d/%m/%Y}", 10, False), ("", 10, False),
              (f"Responsável legal: {c['legal_nome']} · CPF {fmt_doc(c['legal_cpf'])}", 10, False),
              (f"Responsável técnico: {c['tec_nome']} · CPF {fmt_doc(c['tec_cpf'])}", 10, False),
              ("Documento assinado eletronicamente com certificados digitais ICP-Brasil (PAdES).", 8, False)]
    conteudo = pdf(blocos, "Atestado Técnico e Termo de Responsabilidade")
    tipos = ("legal",) if digitos(c["legal_cpf"]) == digitos(c["tec_cpf"]) else ("legal", "tecnico")
    return assinar_pades(db, conteudo, tipos)


# ------------------------------------------------------------------ trabalhador: comprovantes
@rota.get("/app/comprovantes")
def meus_comprovantes(request: Request, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    desde = banco.agora() - timedelta(days=31)
    ids = [b.id for b in db.scalars(select(Batida).where(Batida.usuario_id == u.id, Batida.momento >= desde))]
    regs = list(db.scalars(select(RegistroRep).where(RegistroRep.batida_id.in_(ids), RegistroRep.tipo == 7)
                           .order_by(RegistroRep.nsr.desc()))) if ids else []
    return render(request, "app_comprovantes.html", u=u, e=u.empresa, regs=regs, aba="comprovantes")


@rota.get("/app/comprovante/{nsr}.pdf")
def comprovante(nsr: int, request: Request, u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    r = db.scalar(select(RegistroRep).where(RegistroRep.empresa_id == u.empresa_id, RegistroRep.nsr == nsr,
                                            RegistroRep.tipo == 7))
    b = db.get(Batida, r.batida_id) if r and r.batida_id else None
    if not r or not b or (b.usuario_id != u.id and not (u.gestor and pode(u, "ponto"))):
        return Response("Comprovante não encontrado.", status_code=404)
    return Response(comprovante_pdf(db, r), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="comprovante-{r.nsr:09d}.pdf"'})


# ------------------------------------------------------------------ empresa: Portaria 671
def _periodo(e: Empresa, ini: str, fim: str) -> tuple[date, date]:
    hoje = regras.hoje(e)
    try:
        f = date.fromisoformat(fim)
    except ValueError:
        f = hoje
    try:
        i = date.fromisoformat(ini)
    except ValueError:
        i = f.replace(day=1)
    if i > f:
        i, f = f, i
    return i, min(f, hoje)


@rota.get("/painel/portaria671")
def tela(request: Request, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    equipe = list(db.scalars(select(Usuario).where(Usuario.empresa_id == e.id, Usuario.ativo.is_(True),
                                                   Usuario.na_escala.is_(True)).order_by(Usuario.nome)))
    cpfs = {uid: digitos(c) for uid, c in db.execute(select(DadosRH.usuario_id, DadosRH.cpf).where(
        DadosRH.empresa_id == e.id).execution_options(alcance_livre=True))}
    sem_cpf = [p for p in equipe if len(cpfs.get(p.id, "")) != 11]
    total = db.scalar(select(func.count()).select_from(RegistroRep).where(RegistroRep.empresa_id == e.id)) or 0
    hoje = regras.hoje(e)
    return render(request, "painel_portaria671.html", u=u, e=e, aba="conformidade", sem_cpf=sem_cpf,
                  total=total, plat671=situacao(db), ini=hoje.replace(day=1), fim=hoje,
                  doc_ok=len(digitos(e.cpf_cnpj)) in (11, 14))


@rota.post("/painel/portaria671", dependencies=CSRF)
def salvar_empresa(request: Request, cpf_cnpj: str = Form(""), rep_cno: str = Form(""),
                   rep_cpf_resp: str = Form(""), rep_local: str = Form(""),
                   u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    from .asaas import cpf_cnpj_valido
    e = u.empresa
    if not u.dono:
        avisar(request, "Só o dono da conta muda estes dados.", "erro")
        return ir("/painel/portaria671")
    doc, resp = digitos(cpf_cnpj), digitos(rep_cpf_resp)
    if doc and not cpf_cnpj_valido(doc):
        avisar(request, "CPF ou CNPJ da empresa inválido.", "erro")
        return ir("/painel/portaria671")
    if resp and not cpf_cnpj_valido(resp):
        avisar(request, "CPF do responsável inválido.", "erro")
        return ir("/painel/portaria671")
    e.cpf_cnpj, e.rep_cno, e.rep_cpf_resp = doc or e.cpf_cnpj, digitos(rep_cno)[:14], resp
    e.rep_local = " ".join(rep_local.split())[:100]
    regras.registrar(db, e.id, u.nome, "portaria 671", "Dados do empregador para o REP-P atualizados.")
    db.commit()
    avisar(request, "Dados salvos.")
    return ir("/painel/portaria671")


@rota.get("/painel/portaria671/{arquivo}")
def baixar(arquivo: str, request: Request, ini: str = "", fim: str = "",
           u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    if not pode(u, "ponto") or restrito(u):   # arquivo da empresa inteira
        avisar(request, "Os arquivos da Portaria 671 são da empresa inteira: peça ao dono da conta.", "erro")
        raise Redirecionar("/painel/portaria671")
    if arquivo == "atestado.pdf":
        conteudo, assinado = atestado_pdf(db, e)
        return Response(conteudo, media_type="application/pdf", headers={
            "Content-Disposition": 'attachment; filename="atestado-tecnico-portaria-671.pdf"'})
    if arquivo not in ("afd", "aej"):
        raise Redirecionar("/painel/portaria671")
    i, f = _periodo(e, ini, fim)
    nome, conteudo = (gerar_afd if arquivo == "afd" else gerar_aej)(db, e, i, f)
    z, assinado = pacote(db, nome, conteudo)
    regras.registrar(db, e.id, u.nome, f"{arquivo} gerado",
                     f"{arquivo.upper()} de {i:%d/%m/%Y} a {f:%d/%m/%Y}" + ("" if assinado else " (sem assinatura digital)") + ".")
    db.commit()
    return Response(z, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{nome[:-4]}.zip"'})


# ------------------------------------------------------------------ master
@rota.get("/master/portaria671")
def master_tela(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    return render(request, "master/portaria671.html", u=u, e=u.empresa, aba="master", sub="portaria671",
                  s=situacao(db), CERTS=CERTS)


@rota.post("/master/portaria671", dependencies=CSRF)
async def master_salvar(request: Request, u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    form = await request.form()
    c = config(db)
    for k in CAMPOS_CONFIG:
        if k in form:
            v = str(form.get(k) or "").strip()[:200]
            c[k] = digitos(v) if k in ("inpi", "dev_doc", "legal_cpf", "tec_cpf") else v
    reg = db.get(Config, "rep") or Config(chave="rep", valor="")
    reg.valor = json.dumps(c, ensure_ascii=False)
    db.merge(reg)
    db.commit()
    avisar(request, "Dados do desenvolvedor salvos.")
    return ir("/master/portaria671")


@rota.post("/master/portaria671/certificado", dependencies=CSRF)
def master_certificado(request: Request, tipo: str = Form(""), senha: str = Form(""),
                       arquivo: UploadFile | None = File(None), remover: str = Form(""),
                       u: Usuario = Depends(admin_logado), db: Session = Depends(get_db)):
    if tipo not in CERTS:
        return ir("/master/portaria671")
    destino = PASTA_CERT / f"{tipo}.pfx"
    if remover:
        destino.unlink(missing_ok=True)
        cfg = db.get(Config, f"rep_senha_{tipo}")
        if cfg:
            db.delete(cfg)
        db.commit()
        avisar(request, "Certificado removido.")
        return ir("/master/portaria671")
    dados = arquivo.file.read(200_001) if arquivo else b""
    if not dados or len(dados) > 200_000:
        avisar(request, "Envie o arquivo .pfx (ou .p12) do certificado A1.", "erro")
        return ir("/master/portaria671")
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        chave, cert, _ = pkcs12.load_key_and_certificates(dados, senha.encode() or None)
        if not chave or not cert:
            raise ValueError("sem chave")
    except Exception:  # noqa: BLE001
        avisar(request, "Não consegui abrir o certificado. Confira o arquivo e a senha.", "erro")
        return ir("/master/portaria671")
    PASTA_CERT.mkdir(parents=True, exist_ok=True)
    try:
        PASTA_CERT.chmod(0o700)
    except OSError:
        pass
    destino.write_bytes(dados)
    try:
        destino.chmod(0o600)
    except OSError:
        pass
    db.merge(Config(chave=f"rep_senha_{tipo}", valor=senha))
    db.commit()
    avisar(request, "Certificado salvo.")
    return ir("/master/portaria671")


# ------------------------------------------------------------------ página pública
@rota.get("/portaria-671")
def pagina_publica(request: Request, db: Session = Depends(get_db)):
    s = situacao(db)
    return render(request, "portaria671.html", s=s, inpi=digitos(s["config"]["inpi"]))
