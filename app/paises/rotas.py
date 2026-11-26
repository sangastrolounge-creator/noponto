"""Telas inteiras que existem numa versão por país.

Quando a tela de um país é diferente demais para caber na mesma rota (a folha brasileira e o
pagamento bruto americano, por exemplo), a versão do país fica registrada com o prefixo
/<país> (ex.: /eua/painel/pagamento) e este middleware manda para ela quem é de uma empresa
daquele país. Os endereços que o usuário vê continuam os mesmos para todo mundo, e ninguém
chega ao prefixo direto pela barra de endereço."""
import anyio

# país -> (prefixo interno, endereços que têm versão própria)
TELAS = {"US": ("/eua", ("/painel/pagamento", "/app/holerite"))}
INTERNOS = tuple(p for p, _ in TELAS.values())


def _pais_do_usuario(uid) -> str:
    from ..db import SessionLocal, Usuario
    with SessionLocal() as db:
        u = db.get(Usuario, uid)
        return (u.empresa.pais or "BR") if u and u.empresa else "BR"


def _casa(caminho: str, base: str) -> bool:
    return caminho == base or caminho.startswith(base + "/")


class RotasPorPais:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            caminho = scope["path"]
            if any(_casa(caminho, p) for p in INTERNOS):
                scope = {**scope, "path": "/nao-encontrado" + caminho, "raw_path": b"/nao-encontrado"}
            else:
                uid = (scope.get("session") or {}).get("uid")
                if uid:
                    for pais, (prefixo, enderecos) in TELAS.items():
                        if any(_casa(caminho, e) for e in enderecos):
                            if await anyio.to_thread.run_sync(_pais_do_usuario, uid) == pais:
                                novo = prefixo + caminho
                                scope = {**scope, "path": novo, "raw_path": novo.encode(),
                                         "caminho_pedido": caminho}
                            break
        await self.app(scope, receive, send)
