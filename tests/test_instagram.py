"""Leads do Instagram no CRM (Fase 1) e o link da bio com UTM (Fase 2). Rodar da raiz: python tests/test_instagram.py"""
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
                   "ADMIN_EMAIL": "dono@plataforma.com"})
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import crm, rastreio  # noqa: E402
from app.db import Empresa, Lead, LeadAtividade, SessionLocal, VisitaCanal  # noqa: E402
from app.main import app  # noqa: E402

BIO = "/?utm_source=instagram&utm_medium=social&utm_campaign=escala6x1"


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def cadastrar(c, empresa, nome, email):
    return c.post("/cadastro", data={"csrf": csrf(c, "/cadastro"), "aceito": "1", "empresa": empresa,
                                     "nome": nome, "email": email, "senha": "segredo1"})


def main():
    # o @ aceita "@perfil", "perfil" e o link do perfil; recusa o que não é perfil
    assert crm.arroba("@Padaria.Sol") == "padaria.sol" and crm.arroba("padaria_sol") == "padaria_sol"
    assert crm.arroba("https://www.instagram.com/padaria.sol/?hl=pt") == "padaria.sol"
    assert crm.arroba("padaria sol") == "" and crm.arroba("@") == "" and crm.arroba("x" * 31) == ""

    m = TestClient(app, follow_redirects=False)
    cadastrar(m, "Plataforma", "Dono", "dono@plataforma.com")

    # FASE 1 ---------------------------------------------------------------
    tela = m.get("/master/crm").text
    assert "@ do Instagram" in tela and '<option value="instagram">Instagram</option>' in tela

    # cadastro à mão: Instagram, @ e número de funcionários (sem nome: o @ vira o nome)
    tok = csrf(m, "/master/crm")
    r = m.post("/master/crm/novo", data={"csrf": tok, "nome": "", "instagram": "@Bar.Do.Ze", "funcionarios": "12",
                                         "origem": "instagram"})
    assert r.status_code == 303 and re.match(r"/master/crm/\d+$", r.headers["location"]), r.headers
    with SessionLocal() as db:
        ld = db.scalar(select(Lead).where(Lead.instagram == "bar.do.ze"))
        assert ld and ld.origem == "instagram" and ld.funcionarios == 12 and ld.nome == "@bar.do.ze"
        lid = ld.id

    # duplicidade pelo @ (com outra grafia) leva para a ficha que já existe
    r = m.post("/master/crm/novo", data={"csrf": tok, "nome": "Zé", "instagram": "instagram.com/BAR.DO.ZE",
                                         "origem": "instagram"})
    assert r.headers["location"] == f"/master/crm/{lid}"
    with SessionLocal() as db:
        assert len(db.scalars(select(Lead).where(Lead.instagram == "bar.do.ze")).all()) == 1

    # @ inválido é recusado
    m.post("/master/crm/novo", data={"csrf": tok, "nome": "X", "instagram": "bar do ze", "origem": "instagram"})
    with SessionLocal() as db:
        assert not db.scalar(select(Lead).where(Lead.nome == "X"))

    # outro lead manual, sem Instagram
    m.post("/master/crm/novo", data={"csrf": tok, "nome": "Lia Manual", "telefone": "98999990007", "origem": "manual"})

    # filtro do funil por origem e busca pelo @
    so_ig = m.get("/master/crm?ver=todos&origem=instagram").text
    assert "@bar.do.ze" in so_ig and "Lia Manual" not in so_ig and "12 funcionário(s)" in so_ig
    assert "Lia Manual" in m.get("/master/crm?ver=todos").text
    assert "@bar.do.ze" in m.get("/master/crm?ver=todos&q=%40bar.do").text

    # ficha: mostra o link do perfil; edita o @, o nome e a origem
    ficha = m.get(f"/master/crm/{lid}").text
    assert 'href="https://www.instagram.com/bar.do.ze/"' in ficha and 'value="@bar.do.ze"' in ficha
    tok = csrf(m, f"/master/crm/{lid}")
    m.post(f"/master/crm/{lid}", data={"csrf": tok, "nome": "Zé do Bar", "instagram": "@bardoze_oficial",
                                       "funcionarios": "15", "origem": "indicacao", "dono_id": ""})
    with SessionLocal() as db:
        ld = db.get(Lead, lid)
        assert ld.instagram == "bardoze_oficial" and ld.nome == "Zé do Bar" and ld.funcionarios == 15
        assert ld.origem == "indicacao"
        assert any("Origem:" in a.texto for a in db.scalars(select(LeadAtividade).where(LeadAtividade.lead_id == lid)))
    # não deixa usar o @ de outro lead
    m.post("/master/crm/novo", data={"csrf": tok, "nome": "Outro", "instagram": "@outro.perfil", "origem": "instagram"})
    m.post(f"/master/crm/{lid}", data={"csrf": tok, "nome": "Zé do Bar", "instagram": "outro.perfil",
                                       "funcionarios": "15", "dono_id": ""})
    with SessionLocal() as db:
        assert db.get(Lead, lid).instagram == "bardoze_oficial"

    # FASE 2 ---------------------------------------------------------------
    # link da bio: cai no canal Instagram orgânico com a campanha, e conta a visita uma vez por navegador
    v = TestClient(app, follow_redirects=False)
    v.get(BIO)
    o = rastreio.ler(types.SimpleNamespace(cookies={"dw_origem": v.cookies.get("dw_origem") or ""}))
    assert rastreio.canal(o) == "Instagram · orgânico" and rastreio.campanha(o) == "escala6x1"
    v.get(BIO)                                     # mesmo navegador, mesma campanha: não conta de novo
    TestClient(app, follow_redirects=False).get(BIO)   # outro navegador: conta
    with SessionLocal() as db:
        vis = db.scalar(select(VisitaCanal).where(VisitaCanal.campanha == "escala6x1"))
        assert vis and vis.visitas == 2 and vis.canal == "Instagram · orgânico"

    # conta de teste criada por esse link vira lead com origem Instagram e a campanha
    cadastrar(v, "Pizzaria Bela", "Ana", "ana@bela.com")
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Pizzaria Bela"))
        ld = db.scalar(select(Lead).where(Lead.empresa_id == e.id))
        assert ld.origem == "instagram" and json.loads(ld.utm)["campaign"] == "escala6x1"
        nid = ld.id
    assert "Instagram · orgânico · escala6x1" in m.get(f"/master/crm/{nid}").text
    # conta sem origem continua "Conta de teste"
    w = TestClient(app, follow_redirects=False)
    cadastrar(w, "Café Azul", "Rui", "rui@azul.com")
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Café Azul"))
        assert db.scalar(select(Lead).where(Lead.empresa_id == e.id)).origem == "teste"

    # Canais: visitas, leads e contas de teste do Instagram por campanha
    tela = m.get("/master/crm/canais").text
    linha = re.search(r"<tr><td>Instagram · orgânico</td><td>escala6x1</td>(.*?)</tr>", tela, re.S).group(1)
    nums = re.findall(r'class="num">([^<]*)<', linha)
    assert nums[:3] == ["2", "1", "1"], nums
    print("ok: Instagram no CRM e link da bio")


if __name__ == "__main__":
    main()
