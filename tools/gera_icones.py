"""Gera os ícones PNG do app (192 e 512) sem depender de biblioteca de imagem.
Rodar da raiz: python tools/gera_icones.py"""
import struct
import zlib
from pathlib import Path

VERDE, BRANCO = (15, 118, 110), (255, 255, 255)


def dist_segmento(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def cor(x, y):
    """Coordenadas em 0..512. Fundo inteiro verde (serve como ícone 'maskable')."""
    if ((x - 256) ** 2 + (y - 256) ** 2) ** 0.5 > 150:
        return VERDE
    if min(dist_segmento(x, y, 256, 160, 256, 260),
           dist_segmento(x, y, 256, 260, 322, 304)) <= 17:
        return VERDE
    return BRANCO


def png(tamanho: int) -> bytes:
    amostras = 3  # suaviza as bordas
    linhas = bytearray()
    for j in range(tamanho):
        linhas.append(0)
        for i in range(tamanho):
            r = g = b = 0
            for sj in range(amostras):
                for si in range(amostras):
                    c = cor((i + (si + .5) / amostras) * 512 / tamanho,
                            (j + (sj + .5) / amostras) * 512 / tamanho)
                    r, g, b = r + c[0], g + c[1], b + c[2]
            n = amostras * amostras
            linhas += bytes((r // n, g // n, b // n))

    def bloco(tipo: bytes, dados: bytes) -> bytes:
        return (struct.pack(">I", len(dados)) + tipo + dados
                + struct.pack(">I", zlib.crc32(tipo + dados)))

    return (b"\x89PNG\r\n\x1a\n"
            + bloco(b"IHDR", struct.pack(">IIBBBBB", tamanho, tamanho, 8, 2, 0, 0, 0))
            + bloco(b"IDAT", zlib.compress(bytes(linhas), 9)) + bloco(b"IEND", b""))


if __name__ == "__main__":
    destino = Path(__file__).resolve().parent.parent / "app" / "static"
    for t in (192, 512):
        (destino / f"icone-{t}.png").write_bytes(png(t))
        print("ok", t)
