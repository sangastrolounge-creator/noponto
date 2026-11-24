"""Ponto só no local de cada pessoa (lotação da ficha). Rodar da raiz: python tests/test_lotacao.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = tempfile.mkdtemp()
os.environ.update({"DATABASE_URL": f"sqlite:///{tmp}/teste.db".replace("\\", "/"),
                   "SECRET_KEY": "teste", "IDIOMA_PADRAO": "pt", "SEM_AGENDADOR": "1"})

from app import regras  # noqa: E402
from app.db import Empresa, Local, SessionLocal, Turno, Usuario, criar_tabelas  # noqa: E402
import app.main  # noqa: E402,F401

criar_tabelas()


def nomes(ls):
    return [x[0] for x in ls]


def main():
    with SessionLocal() as db:
        e = Empresa(nome="Rede Sol", lat=-2.50, lng=-44.30, raio_m=150)
        db.add(e)
        db.flush()
        assert e.ponto_na_lotacao                                   # empresa nova já vem com a regra ligada
        fil = Local(empresa_id=e.id, nome="Filial Centro", lat=-2.53, lng=-44.29, raio_m=150)
        fil2 = Local(empresa_id=e.id, nome="Filial Praia", lat=-2.48, lng=-44.25, raio_m=150)
        db.add_all([fil, fil2])
        db.flush()
        matriz = Usuario(empresa_id=e.id, nome="Ana Matriz", celular="98999990001")
        filial = Usuario(empresa_id=e.id, nome="Beto Filial", celular="98999990002", local_id=fil.id)
        db.add_all([matriz, filial])
        db.flush()
        # regra ligada: cada um só no seu local
        assert nomes(regras.lugares(db, e, None, matriz)) == [""]
        assert nomes(regras.lugares(db, e, None, filial)) == ["Filial Centro"]
        # a escala do dia manda: Ana escalada na Filial Praia só vale lá
        t = Turno(empresa_id=e.id, usuario_id=matriz.id, data=regras.agora_local(e).date(), inicio="08:00",
                  fim="17:00", local_id=fil2.id)
        assert nomes(regras.lugares(db, e, t, matriz)) == ["Filial Praia"]
        # a conferência da cerca durante o turno também respeita a lotação
        _, dentro_matriz = __import__("app.cerca", fromlist=["medir"]).medir(-2.53, -44.29, regras.lugares(db, e, None, matriz)[0])
        assert not dentro_matriz                                    # Ana na Filial Centro está fora do local dela
        # regra desligada: qualquer endereço cadastrado
        e.ponto_na_lotacao = False
        assert nomes(regras.lugares(db, e, None, matriz)) == ["", "Filial Centro", "Filial Praia"]
        assert nomes(regras.lugares(db, e, None, filial)) == ["", "Filial Centro", "Filial Praia"]
        # sem pessoa (chamadas antigas): continua como antes
        e.ponto_na_lotacao = True
        assert nomes(regras.lugares(db, e, None)) == ["", "Filial Centro", "Filial Praia"]
    escala_pela_tela()
    print("ok: ponto no local de cada pessoa")


def escala_pela_tela():
    """Na escala: a matriz aparece no campo Local; outro local vale só no dia, salvo se o gestor
    marcar "usar este local também nas próximas semanas"."""
    import re
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.db import EscalaFixa
    from app.main import app
    g = TestClient(app, follow_redirects=False)

    def tok(c):
        return re.search(r'name="csrf" value="([^"]+)"', c.get(c_url).text).group(1)
    c_url = "/cadastro"
    g.post("/cadastro", data={"csrf": tok(g), "aceito": "1", "empresa": "Rede Lua", "nome": "Gil",
                              "email": "gil@lua.com", "senha": "segredo1"})
    c_url = "/painel/escala"
    with SessionLocal() as db:
        gil = db.scalar(select(Usuario).where(Usuario.email == "gil@lua.com"))
        e = gil.empresa
        e.lat, e.lng = -2.5, -44.3
        fil = Local(empresa_id=e.id, nome="Filial Norte", lat=-2.4, lng=-44.2, raio_m=150)
        db.add(fil)
        db.flush()
        caio = Usuario(empresa_id=e.id, nome="Caio Filial", celular="98999990009", local_id=fil.id)
        db.add(caio)
        db.commit()
        cid, fid, hoje = caio.id, fil.id, regras.hoje(e)
    tela = g.get("/painel/escala").text
    assert 'value="principal"' in tela and "Rede Lua (endereço principal)" in tela
    dia = hoje + timedelta(days=3)

    def salvar(**extra):
        dados = {"csrf": tok(g), "usuario_id": str(cid), "data": dia.isoformat(), "modo": "trabalho",
                 "inicio": "08:00", "fim": "16:00", "intervalo": "60", "repetir": "dia", "fixar": "1"}
        dados.update(extra)
        assert g.post("/painel/escala/turno", data=dados).status_code == 303

    salvar(local_id="")                                     # escala fixa no local dele
    salvar(local_id="principal")                            # neste dia, na matriz (só neste dia)
    with SessionLocal() as db:
        t = db.scalar(select(Turno).where(Turno.usuario_id == cid, Turno.data == dia))
        r = db.scalar(select(EscalaFixa).where(EscalaFixa.usuario_id == cid, EscalaFixa.dia_semana == dia.weekday()))
        prox = db.scalar(select(Turno).where(Turno.usuario_id == cid, Turno.data == dia + timedelta(days=7)))
        assert t.so_principal and not t.local_id
        assert not r.so_principal and not r.local_id            # a escala fixa não pegou a matriz
        assert prox and not prox.so_principal and prox.inicio == "08:00"
        assert nomes(regras.lugares(db, t.usuario.empresa, t, t.usuario)) == [""]
    salvar(local_id="principal", local_sempre="1")          # agora decidido: toda semana na matriz
    with SessionLocal() as db:
        r = db.scalar(select(EscalaFixa).where(EscalaFixa.usuario_id == cid, EscalaFixa.dia_semana == dia.weekday()))
        prox = db.scalar(select(Turno).where(Turno.usuario_id == cid, Turno.data == dia + timedelta(days=7)))
        assert r.so_principal and prox.so_principal
    salvar(local_id=str(fid))                               # filial num dia: a fixa continua na matriz
    with SessionLocal() as db:
        r = db.scalar(select(EscalaFixa).where(EscalaFixa.usuario_id == cid, EscalaFixa.dia_semana == dia.weekday()))
        t = db.scalar(select(Turno).where(Turno.usuario_id == cid, Turno.data == dia))
        assert r.so_principal and t.local_id == fid and not t.so_principal
    assert "Principal" in g.get(f"/painel/escala?semana={(dia + timedelta(days=7) - timedelta(days=dia.weekday())).isoformat()}").text
    # filtro "Local" ao lado do setor: quem é do local e quem foi escalado lá na semana
    with SessionLocal() as db:
        db.add(Usuario(empresa_id=db.get(Usuario, cid).empresa_id, nome="Duda Matriz", celular="98999990010"))
        db.commit()
    sem = (dia - timedelta(days=dia.weekday())).isoformat()
    prox = (dia + timedelta(days=7) - timedelta(days=dia.weekday())).isoformat()
    tela = g.get(f"/painel/escala?semana={sem}&local=principal").text
    assert "Duda Matriz" in tela and "Caio Filial" not in tela and 'aria-label="Filtrar colaboradores por local"' in tela
    tela = g.get(f"/painel/escala?semana={sem}&local={fid}").text
    assert "Caio Filial" in tela and "Duda Matriz" not in tela
    assert "Caio Filial" in g.get(f"/painel/escala?semana={prox}&local=principal").text   # escalado na matriz
    g.get(f"/painel/escala?semana={sem}&local={fid}")
    assert "Duda Matriz" not in g.get(f"/painel/escala?semana={sem}").text      # o filtro fica guardado
    tela = g.get(f"/painel/escala?semana={sem}&local=").text
    assert "Caio Filial" in tela and "Duda Matriz" in tela


if __name__ == "__main__":
    main()
