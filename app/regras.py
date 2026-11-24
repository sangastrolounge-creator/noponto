"""Regras do negócio: horário local, distância, batida de ponto, situação do turno,
alertas, checklist e espelho de ponto."""
import hashlib
import hmac
import math
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as banco
from .db import (PASTA_FOTOS, Alerta, Aparelho, Ausencia, Batida, Empresa, EscalaFixa, Historico, Local,
                 Tarefa, TarefaFeita, Totem, Turno, Usuario)

VALIDA = Batida.anulada.is_(False)  # batida anulada não conta em lugar nenhum

# O "dia de trabalho" vira às 4h: quem fecha a casa de madrugada ainda está no dia anterior.
VIRADA_H = 4
FUSOS = [
    ("America/Sao_Paulo", "Brasília (GMT-3)"),
    ("America/Manaus", "Manaus (GMT-4)"),
    ("America/Rio_Branco", "Rio Branco (GMT-5)"),
    ("America/Noronha", "Fernando de Noronha (GMT-2)"),
]
DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


class ErroPonto(Exception):
    def __init__(self, mensagem: str, alertas: list[Alerta] | None = None, codigo: str = ""):
        super().__init__(mensagem)
        self.alertas = alertas or []
        self.codigo = codigo   # ex.: "precisa_motivo" (o app pede o motivo e manda de novo)


# ------------------------------------------------------------------ tempo
def fuso(e: Empresa) -> ZoneInfo:
    try:
        return ZoneInfo(e.fuso)
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


def para_local(e: Empresa, dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(fuso(e)).replace(tzinfo=None)


def para_utc(e: Empresa, dt: datetime) -> datetime:
    return dt.replace(tzinfo=fuso(e)).astimezone(timezone.utc).replace(tzinfo=None)


def agora_local(e: Empresa) -> datetime:
    return para_local(e, banco.agora())


def dia_de(dt_local: datetime) -> date:
    return (dt_local - timedelta(hours=VIRADA_H)).date()


def hoje(e: Empresa) -> date:
    return dia_de(agora_local(e))


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def intervalo(t: Turno) -> tuple[datetime, datetime]:
    """Início e fim do turno em horário local. Fim <= início = termina no dia seguinte."""
    ini = datetime.combine(t.data, hm(t.inicio))
    fim = datetime.combine(t.data, hm(t.fim))
    if fim <= ini:
        fim += timedelta(days=1)
    return ini, fim


def inicio_do_intervalo(t: Turno) -> datetime | None:
    """Hora combinada da saída para o intervalo, em horário local (None se não foi definida)."""
    if t.folga or not t.intervalo_inicio or not t.intervalo_min:
        return None
    ini, fim = intervalo(t)
    x = datetime.combine(t.data, hm(t.intervalo_inicio))
    if x <= ini:
        x += timedelta(days=1)  # turno que vira a noite
    return x if ini < x < fim else None


def intervalo_cabe(inicio: str, fim: str, saida: str, minutos: int) -> bool:
    """A saída para o intervalo, mais a duração dele, cabe dentro do turno?"""
    def m(s):
        return int(s[:2]) * 60 + int(s[3:])
    a, b, x = m(inicio), m(fim), m(saida)
    if b <= a:
        b += 1440
    if x <= a:
        x += 1440
    return a < x and x + minutos <= b


def minutos_turno(t: Turno) -> int:
    if t.folga:
        return 0
    ini, fim = intervalo(t)
    return max(0, int((fim - ini).total_seconds() // 60) - (t.intervalo_min or 0))


def fmt_min(m: int) -> str:
    return f"{m // 60}h{m % 60:02d}"


def distancia_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------------------------------------------ escala fixa
DIAS_A_FRENTE = 14  # até quando os turnos da escala fixa ficam criados


def copiar_para(destino, origem):
    """Copia horário, folga, intervalo, local e observação entre turno e regra fixa."""
    destino.folga, destino.inicio, destino.fim = origem.folga, origem.inicio, origem.fim
    destino.intervalo_min, destino.local_id, destino.obs = (
        origem.intervalo_min, origem.local_id, origem.obs)
    destino.so_principal = bool(origem.so_principal)
    destino.intervalo_inicio = origem.intervalo_inicio


def gerar_da_fixa(db: Session, e: Empresa, ini: date, fim: date) -> int:
    """Cria os turnos que faltam entre as datas, a partir da escala fixa. Nunca mexe no
    passado nem em dia que já tem turno (exceção marcada pelo gestor)."""
    ini = max(ini, hoje(e))
    if ini > fim:
        return 0
    fixas = list(db.scalars(select(EscalaFixa).join(Usuario, Usuario.id == EscalaFixa.usuario_id)
                            .where(EscalaFixa.empresa_id == e.id, Usuario.ativo.is_(True),
                                   Usuario.na_escala.is_(True))))
    if not fixas:
        return 0
    ultimo = dict(db.execute(select(Usuario.id, Usuario.desligado_em).where(
        Usuario.empresa_id == e.id, Usuario.desligado_em.is_not(None))).all())
    ja = {(t.usuario_id, t.data) for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(ini, fim)))}
    n, d = 0, ini
    while d <= fim:
        for r in fixas:
            if (r.dia_semana == d.weekday() and (r.usuario_id, d) not in ja
                    and not (ultimo.get(r.usuario_id) and d > ultimo[r.usuario_id])):
                t = Turno(empresa_id=e.id, usuario_id=r.usuario_id, data=d, fixa=True)
                copiar_para(t, r)
                db.add(t)
                n += 1
        d += timedelta(days=1)
    if n:
        db.commit()
    return n


def limpar_futuros(db: Session, usuario_ids: list[int], depois_de: date, dias_semana: set[int]):
    """Apaga os turnos futuros desses dias da semana (os que ainda não têm batida), para
    serem recriados pela escala fixa nova. Quem chama faz o commit."""
    com_batida = select(Batida.turno_id).where(Batida.turno_id.is_not(None))
    for t in db.scalars(select(Turno).where(
            Turno.usuario_id.in_(usuario_ids), Turno.data > depois_de,
            Turno.id.not_in(com_batida))):
        if t.data.weekday() in dias_semana:
            db.delete(t)


# ------------------------------------------------------------------ ponto
def ultima_batida(db: Session, u: Usuario) -> Batida | None:
    return db.scalar(select(Batida).where(Batida.usuario_id == u.id, VALIDA)
                     .order_by(Batida.momento.desc()).limit(1))


def ponto_aberto(db: Session, u: Usuario) -> Batida | None:
    """Devolve a entrada em aberto (sem saída), se houver uma nas últimas 18 h."""
    b = ultima_batida(db, u)
    if b and b.tipo == "entrada" and banco.agora() - b.momento < timedelta(hours=18):
        return b
    return None


def turno_para_entrada(db: Session, u: Usuario, agora_l: datetime) -> Turno | None:
    """Turno a que uma entrada pertence: do início menos 4 h até o fim."""
    d = agora_l.date()
    candidatos = []
    for t in db.scalars(select(Turno).where(
            Turno.usuario_id == u.id, Turno.folga.is_(False),
            Turno.data.between(d - timedelta(days=1), d + timedelta(days=1)))):
        ini, fim = intervalo(t)
        if ini - timedelta(hours=4) <= agora_l <= fim:
            candidatos.append((abs((agora_l - ini).total_seconds()), t))
    return min(candidatos, key=lambda c: c[0])[1] if candidatos else None


def _alertas_do_plano(e: Empresa) -> bool:
    """O plano da empresa inclui os alertas de atraso e de não comparecimento?"""
    from .core import alertas_liberados
    return alertas_liberados(e)


def _alerta(db: Session, u: Usuario, tipo: str, texto: str, turno_id: int | None = None) -> Alerta:
    a = Alerta(empresa_id=u.empresa_id, usuario_id=u.id, tipo=tipo, texto=texto,
               turno_id=turno_id, criado_em=banco.agora())
    db.add(a)
    return a


def registrar(db: Session, empresa_id: int, autor: str, acao: str, detalhe: str,
              usuario_id: int | None = None, dia: date | None = None):
    """Anota no histórico (trilha de auditoria). Quem chama faz o commit.
    `usuario_id` e `dia`: a pessoa e o dia de ponto afetados, quando houver."""
    db.add(Historico(empresa_id=empresa_id, autor=autor[:120], acao=acao[:40],
                     detalhe=detalhe[:500], criado_em=banco.agora(),
                     usuario_id=usuario_id, dia=dia))


def nome_aparelho(ua: str) -> str:
    ua = ua or ""
    so = next((n for chave, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                  ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux"))
               if chave in ua), "Aparelho")
    modelo = re.search(r"Android [0-9.]+; ([^;)]+?)(?: Build|[)])", ua)
    nav = next((n for chave, n in (("Edg/", "Edge"), ("SamsungBrowser", "Samsung Internet"),
                                   ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"),
                                   ("Safari", "Safari")) if chave in ua), "")
    partes = [so]
    if modelo and modelo.group(1).strip() != "K":
        partes.append(modelo.group(1).strip())
    if nav:
        partes.append(nav)
    return " · ".join(partes)[:80]


def hash_aparelho(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def conferir_aparelho(db: Session, u: Usuario, token: str, ua: str) -> list[Alerta]:
    """Um funcionário, um aparelho. O primeiro que ele usar fica autorizado; qualquer outro
    precisa do OK do gestor. Levanta ErroPonto se este aparelho não pode bater o ponto."""
    e = u.empresa
    if not e.aparelho_unico:
        return []
    if not token:
        raise ErroPonto("Não consegui identificar este aparelho. Recarregue a página e tente de novo.")
    h = hash_aparelho(token)
    agora_u = banco.agora()
    meus = list(db.scalars(select(Aparelho).where(Aparelho.usuario_id == u.id)))
    este = next((a for a in meus if a.token_hash == h), None)
    if este and este.situacao == "autorizado":
        este.ultimo_uso = agora_u
        return []
    dono = db.scalar(select(Aparelho).join(Usuario).where(
        Aparelho.token_hash == h, Aparelho.usuario_id != u.id, Aparelho.situacao == "autorizado",
        Usuario.empresa_id == e.id, Usuario.ativo.is_(True)))
    if dono:
        alertas = []
        if not db.scalar(select(Alerta.id).where(
                Alerta.usuario_id == u.id, Alerta.tipo == "aparelho",
                Alerta.criado_em >= agora_u - timedelta(minutes=10)).limit(1)):
            alertas.append(_alerta(db, u, "aparelho",
                                   f"{u.nome} tentou bater o ponto no aparelho de {dono.usuario.nome}."))
            db.commit()
        raise ErroPonto("Este aparelho está registrado para outro colaborador. "
                        "Bata o ponto no seu próprio celular.", alertas)
    if not any(a.situacao == "autorizado" for a in meus):  # primeiro aparelho dele
        if este:
            este.situacao, este.ultimo_uso = "autorizado", agora_u
        else:
            db.add(Aparelho(usuario_id=u.id, token_hash=h, nome=nome_aparelho(ua),
                            situacao="autorizado", criado_em=agora_u, ultimo_uso=agora_u))
        return []
    if este and este.situacao == "recusado":
        raise ErroPonto("O gestor não liberou este aparelho. Bata o ponto no seu celular autorizado.")
    alertas = []
    if not este:
        db.add(Aparelho(usuario_id=u.id, token_hash=h, nome=nome_aparelho(ua),
                        situacao="pendente", criado_em=agora_u))
        alertas.append(_alerta(db, u, "aparelho",
                               f"{u.nome} pediu a liberação de um novo aparelho ({nome_aparelho(ua)})."))
        db.commit()
    raise ErroPonto("Este não é o seu aparelho autorizado. Avisamos o gestor: assim que ele "
                    "liberar, é só bater o ponto de novo.", alertas)


def guardar_foto(b: Batida, foto: bytes):
    pasta = PASTA_FOTOS / str(b.empresa_id)
    pasta.mkdir(parents=True, exist_ok=True)
    b.foto = f"{b.id}-{secrets.token_hex(6)}.jpg"
    (pasta / b.foto).write_bytes(foto)


def limpar_fotos(db: Session) -> int:
    """Apaga as fotos mais antigas que o prazo de cada empresa (o registro da batida fica)."""
    n = 0
    for e in db.scalars(select(Empresa)):
        limite = banco.agora() - timedelta(days=max(1, e.fotos_dias))
        for b in db.scalars(select(Batida).where(
                Batida.empresa_id == e.id, Batida.foto != "", Batida.momento < limite)):
            (PASTA_FOTOS / str(e.id) / b.foto).unlink(missing_ok=True)
            b.foto = ""
            n += 1
    db.commit()
    return n


def lugares(db: Session, e: Empresa, t: Turno | None, u: Usuario | None = None) -> list[tuple]:
    """Lugares onde o ponto vale agora, como (nome, lat, lng, raio, contorno). Nome vazio = o
    endereço principal da empresa. Contorno (cerca desenhada) vazio = vale o raio. Turno com
    local definido só vale naquele local; sem ele, com "ponto na lotação" ligado, vale só o local
    da ficha da pessoa (principal ou filial)."""
    from .cerca import pontos as _pontos
    from .core import cerca_liberada
    liberada = cerca_liberada(e)

    def pontos(txt):
        return _pontos(txt) if liberada else []
    extras = [l for l in db.scalars(select(Local).where(
        Local.empresa_id == e.id, Local.ativo.is_(True)).order_by(Local.id))
        if l.lat is not None and l.lng is not None]
    principal = [("", e.lat, e.lng, e.raio_m, pontos(e.cerca))] if e.lat is not None and e.lng is not None else []
    if t and t.so_principal and not t.local_id and principal:
        return principal
    if t and t.local_id:
        do_turno = [(l.nome, l.lat, l.lng, l.raio_m, pontos(l.cerca)) for l in extras if l.id == t.local_id]
        if do_turno:
            return do_turno
    if u is not None and e.ponto_na_lotacao:
        if u.local_id:
            lotacao = [(l.nome, l.lat, l.lng, l.raio_m, pontos(l.cerca)) for l in extras if l.id == u.local_id]
            if lotacao:
                return lotacao
        elif principal:
            return principal
    return principal + [(l.nome, l.lat, l.lng, l.raio_m, pontos(l.cerca)) for l in extras]


def codigo_totem(t: Totem, desloc: int = 0) -> str:
    """Código de 6 dígitos do totem, que muda a cada minuto."""
    janela = int(banco.agora().replace(tzinfo=timezone.utc).timestamp() // 60) + desloc
    d = hmac.new(bytes.fromhex(t.segredo), str(janela).encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(d[:4], 'big') % 1_000_000:06d}"


def totem_do_codigo(db: Session, e: Empresa, codigo: str) -> Totem | None:
    """Totem da empresa que está mostrando esse código (vale o do minuto atual e o anterior)."""
    codigo = re.sub(r"\D", "", codigo or "")
    if len(codigo) != 6:
        return None
    for t in db.scalars(select(Totem).where(Totem.empresa_id == e.id)):
        if any(hmac.compare_digest(codigo, codigo_totem(t, d)) for d in (0, -1)):
            return t
    return None


def bater(db: Session, u: Usuario, lat: float | None, lng: float | None,
          precisao: float | None, aparelho: str = "", ua: str = "", foto: bytes | None = None,
          totem: Totem | None = None, no_totem: bool = False, motivo: str = "") -> tuple[Batida, list[Alerta]]:
    """Registra entrada ou saída (o que vier a seguir). Pelo celular confere o aparelho, a foto
    e a distância até um local da empresa; com o código do totem a presença já está provada;
    no próprio totem (no_totem, por PIN) não há aparelho nem foto para conferir."""
    e = u.empresa
    agora_u = banco.agora()
    agora_l = para_local(e, agora_u)
    if u.ponto_desde and agora_l.date() < u.ponto_desde:
        raise ErroPonto(f"Seu registro de ponto começa em {u.ponto_desde:%d/%m/%Y}. "
                        "Até lá você pode conhecer o app, mas o ponto ainda não é registrado.")
    alertas: list[Alerta] = [] if no_totem else conferir_aparelho(db, u, aparelho, ua)
    from .core import exige_foto, foto_liberada, gps_liberado
    if not foto_liberada(e):
        foto = None   # o plano não inclui foto: nada é guardado
    if not gps_liberado(e) and not totem:
        # o plano não inclui o ponto pelo celular com GPS: só pelo totem (PIN ou código)
        raise ErroPonto("Na sua empresa o ponto é batido no totem (o tablet da empresa): use o seu PIN nele ou digite aqui o código que aparece na tela.")
    if exige_foto(e) and not foto and not no_totem:
        raise ErroPonto("A foto é obrigatória para bater o ponto. Permita o uso da câmera e tente de novo.")
    aberta = ponto_aberto(db, u)
    if aberta:
        t = db.get(Turno, aberta.turno_id) if aberta.turno_id else None
    else:
        t = turno_para_entrada(db, u, agora_l)
    # Portaria 671 (art. 74, I): o sistema não pode restringir o horário da marcação. A entrada muito
    # antes do turno é registrada e só fica sinalizada para o gestor decidir no tratamento do ponto.
    antecipada = 0
    if (not aberta and t and e.entrada_antecipada_min
            and agora_l < intervalo(t)[0] - timedelta(minutes=e.entrada_antecipada_min)):
        antecipada = int((intervalo(t)[0] - agora_l).total_seconds() // 60)

    dist = dentro = None
    local_nome, origem, onde = "", "", "da empresa"
    if totem:
        dentro, origem = True, "totem" if no_totem else "codigo"
        local_nome = totem.local.nome if totem.local else ""
        lat = lng = precisao = None
    else:
        ls = lugares(db, e, t, u) if lat is not None and lng is not None else []
        if ls:
            from .cerca import medir
            medidos = [(medir(lat, lng, l), l) for l in ls]
            (dist, dentro), (local_nome, _, _, raio, poli) = min(medidos, key=lambda m: (not m[0][1], m[0][0]))
            if local_nome:
                onde = f"do local {local_nome}"
            if not dentro and e.bloquear_fora:
                recente = db.scalar(select(Alerta).where(
                    Alerta.usuario_id == u.id, Alerta.tipo == "fora_area",
                    Alerta.criado_em >= agora_u - timedelta(minutes=10)))
                if not recente:
                    alertas.append(_alerta(db, u, "fora_area",
                                           f"{u.nome} tentou bater o ponto a {dist} m {onde}."))
                    db.commit()
                msg = (f"Você está a {dist} m da área {onde}. Entre na área e tente de novo." if poli else
                       f"Você está a {dist} m {onde}. O ponto só vale a até "
                       f"{raio} m. Chegue mais perto e tente de novo.")
                if precisao and precisao > 100:
                    msg += f" (O GPS está impreciso, cerca de {round(precisao)} m: tente ao ar livre.)"
                raise ErroPonto(msg, alertas)
            motivo = (motivo or "").strip()[:200]
            if not dentro and e.motivo_fora and not motivo:
                raise ErroPonto(f"Você está a {dist} m {onde}. Para bater o ponto fora da área, informe o motivo.",
                                codigo="precisa_motivo")

    ultima = ultima_batida(db, u)
    if ultima and (agora_u - ultima.momento).total_seconds() < 60:
        raise ErroPonto("Seu ponto já foi registrado há instantes.")
    b = Batida(empresa_id=e.id, usuario_id=u.id, momento=agora_u, lat=lat, lng=lng,
               precisao_m=round(precisao) if precisao is not None else None,
               distancia_m=dist, dentro=dentro, local_nome=local_nome, origem=origem,
               motivo_fora=(motivo or "").strip()[:200] if dentro is False else "",
               obs=f"totem {totem.nome}"[:200] if totem else "")
    tol = e.tolerancia_min

    if aberta:
        b.tipo, b.dia, b.turno_id = "saida", aberta.dia, aberta.turno_id
        direto = int((agora_u - aberta.momento).total_seconds() // 60)
        if (e.intervalo_obrigatorio and t and t.intervalo_min and direto > 360
                and not db.scalar(select(Batida.id).where(
                    Batida.turno_id == t.id, Batida.tipo == "saida", VALIDA).limit(1))):
            alertas.append(_alerta(db, u, "sem_intervalo",
                                   f"{u.nome} trabalhou {fmt_min(direto)} direto, sem bater a "
                                   f"saída e a volta do intervalo.", t.id))
        if t:
            falta = int((intervalo(t)[1] - agora_l).total_seconds() // 60)
            if falta > tol:
                alertas.append(_alerta(db, u, "saida_antecipada",
                                       f"{u.nome} saiu {fmt_min(falta)} antes do fim do turno "
                                       f"(previsto {t.fim}).", t.id))
    else:
        b.tipo = "entrada"
        if t:
            b.dia, b.turno_id = t.data, t.id
            primeira = not db.scalar(select(Batida.id).where(
                Batida.turno_id == t.id, VALIDA).limit(1))
            if antecipada:
                b.conferida = False
                alertas.append(_alerta(
                    db, u, "antecipada",
                    f"{u.nome} bateu a entrada às {agora_l:%H:%M}, {fmt_min(antecipada)} antes do turno "
                    f"({t.inicio}). Decida em Problemas de ponto se o tempo antes conta.", t.id))
            if primeira:
                b.atraso_min = max(0, int((agora_l - intervalo(t)[0]).total_seconds() // 60))
                if b.atraso_min > tol and _alertas_do_plano(u.empresa):
                    alertas.append(_alerta(
                        db, u, "atraso",
                        f"{u.nome} chegou {b.atraso_min} min atrasado "
                        f"(entrada às {t.inicio}, bateu às {agora_l:%H:%M}).", t.id))
        else:
            b.dia = dia_de(agora_l)
            alertas.append(_alerta(db, u, "sem_escala",
                                   f"{u.nome} bateu o ponto às {agora_l:%H:%M} sem estar na escala."))
    if dentro is False:
        alertas.append(_alerta(db, u, "fora_area",
                               f"{u.nome} bateu o ponto a {dist} m {onde}. Motivo: {b.motivo_fora}" if b.motivo_fora
                               else f"{u.nome} bateu o ponto a {dist} m {onde}.", b.turno_id))
    db.add(b)
    if foto:
        db.flush()
        guardar_foto(b, foto)
    from . import rep   # Portaria 671: grava a marcação no ARP com NSR e hash
    rep.registrar_marcacao(db, b, rep.COLETOR_DISPOSITIVO if no_totem else rep.COLETOR_WEB)
    db.commit()
    return b, alertas


def lancar_manual(db: Session, f: Usuario, tipo: str, local: datetime, obs: str) -> Batida:
    """Batida lançada pelo gestor (direto ou ao aprovar um pedido de ajuste)."""
    e = f.empresa
    momento = para_utc(e, local)
    b = Batida(empresa_id=e.id, usuario_id=f.id, tipo=tipo, momento=momento, manual=True,
               obs=obs[:200])
    if tipo == "saida":
        ent = db.scalar(select(Batida).where(
            Batida.usuario_id == f.id, Batida.tipo == "entrada", VALIDA,
            Batida.momento <= momento, Batida.momento >= momento - timedelta(hours=18))
            .order_by(Batida.momento.desc()).limit(1))
        b.dia, b.turno_id = (ent.dia, ent.turno_id) if ent else (dia_de(local), None)
    else:
        t = turno_para_entrada(db, f, local)
        b.dia, b.turno_id = (t.data, t.id) if t else (dia_de(local), None)
        if t and not db.scalar(select(Batida.id).where(Batida.turno_id == t.id, VALIDA).limit(1)):
            b.atraso_min = max(0, int((local - intervalo(t)[0]).total_seconds() // 60))
    db.add(b)
    return b


def situacao_turno(t: Turno, batidas: list[Batida], agora_l: datetime, tol: int) -> dict:
    """Situação de um turno para o painel do gestor."""
    if t.folga:
        return {"cod": "folga", "texto": "Folga", "cor": ""}
    ini, fim = intervalo(t)
    if not batidas:
        if agora_l <= ini + timedelta(minutes=tol):
            return {"cod": "aguardando", "texto": "Aguardando", "cor": ""}
        if agora_l < fim:
            return {"cod": "sem_ponto", "texto": "Não bateu o ponto", "cor": "erro"}
        return {"cod": "falta", "texto": "Faltou", "cor": "erro"}
    atraso = batidas[0].atraso_min if batidas[0].tipo == "entrada" else 0
    atrasado = atraso > tol
    if batidas[-1].tipo == "entrada":
        if agora_l > fim + timedelta(minutes=60):
            return {"cod": "sem_saida", "texto": "Sem saída registrada", "cor": "alerta"}
        return {"cod": "trabalhando",
                "texto": f"Trabalhando · atrasou {atraso} min" if atrasado else "Trabalhando",
                "cor": "alerta" if atrasado else "ok"}
    return {"cod": "encerrado",
            "texto": f"Encerrado · atrasou {atraso} min" if atrasado else "Encerrado",
            "cor": "alerta" if atrasado else "azul"}


def verificar_turnos(db: Session) -> tuple[list[Alerta], list[tuple[int, str, str]]]:
    """Roda a cada minuto. Devolve os alertas novos para o gestor (ponto não batido, saída
    esquecida) e os lembretes para os funcionários, como (usuario_id, título, texto)."""
    novos: list[Alerta] = []
    lembretes: list[tuple[int, str, str]] = []
    for e in db.scalars(select(Empresa)):
        al = agora_local(e)
        d = al.date()
        from . import desligamento
        desligamento.encerrar_vencidos(db, e.id, d)
        gerar_da_fixa(db, e, hoje(e), hoje(e) + timedelta(days=DIAS_A_FRENTE))
        turnos = db.scalars(select(Turno).join(Usuario).where(
            Turno.empresa_id == e.id, Turno.folga.is_(False), Usuario.ativo.is_(True),
            Turno.data.between(d - timedelta(days=1), d)))
        for t in turnos:
            ini, fim = intervalo(t)
            if al > fim + timedelta(hours=6):
                continue  # antigo demais para avisar
            if t.usuario.ponto_desde and t.data < t.usuario.ponto_desde:
                continue  # antes da vigência: o ponto ainda não é cobrado
            if db.scalar(select(Ausencia.id).where(
                    Ausencia.usuario_id == t.usuario_id, Ausencia.situacao == "aprovado",
                    Ausencia.inicio <= t.data, Ausencia.fim >= t.data).limit(1)):
                continue  # atestado, férias etc.: não cobra ponto
            bats = list(db.scalars(select(Batida).where(Batida.turno_id == t.id, VALIDA)
                                   .order_by(Batida.momento)))
            tol = timedelta(minutes=e.tolerancia_min)
            if not bats and not t.lembrou_ini and ini - timedelta(minutes=5) <= al <= ini + tol:
                t.lembrou_ini = True
                lembretes.append((t.usuario_id, "Hora de bater o ponto",
                                  f"Seu turno começa às {t.inicio}."))
            pausa_em = inicio_do_intervalo(t)
            if (pausa_em and not t.lembrou_int and len(bats) == 1 and bats[0].tipo == "entrada"
                    and pausa_em <= al <= pausa_em + timedelta(minutes=15)):
                t.lembrou_int = True
                lembretes.append((t.usuario_id, "Hora do intervalo",
                                  f"Seu intervalo de {t.intervalo_min} min começa às "
                                  f"{t.intervalo_inicio}. Bata a saída ao parar."))
            if (bats and bats[-1].tipo == "entrada" and not t.lembrou_fim
                    and fim <= al <= fim + timedelta(minutes=60)):
                t.lembrou_fim = True
                lembretes.append((t.usuario_id, "Fim do turno",
                                  f"Seu turno terminou às {t.fim}. Não esqueça de bater a saída."))
            tipo = texto = None
            if (not bats and al > ini + timedelta(minutes=e.tolerancia_min)
                    and _alertas_do_plano(e)):
                tipo = "sem_ponto"
                texto = f"{t.usuario.nome} não bateu o ponto (entrada às {t.inicio})."
            elif bats and bats[-1].tipo == "entrada" and al > fim + timedelta(minutes=min(e.saida_auto_min or 60, 300)):
                # Portaria 671 (art. 74, II): nada de saída automática. Avisa e o gestor lança.
                tipo = "sem_saida"
                texto = (f"{t.usuario.nome} não registrou a saída (turno terminou às {t.fim}). "
                         "Lance em Problemas de ponto.")
            if tipo and not db.scalar(select(Alerta.id).where(
                    Alerta.turno_id == t.id, Alerta.tipo == tipo).limit(1)):
                novos.append(_alerta(db, t.usuario, tipo, texto, t.id))
                if tipo == "sem_ponto":
                    lembretes.append((t.usuario_id, "Você ainda não bateu o ponto",
                                      f"Seu turno começou às {t.inicio}."))
                else:
                    lembretes.append((t.usuario_id, "Você não bateu a saída",
                                      f"Seu turno terminou às {t.fim}. Bata a saída agora ou, se já saiu, "
                                      "peça um ajuste no app."))
    db.commit()
    return novos, lembretes


# ------------------------------------------------------------------ checklist
def tarefas_do_dia(db: Session, e: Empresa, dia: date, u: Usuario | None = None) -> list[dict]:
    """Tarefas do dia com quem fez. Com `u`, só as da equipe toda e as dele."""
    filtros = [Tarefa.empresa_id == e.id, Tarefa.ativo.is_(True),
               Tarefa.dias.contains(str(dia.weekday()))]
    if u:
        filtros.append((Tarefa.usuario_id.is_(None)) | (Tarefa.usuario_id == u.id))
    tarefas = list(db.scalars(select(Tarefa).where(*filtros).order_by(Tarefa.ordem, Tarefa.id)))
    feitas = {f.tarefa_id: f for f in db.scalars(select(TarefaFeita).where(
        TarefaFeita.empresa_id == e.id, TarefaFeita.data == dia))}
    return [{"t": t, "feita": feitas.get(t.id)} for t in tarefas]


# ------------------------------------------------------------------ espelho
def espelho(db: Session, e: Empresa, ini: date, fim: date, usuario_id: int | None = None
            ) -> tuple[list[dict], list[dict]]:
    """Linhas por funcionário/dia (entradas, saídas, horas) e totais por funcionário."""
    filtros = [Batida.empresa_id == e.id, Batida.dia.between(ini, fim), VALIDA]
    if usuario_id:
        filtros.append(Batida.usuario_id == usuario_id)
    grupos: dict[tuple[int, date], list[Batida]] = {}
    for b in db.scalars(select(Batida).where(*filtros).order_by(Batida.momento)):
        grupos.setdefault((b.usuario_id, b.dia), []).append(b)
    turnos = {(t.usuario_id, t.data): t for t in db.scalars(select(Turno).where(
        Turno.empresa_id == e.id, Turno.data.between(ini, fim)))}

    linhas, totais = [], {}
    for (uid, dia), bats in grupos.items():
        pares, aberta, minutos = [], None, 0
        for b in bats:
            if b.tipo == "entrada":
                if aberta:
                    pares.append((aberta, None))
                aberta = b
            else:
                pares.append((aberta, b))
                if aberta:
                    minutos += int((b.momento - aberta.momento).total_seconds() // 60)
                aberta = None
        if aberta:
            pares.append((aberta, None))
        usuario = bats[0].usuario
        t = turnos.get((uid, dia))
        linha = {"u": usuario, "dia": dia, "pares": pares, "minutos": minutos,
                 "turno": t, "atraso": max(b.atraso_min for b in bats),
                 "fora": any(b.dentro is False for b in bats),
                 "andamento": False}
        # Entrada recente ainda sem saída = está trabalhando, não é batida faltando.
        if pares[-1][0] and not pares[-1][1] and                 banco.agora() - pares[-1][0].momento < timedelta(hours=18):
            linha["andamento"] = True
        fechados = pares[:-1] if linha["andamento"] else pares
        linha["incompleto"] = any(p[0] is None or p[1] is None for p in fechados)
        linhas.append(linha)
        tot = totais.setdefault(uid, {"u": usuario, "dias": 0, "minutos": 0, "atrasos": 0,
                                      "previsto": 0})
        tot["dias"] += 1
        tot["minutos"] += minutos
        tot["atrasos"] += 1 if linha["atraso"] > e.tolerancia_min else 0
    for (uid, _), t in turnos.items():
        if uid in totais:
            totais[uid]["previsto"] += minutos_turno(t)
    linhas.sort(key=lambda l: (-l["dia"].toordinal(), l["u"].nome.lower()))
    return linhas, sorted(totais.values(), key=lambda t: t["u"].nome.lower())
