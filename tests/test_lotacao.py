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
    print("ok: ponto no local de cada pessoa")


if __name__ == "__main__":
    main()
