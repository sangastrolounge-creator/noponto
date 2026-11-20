"""Cerca virtual.

- Contorno desenhado no mapa para o local principal e para cada local extra. Com contorno, o ponto
  vale dentro dele; sem contorno, vale o raio em volta do ponto do mapa.
- Batida fora da área (quando a empresa não bloqueia): o funcionário informa o motivo, que vai
  junto no aviso ao gestor.
- Conferência com o app aberto durante o turno (opcional, desligada por padrão): enquanto a pessoa
  está com o ponto aberto E com o app aberto na tela, o celular confere a área de tempos em tempos;
  fora dela por X minutos, o gestor recebe um aviso. Nada é lido com o app fechado nem fora do turno
  (os navegadores não permitem e a política de privacidade não prevê)."""
import json
import math
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import push, regras
from . import db as banco
from .core import avisar, cerca_liberada, exigir_csrf, gestor_logado, ir, render, usuario_logado
from .db import Alerta, Local, Usuario, get_db

rota = APIRouter()
CSRF = [Depends(exigir_csrf)]
MAX_PONTOS = 40


def pontos(txt: str) -> list[tuple[float, float]]:
    try:
        lst = json.loads(txt or "[]")
        return [(float(a), float(b)) for a, b in lst][:MAX_PONTOS] if len(lst) >= 3 else []
    except (ValueError, TypeError):
        return []


def _xy(lat0: float, lat: float, lng: float) -> tuple[float, float]:
    """Metros num plano local (equiretangular), bom para áreas pequenas."""
    k = 111320.0
    return lng * k * math.cos(math.radians(lat0)), lat * k


def dentro_poligono(lat: float, lng: float, poli: list[tuple[float, float]]) -> bool:
    x, y = lng, lat
    dentro = False
    n = len(poli)
    for i in range(n):
        y1, x1 = poli[i]
        y2, x2 = poli[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xc:
                dentro = not dentro
    return dentro


def distancia_borda(lat: float, lng: float, poli: list[tuple[float, float]]) -> int:
    """Distância em metros até a borda mais próxima do contorno."""
    px, py = _xy(lat, lat, lng)
    melhor = float("inf")
    n = len(poli)
    for i in range(n):
        ax, ay = _xy(lat, *poli[i])
        bx, by = _xy(lat, *poli[(i + 1) % n])
        dx, dy = bx - ax, by - ay
        t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        melhor = min(melhor, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return round(melhor)


def medir(lat: float, lng: float, lugar: tuple) -> tuple[int, bool]:
    """(distância em metros, está dentro?) para um lugar (nome, lat, lng, raio, cerca)."""
    _, clat, clng, raio, poli = lugar
    if poli:
        if dentro_poligono(lat, lng, poli):
            return 0, True
        return distancia_borda(lat, lng, poli), False
    d = round(regras.distancia_m(lat, lng, clat, clng))
    return d, d <= raio


# ------------------------------------------------------------------ editor no mapa
def _alvo(db: Session, u: Usuario, alvo: str):
    e = u.empresa
    if alvo == "principal":
        return e, e.nome
    l = db.get(Local, int(alvo)) if alvo.isdigit() else None
    if not l or l.empresa_id != e.id:
        return None, ""
    return l, l.nome


@rota.get("/painel/cerca/{alvo}")
def editor(request: Request, alvo: str, u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    obj, nome = _alvo(db, u, alvo)
    if obj is None:
        return ir("/painel/locais")
    if not cerca_liberada(u.empresa):
        avisar(request, "Seu plano não inclui a cerca virtual. Veja os planos em Assinatura.", "erro")
        return ir("/painel/locais")
    if obj.lat is None:
        avisar(request, "Marque primeiro o ponto do local no mapa (latitude e longitude).", "erro")
        return ir("/painel/config" if alvo == "principal" else "/painel/locais")
    return render(request, "painel_cerca.html", u=u, e=u.empresa, aba="locais", alvo=alvo, nome_local=nome,
                  centro=(obj.lat, obj.lng), raio=obj.raio_m, contorno=[list(p) for p in pontos(obj.cerca)])


@rota.post("/painel/cerca/{alvo}", dependencies=CSRF)
def salvar(request: Request, alvo: str, contorno: str = Form(""), u: Usuario = Depends(gestor_logado),
           db: Session = Depends(get_db)):
    obj, nome = _alvo(db, u, alvo)
    if obj is None:
        return ir("/painel/locais")
    try:
        lst = json.loads(contorno or "[]")
        lst = [[round(float(a), 6), round(float(b), 6)] for a, b in lst]
    except (ValueError, TypeError):
        lst = None
    if lst is None or (lst and not 3 <= len(lst) <= MAX_PONTOS) or any(not (-90 <= a <= 90 and -180 <= b <= 180) for a, b in lst or []):
        avisar(request, "Desenhe a cerca com 3 a 40 pontos.", "erro")
        return ir(f"/painel/cerca/{alvo}")
    obj.cerca = json.dumps(lst) if lst else ""
    regras.registrar(db, u.empresa_id, u.nome, "cerca virtual",
                     f"{nome}: contorno com {len(lst)} pontos." if lst else f"{nome}: contorno apagado, vale o raio.")
    db.commit()
    avisar(request, "Cerca salva. O ponto passa a valer só dentro dela." if lst else "Contorno apagado: vale o raio de novo.")
    return ir("/painel/locais")


# ------------------------------------------------------------------ conferência durante o turno
@rota.post("/app/cerca", dependencies=CSRF)
def conferir(tarefas_bg: BackgroundTasks, lat: str = Form(""), lng: str = Form(""), precisao: str = Form(""),
             fora_desde_min: str = Form("0"), u: Usuario = Depends(usuario_logado), db: Session = Depends(get_db)):
    """O app (aberto, com o ponto aberto) manda a posição. Responde se está dentro; se estiver fora há
    pelo menos X minutos, avisa o gestor uma vez por turno."""
    e = u.empresa
    if not e.cerca_turno or not cerca_liberada(e):
        return {"ativo": False}
    aberta = regras.ponto_aberto(db, u)
    if not aberta:
        return {"ativo": False}
    try:
        la, ln = float(lat), float(lng)
        prec = float(precisao) if precisao else None
        minutos = int(float(fora_desde_min or 0))
    except ValueError:
        return {"ativo": True, "dentro": None}
    t = db.get(regras.Turno, aberta.turno_id) if aberta.turno_id else None
    ls = regras.lugares(db, e, t)
    if not ls:
        return {"ativo": True, "dentro": None}
    dist, dentro = min((medir(la, ln, l) for l in ls), key=lambda m: (not m[1], m[0]))
    if not dentro and prec and prec > 2 * max(dist, 50):
        return {"ativo": True, "dentro": None}          # GPS impreciso demais para afirmar
    aviso = False
    if not dentro and minutos >= e.cerca_turno_min and not db.scalar(select(Alerta.id).where(
            Alerta.usuario_id == u.id, Alerta.tipo == "fora_turno", Alerta.criado_em >= aberta.momento).limit(1)):
        texto = f"{u.nome} está a {dist} m da área há {minutos} min, com o ponto aberto."
        db.add(Alerta(empresa_id=e.id, usuario_id=u.id, tipo="fora_turno", turno_id=aberta.turno_id,
                      criado_em=banco.agora(), texto=texto))
        db.commit()
        tarefas_bg.add_task(push.avisar_gestores, e.id, f"Fora da área: {u.primeiro_nome}", texto, "/painel", "ponto")
        aviso = True
    return {"ativo": True, "dentro": dentro, "distancia": dist, "avisou": aviso,
            "intervalo_s": 120, "limite_min": e.cerca_turno_min}


@rota.post("/painel/config/cerca", dependencies=CSRF)
def regras_cerca(request: Request, motivo_fora: str = Form(""), cerca_turno: str = Form(""),
                 cerca_turno_min: str = Form("10"), u: Usuario = Depends(gestor_logado), db: Session = Depends(get_db)):
    e = u.empresa
    m = cerca_turno_min.strip()
    if not (m.isdigit() and 3 <= int(m) <= 120):
        avisar(request, "Use de 3 a 120 minutos.", "erro")
        return ir("/painel/locais#cerca")
    e.motivo_fora, e.cerca_turno, e.cerca_turno_min = bool(motivo_fora), bool(cerca_turno), int(m)
    regras.registrar(db, e.id, u.nome, "cerca virtual",
                     f"Motivo fora da área: {'sim' if e.motivo_fora else 'não'}; conferência no turno: "
                     f"{'ligada' if e.cerca_turno else 'desligada'} ({e.cerca_turno_min} min).")
    db.commit()
    avisar(request, "Regras da cerca salvas.")
    return ir("/painel/locais#cerca")
