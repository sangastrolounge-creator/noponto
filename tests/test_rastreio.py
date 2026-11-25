"""Rastreamento de anúncios, origem do cliente e relatório de canais. Rodar da raiz: python tests/test_rastreio.py"""
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

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import core, rastreio, vendas  # noqa: E402
from app.db import (Config, ConversaVenda, Empresa, Lead, Pagamento, Plataforma, SessionLocal,  # noqa: E402
                    Usuario)
from app.main import app  # noqa: E402


class _Msgs:
    def create(self, **kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Oi!")])


sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda **kw: types.SimpleNamespace(messages=_Msgs()))


def csrf(cli, caminho):
    return re.search(r'name="csrf" value="([^"]+)"', cli.get(caminho).text).group(1)


def cadastrar(c, empresa, nome, email):
    return c.post("/cadastro", data={"csrf": csrf(c, "/cadastro"), "aceito": "1", "empresa": empresa,
                                     "nome": nome, "email": email, "senha": "segredo1"})


def origem(cli):
    return rastreio.ler(types.SimpleNamespace(cookies={"dw_origem": cli.cookies.get("dw_origem") or ""}))


def main():
    m = TestClient(app, follow_redirects=False)
    cadastrar(m, "Plataforma", "Dono", "dono@plataforma.com")
    with SessionLocal() as db:
        db.get(Plataforma, 1).whatsapp = "98981004999"
        db.merge(Config(chave="anthropic_api_key", valor="sk-teste"))
        db.commit()
    core._plat_cache = None

    # sem códigos: nenhuma linha de pixel nem aviso de cookies
    v0 = TestClient(app, follow_redirects=False)
    assert "dw_cookies" not in v0.get("/").text

    # Master -> Rastreamento: valida o formato e salva o resto
    tok = csrf(m, "/master/rastreamento")
    m.post("/master/rastreamento", data={"csrf": tok, "rast_meta_pixel": "1234567890123", "rast_ga4": "errado",
                                         "rast_google_ads": "AW-123456789", "rast_google_ads_rotulo": "AbCdEf123",
                                         "rast_tiktok_pixel": "c1a2b3c4d5e6f7g8"})
    d = rastreio.ids()
    assert d["rast_meta_pixel"] == "1234567890123" and d["rast_ga4"] == "" and d["rast_tiktok_pixel"] == "C1A2B3C4D5E6F7G8"
    assert "Confira o formato de" in m.get("/master/rastreamento").text
    m.post("/master/rastreamento", data={"csrf": tok, "rast_meta_pixel": "1234567890123", "rast_ga4": "G-ABC1234",
                                         "rast_google_ads": "AW-123456789", "rast_google_ads_rotulo": "AbCdEf123",
                                         "rast_tiktok_pixel": "C1A2B3C4D5E6F7G8"})
    assert rastreio.ids()["rast_ga4"] == "G-ABC1234"

    # visitante chega por um anúncio do Instagram: a origem fica num cookie nosso
    v = TestClient(app, follow_redirects=False)
    home = v.get("/?utm_source=instagram&utm_medium=cpc&utm_campaign=escala&fbclid=abc")
    o = origem(v)
    assert o["source"] == "instagram" and o["campaign"] == "escala" and o["clique"] == "meta"
    assert '"meta": "1234567890123"' in home.text and "dw_cookies" in home.text      # código só com o aceite
    assert "Aviso de cookies" in home.text and "/privacy#cookies" in home.text
    v.get("/?utm_source=x", headers={"referer": "https://www.google.com/"})   # nova campanha substitui
    assert origem(v)["source"] == "x"
    v.get("/?utm_source=instagram&utm_medium=cpc&utm_campaign=escala")
    v.get("/", headers={"referer": "https://www.google.com/"})                # sem campanha: não substitui
    assert origem(v)["source"] == "instagram"
    assert "dw_cookies" not in v.get("/privacy").text                       # páginas legais: sem códigos

    # cadastro: a origem vai para a empresa e para o lead; a primeira tela do painel envia a conversão
    cadastrar(v, "Padaria Sol", "Bia", "bia@sol.com")
    with SessionLocal() as db:
        e = db.scalar(select(Empresa).where(Empresa.nome == "Padaria Sol"))
        assert json.loads(e.origem)["source"] == "instagram"
        ld = db.scalar(select(Lead).where(Lead.empresa_id == e.id))
        assert ld and rastreio.canal(ld.utm) == "Instagram · anúncio"
        eid, lid = e.id, ld.id
    p1 = v.get("/painel").text
    assert '"evento": true' in p1 and "CompleteRegistration" in p1
    assert "dw_cookies" not in v.get("/painel").text                        # uma vez só; painel sem códigos

    # chat do site: a origem também vai para a conversa e para o lead do contato
    w = TestClient(app, follow_redirects=False)
    w.get("/?utm_source=google&utm_medium=cpc&utm_campaign=ponto&gclid=xyz")
    r = w.post("/vendedor/mensagem", json={"texto": "Quanto custa?"}).json()
    w.post("/vendedor/contato", json={"conversa": r["conversa"], "nome": "Rui", "telefone": "(98) 99999-0005",
                                      "autorizo": True})
    with SessionLocal() as db:
        c = db.scalar(select(ConversaVenda).where(ConversaVenda.chave == r["conversa"]))
        assert json.loads(c.utm)["source"] == "google"
        lr = db.scalar(select(Lead).where(Lead.telefone == "98999990005"))
        assert rastreio.canal(lr.utm) == "Google · anúncio"
        rid = lr.id

    # orgânico: veio do Instagram sem link marcado
    z = TestClient(app, follow_redirects=False)
    z.get("/", headers={"referer": "https://l.instagram.com/?u=x"})
    assert rastreio.canal(origem(z)) == "Instagram · orgânico"
    assert rastreio.canal("") == "Direto" and rastreio.canal('{"ref": "blog.exemplo.com"}') == "Outros sites"

    # fichas mostram a origem
    assert "Instagram · anúncio · escala" in m.get(f"/master/crm/{lid}").text
    assert "Google · anúncio · ponto" in m.get(f"/master/crm/{rid}").text
    assert "chegou por" in m.get(f"/master/empresas/{eid}").text

    # relatório de canais: conta de teste e pagante
    with SessionLocal() as db:
        db.add(Pagamento(empresa_id=eid, valor_centavos=13990, status="pago"))
        db.commit()
    tela = m.get("/master/crm/canais").text
    linha = re.search(r"<tr><td>Instagram · anúncio</td>(.*?)</tr>", tela, re.S).group(1)
    nums = re.findall(r'class="num">([^<]*)<', linha)
    assert nums[:3] == ["1", "1", "1"] and nums[-1] == "100%", nums
    assert "Google · anúncio" in tela

    # página de vendas: dias de teste sempre os atuais
    with SessionLocal() as db:
        db.get(Plataforma, 1).dias_teste = 14
        db.commit()
    core._plat_cache = None
    c = vendas.dias_atuais({"topo": {"botao": "Testar 7 dias grátis", "nota": "Teste por 7 dias sem pagar nada"},
                            "faq": {"lista": [{"resposta": "O teste dura 7 dias. O fechamento sai em 30 dias."}]}})
    assert c["topo"]["botao"] == "Testar 14 dias grátis" and c["topo"]["nota"] == "Teste por 14 dias sem pagar nada"
    assert c["faq"]["lista"][0]["resposta"] == "O teste dura 14 dias. O fechamento sai em 30 dias."

    # política de privacidade cita os códigos e o aceite
    p = v.get("/privacy").text
    assert "dw_origem" in p and "TikTok" in p
    print("ok: rastreamento, origem e canais")


if __name__ == "__main__":
    main()
