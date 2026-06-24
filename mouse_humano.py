"""
MURI PRO — Mouse Humano v3
============================
Movimento de mouse realista com modelagem comportamental avancada.

Curvas:
  - Bezier cubico (40%): curva suave classica
  - Bezier quadratico (30%): curva simples (movimentos diretos)
  - Multi-segmento (30%): 2-3 curvas encadeadas

Perfis de velocidade (easing espacial):
  - ease-in-out (50%): padrao
  - ease-out (25%): urgencia
  - ease-in (25%): hesitacao

Modelagem v3:
  - Two-phase ballistic + corretiva (Meyer/Woodworth) para dist > 150
  - Polling rate coerente por sessao (125/250/500/1000 Hz)
  - Tremor fisiologico AR(1) autocorrelacionado (~8-12 Hz)
  - Fitts law: sigma sub-linear + num_passos com indice de dificuldade
  - Gap inter-movimento contextual (corretivo / decisao / reorientacao)
  - PointerEvent completo via CDP (force, tilt, twist, pointerType=mouse)
  - Estado por pagina (CursorState) — suporta multiplas abas em paralelo

Todas interacoes de mouse (move, down, up, wheel) passam por
Input.dispatchMouseEvent via CDP, com fallback para page.mouse.*.
"""

import asyncio
import math
import random
import time


# ══════════════════════════════════════════════════════════════════════════════
#  ESTADO POR PAGINA — CursorState
# ══════════════════════════════════════════════════════════════════════════════

class CursorState:
    """
    Estado do cursor por pagina. Anexado como page._muri_cursor via
    lazy init em _get_state(page). Substitui as antigas variaveis globais.

    Campos:
      x, y               — posicao atual
      tremor_x, tremor_y — estado persistente do AR(1)
      last_move_end_ts   — timestamp do ultimo move (pra gap contextual)
      poll_hz            — frequencia de polling (125/250/500/1000 Hz)
      poll_interval      — 1/poll_hz em segundos
      _cdp_session       — sessao CDP cacheada
      lock               — asyncio.Lock pra mover() concorrente
    """

    __slots__ = (
        "x", "y",
        "tremor_x", "tremor_y",
        "last_move_end_ts",
        "poll_hz", "poll_interval",
        "_cdp_session",
        "lock",
    )

    def __init__(self):
        self.x = float(random.randint(300, 700))
        self.y = float(random.randint(200, 500))
        self.tremor_x = 0.0
        self.tremor_y = 0.0
        self.last_move_end_ts = 0.0
        # polling rate sorteado por sessao — coerencia estatistica do histograma dt
        self.poll_hz = random.choices(
            [125, 250, 500, 1000], weights=[20, 30, 35, 15]
        )[0]
        self.poll_interval = 1.0 / self.poll_hz
        self._cdp_session = None
        self.lock = asyncio.Lock()

    def tick_tremor(self, intensidade: float = 1.0, rho: float = 0.85):
        """
        Tremor fisiologico AR(1) — autocorrelacionado.
        rho alto = memoria longa (picos em baixa freq, humano-like).
        Retorna delta (x, y) pra somar na posicao ideal.
        """
        sigma = 0.4 * intensidade * math.sqrt(max(0.0, 1 - rho * rho))
        self.tremor_x = rho * self.tremor_x + random.gauss(0, sigma)
        self.tremor_y = rho * self.tremor_y + random.gauss(0, sigma)
        return self.tremor_x, self.tremor_y


def _get_state(page) -> CursorState:
    """Lazy init do CursorState por pagina."""
    st = getattr(page, "_muri_cursor", None)
    if st is None:
        st = CursorState()
        try:
            page._muri_cursor = st
        except Exception:
            pass
    return st


# ══════════════════════════════════════════════════════════════════════════════
#  CDP DISPATCH — PointerEvent completo (force/tilt/twist/pointerType)
# ══════════════════════════════════════════════════════════════════════════════

async def _get_cdp(page, state: CursorState):
    """
    Pega/cria sessao CDP. Cacheia em page._muri_cdp (browser_engine
    tambem usa esse slot) e em state._cdp_session.
    """
    cdp = getattr(page, "_muri_cdp", None) or state._cdp_session
    if cdp is None:
        try:
            cdp = await page.context.new_cdp_session(page)
        except Exception:
            return None
    try:
        page._muri_cdp = cdp
    except Exception:
        pass
    state._cdp_session = cdp
    return cdp


async def _dispatch_mouse(
    page, state: CursorState, type_: str, x: float, y: float,
    button: str = "none", buttons: int = 0,
    force: float = 0.0, click_count: int = 0,
):
    """
    Envia Input.dispatchMouseEvent via CDP com PointerEvent completo.
    Fallback automatico pra page.mouse.* se CDP falhar.

    type_: mouseMoved | mousePressed | mouseReleased
    """
    cdp = await _get_cdp(page, state)
    if cdp is not None:
        try:
            params = {
                "type": type_,
                "x": x, "y": y,
                "button": button,
                "buttons": buttons,
                "pointerType": "mouse",
                "force": force,
                "tangentialPressure": 0.0,
                "tiltX": 0, "tiltY": 0,
                "twist": 0,
            }
            if click_count > 0:
                params["clickCount"] = click_count
            await cdp.send("Input.dispatchMouseEvent", params)
            return
        except Exception:
            pass
    # fallback Playwright
    try:
        if type_ == "mouseMoved":
            await page.mouse.move(x, y)
        elif type_ == "mousePressed":
            await page.mouse.down(button=button if button != "none" else "left")
        elif type_ == "mouseReleased":
            await page.mouse.up(button=button if button != "none" else "left")
    except Exception:
        pass


async def _dispatch_wheel(
    page, state: CursorState, x: float, y: float,
    delta_x: float = 0.0, delta_y: float = 0.0,
):
    """mouseWheel via CDP — ancorado em (x, y). Fallback pra page.mouse.wheel."""
    cdp = await _get_cdp(page, state)
    if cdp is not None:
        try:
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": x, "y": y,
                "button": "none", "buttons": 0,
                "pointerType": "mouse",
                "force": 0.0,
                "deltaX": delta_x, "deltaY": delta_y,
            })
            return
        except Exception:
            pass
    try:
        await page.mouse.wheel(delta_x, delta_y)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  CURVAS — 3 modelos (geometria pura)
# ══════════════════════════════════════════════════════════════════════════════

def _distancia(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _bezier_cubica(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
    )


def _bezier_quadratica(p0, p1, p2, t):
    u = 1 - t
    return (
        u**2 * p0[0] + 2 * u * t * p1[0] + t**2 * p2[0],
        u**2 * p0[1] + 2 * u * t * p1[1] + t**2 * p2[1],
    )


def _perpendicular(dx, dy):
    norm = max(math.sqrt(dx * dx + dy * dy), 1)
    return -dy / norm, dx / norm


def _gerar_pontos_cubica(origem, destino, num_passos):
    dx = destino[0] - origem[0]
    dy = destino[1] - origem[1]
    dist = _distancia(origem, destino)
    perp_x, perp_y = _perpendicular(dx, dy)

    desvio = dist * random.uniform(0.1, 0.35)
    lado = random.choice([-1, 1])

    t1 = random.uniform(0.2, 0.4)
    p1 = (
        origem[0] + dx * t1 + perp_x * desvio * lado + random.uniform(-5, 5),
        origem[1] + dy * t1 + perp_y * desvio * lado + random.uniform(-5, 5),
    )
    t2 = random.uniform(0.6, 0.8)
    desvio2 = desvio * random.uniform(0.3, 0.8) * lado
    p2 = (
        origem[0] + dx * t2 + perp_x * desvio2 + random.uniform(-3, 3),
        origem[1] + dy * t2 + perp_y * desvio2 + random.uniform(-3, 3),
    )

    pontos = []
    for i in range(num_passos + 1):
        t = i / num_passos
        pontos.append(_bezier_cubica(origem, p1, p2, destino, t))
    return pontos


def _gerar_pontos_quadratica(origem, destino, num_passos):
    dx = destino[0] - origem[0]
    dy = destino[1] - origem[1]
    dist = _distancia(origem, destino)
    perp_x, perp_y = _perpendicular(dx, dy)

    desvio = dist * random.uniform(0.05, 0.25)
    lado = random.choice([-1, 1])
    tc = random.uniform(0.35, 0.65)
    ctrl = (
        origem[0] + dx * tc + perp_x * desvio * lado + random.uniform(-3, 3),
        origem[1] + dy * tc + perp_y * desvio * lado + random.uniform(-3, 3),
    )

    pontos = []
    for i in range(num_passos + 1):
        t = i / num_passos
        pontos.append(_bezier_quadratica(origem, ctrl, destino, t))
    return pontos


def _gerar_pontos_multi(origem, destino, num_passos):
    dx = destino[0] - origem[0]
    dy = destino[1] - origem[1]
    dist = _distancia(origem, destino)
    perp_x, perp_y = _perpendicular(dx, dy)

    num_seg = random.choice([2, 2, 3])
    waypoints = [origem]
    for s in range(1, num_seg):
        frac = s / num_seg
        desvio = dist * random.uniform(0.05, 0.20) * random.choice([-1, 1])
        wp = (
            origem[0] + dx * frac + perp_x * desvio + random.uniform(-8, 8),
            origem[1] + dy * frac + perp_y * desvio + random.uniform(-8, 8),
        )
        waypoints.append(wp)
    waypoints.append(destino)

    passos_por_seg = max(5, num_passos // num_seg)
    pontos = []
    for i in range(len(waypoints) - 1):
        seg_origem = waypoints[i]
        seg_destino = waypoints[i + 1]
        seg_dx = seg_destino[0] - seg_origem[0]
        seg_dy = seg_destino[1] - seg_origem[1]
        seg_dist = _distancia(seg_origem, seg_destino)
        seg_perp_x, seg_perp_y = _perpendicular(seg_dx, seg_dy)

        desvio_seg = seg_dist * random.uniform(0.05, 0.15) * random.choice([-1, 1])
        ctrl = (
            seg_origem[0] + seg_dx * 0.5 + seg_perp_x * desvio_seg,
            seg_origem[1] + seg_dy * 0.5 + seg_perp_y * desvio_seg,
        )
        start = 0 if i == 0 else 1  # evita ponto duplicado na juncao
        for j in range(start, passos_por_seg + 1):
            t = j / passos_por_seg
            pontos.append(_bezier_quadratica(seg_origem, ctrl, seg_destino, t))

    return pontos


# ══════════════════════════════════════════════════════════════════════════════
#  EASING ESPACIAL — perfil de velocidade vive na distribuicao dos pontos
# ══════════════════════════════════════════════════════════════════════════════

def _ease_in_out(t):
    return t * t * (3 - 2 * t)


def _ease_out(t):
    return 1 - (1 - t) ** 2.5


def _ease_in(t):
    return t ** 2.5


def _escolher_ease():
    r = random.random()
    if r < 0.50:
        return _ease_in_out
    elif r < 0.75:
        return _ease_out
    else:
        return _ease_in


def _aplicar_ease(pontos, ease_fn):
    """
    Redistribui pontos segundo o perfil de velocidade (ease espacial).
    Pontos ficam mais densos onde o movimento e lento. O loop de emissao
    usa delay constante (polling rate) — toda variacao de velocidade vive
    aqui na distribuicao espacial.
    """
    n = len(pontos) - 1
    if n <= 0:
        return pontos
    resultado = []
    for i in range(n + 1):
        t_linear = i / n
        t_eased = ease_fn(t_linear)
        if 0 < i < n:
            t_eased += random.uniform(-0.008, 0.008)
            t_eased = max(0.0, min(1.0, t_eased))
        idx_float = t_eased * n
        # clampa idx em [0, n-1] pra interpolacao nunca estourar
        idx = min(int(idx_float), n - 1)
        frac = idx_float - idx
        x = pontos[idx][0] + (pontos[idx + 1][0] - pontos[idx][0]) * frac
        y = pontos[idx][1] + (pontos[idx + 1][1] - pontos[idx][1]) * frac
        resultado.append((x, y))
    return resultado


# ══════════════════════════════════════════════════════════════════════════════
#  OVERSHOOT (helper — usado como variante ~20% da fase corretiva)
# ══════════════════════════════════════════════════════════════════════════════

def _calcular_overshoot(destino, direcao_dx, direcao_dy, dist):
    overshoot_dist = min(dist * random.uniform(0.02, 0.06), 15)
    norm = max(math.sqrt(direcao_dx * direcao_dx + direcao_dy * direcao_dy), 1)
    return (
        destino[0] + (direcao_dx / norm) * overshoot_dist,
        destino[1] + (direcao_dy / norm) * overshoot_dist,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  EMISSAO DE PONTOS — polling constante + tremor AR(1)
# ══════════════════════════════════════════════════════════════════════════════

async def _percorrer_pontos(page, state: CursorState, pontos, tremor_intensidade: float = 1.0):
    """
    Emite mouseMoved pra cada ponto via CDP com:
      - tremor AR(1) somado na posicao ideal
      - intervalo de sleep = 1/poll_hz + gauss pequena (coerencia de histograma dt)
      - hesitacao ocasional (~3%) — distribuicao de caudas longas
    """
    for (px, py) in pontos:
        tx, ty = state.tick_tremor(intensidade=tremor_intensidade)
        x = px + tx
        y = py + ty
        await _dispatch_mouse(page, state, "mouseMoved", x, y)
        state.x = x
        state.y = y
        interval = max(0.0005, state.poll_interval + random.gauss(0, 0.0005))
        if random.random() < 0.03:
            interval += random.uniform(0.03, 0.08)
        await asyncio.sleep(interval)


# ══════════════════════════════════════════════════════════════════════════════
#  API PUBLICA — mover()
# ══════════════════════════════════════════════════════════════════════════════

async def mover(page, destino_x, destino_y, origem_x=None, origem_y=None,
                target_width=None):
    """
    Movimento humanizado ate (destino_x, destino_y).

    Parametros:
      origem_x, origem_y — default: posicao atual do cursor no state
      target_width       — dimensao do alvo (usado pelo indice de Fitts).
                           Opcional; melhora perfil em alvos pequenos.

    Comportamento:
      - Gap inter-movimento contextual antes de comecar
      - Fitts: num_passos cresce com log2(dist/width + 1)
      - dist > 150: two-phase ballistic + corretiva (Meyer/Woodworth)
          * 20% das corretivas incluem overshoot
      - dist <= 150: curva unica sorteada + ease espacial
      - Loop emite mouseMoved via CDP (force=0, pointerType=mouse)
      - Tremor AR(1) persistente por sessao
    """
    state = _get_state(page)

    async with state.lock:
        # ── gap inter-movimento contextual ──
        if state.last_move_end_ts > 0:
            gap = time.time() - state.last_move_end_ts
            if gap < 0.3:
                pausa = 0.0  # correcao continua, sem pausa
            elif gap < 2.0:
                pausa = random.uniform(0.05, 0.2)  # decisao rapida
            else:
                pausa = random.uniform(0.2, 0.6)  # reorientacao
            if pausa > 0:
                await asyncio.sleep(pausa)

        if origem_x is None:
            origem_x = state.x
        if origem_y is None:
            origem_y = state.y

        origem = (origem_x, origem_y)
        destino = (destino_x, destino_y)
        dist = _distancia(origem, destino)

        # movimento curtissimo — tick unico via CDP
        if dist < 5:
            await _dispatch_mouse(page, state, "mouseMoved", destino_x, destino_y)
            state.x = destino_x
            state.y = destino_y
            state.last_move_end_ts = time.time()
            return

        # ── num_passos com termo de Fitts ──
        if target_width is not None and target_width > 0:
            fitts_id = math.log2(dist / max(target_width, 1.0) + 1)
            extra_passos = int(fitts_id * 1.5)
        else:
            extra_passos = 0
        num_passos = max(
            15, min(70, int(dist / 8) + random.randint(-5, 5) + extra_passos)
        )

        # ── two-phase ballistic + corretiva pra movimentos longos ──
        if dist > 150:
            # FASE 1 — ballistic: origem → 78-88% do caminho, ease_out agressivo
            frac = random.uniform(0.78, 0.88)
            dx_total = destino[0] - origem[0]
            dy_total = destino[1] - origem[1]
            perp_x, perp_y = _perpendicular(dx_total, dy_total)
            intermed = (
                origem[0] + dx_total * frac + perp_x * random.uniform(-10, 10),
                origem[1] + dy_total * frac + perp_y * random.uniform(-10, 10),
            )
            ballistic_passos = max(10, int(num_passos * 0.7))
            pontos_b = _gerar_pontos_cubica(origem, intermed, ballistic_passos)
            pontos_b = _aplicar_ease(pontos_b, _ease_out)
            await _percorrer_pontos(page, state, pontos_b, tremor_intensidade=1.0)

            # pausa breve — feedback visual (olho confirma posicao)
            await asyncio.sleep(random.uniform(0.02, 0.06))

            # FASE 2 — corretiva: intermed → destino, ease_in_out suave
            correct_passos = max(8, num_passos - ballistic_passos)

            # ~20% das corretivas incluem overshoot (variante, nao mecanismo principal)
            if random.random() < 0.20:
                over = _calcular_overshoot(
                    destino, dx_total, dy_total,
                    _distancia(intermed, destino),
                )
                half = max(4, correct_passos // 2)
                pontos_over = _gerar_pontos_quadratica(intermed, over, half)
                pontos_over = _aplicar_ease(pontos_over, _ease_out)
                await _percorrer_pontos(page, state, pontos_over, tremor_intensidade=0.7)
                await asyncio.sleep(random.uniform(0.08, 0.18))
                pontos_c = _gerar_pontos_quadratica(over, destino, half)
            else:
                pontos_c = _gerar_pontos_quadratica(intermed, destino, correct_passos)
            pontos_c = _aplicar_ease(pontos_c, _ease_in_out)
            await _percorrer_pontos(page, state, pontos_c, tremor_intensidade=0.6)

        else:
            # MOVIMENTO CURTO/MEDIO — curva unica sorteada
            r = random.random()
            if r < 0.40:
                pontos = _gerar_pontos_cubica(origem, destino, num_passos)
            elif r < 0.70:
                pontos = _gerar_pontos_quadratica(origem, destino, num_passos)
            else:
                pontos = _gerar_pontos_multi(origem, destino, num_passos)
            ease_fn = _escolher_ease()
            pontos = _aplicar_ease(pontos, ease_fn)

            # pausa mid-flight ocasional (~5%)
            if len(pontos) > 10 and random.random() < 0.05:
                meio = len(pontos) // 2 + random.randint(-2, 2)
                meio = max(1, min(len(pontos) - 1, meio))
                await _percorrer_pontos(page, state, pontos[:meio])
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await _percorrer_pontos(page, state, pontos[meio:])
            else:
                await _percorrer_pontos(page, state, pontos)

        # posicao final exata
        await _dispatch_mouse(page, state, "mouseMoved", destino_x, destino_y)
        state.x = destino_x
        state.y = destino_y
        state.last_move_end_ts = time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  API PUBLICA — mover_para_elemento()
# ══════════════════════════════════════════════════════════════════════════════

async def mover_para_elemento(page, elemento, clicar=True):
    """
    Move ate um elemento e (opcional) clica.
    Mira com sigma sub-linear (Fitts) — botoes pequenos tem mira mais precisa,
    cards grandes tem mira mais espalhada, mas nenhum extremo absurdo.
    """
    box = await elemento.bounding_box()
    if not box:
        return False

    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    # sigma sub-linear (Fitts): sqrt(w)*1.2 + 2, clampado por 15% da dimensao
    sigma_x = min(box["width"] * 0.15, math.sqrt(max(box["width"], 0)) * 1.2 + 2)
    sigma_y = min(box["height"] * 0.15, math.sqrt(max(box["height"], 0)) * 1.2 + 2)
    destino_x = cx + random.gauss(0, sigma_x)
    destino_y = cy + random.gauss(0, sigma_y)

    # clampar dentro do elemento com margem de 10%
    margem_x = box["width"] * 0.1
    margem_y = box["height"] * 0.1
    destino_x = max(box["x"] + margem_x,
                    min(box["x"] + box["width"] - margem_x, destino_x))
    destino_y = max(box["y"] + margem_y,
                    min(box["y"] + box["height"] - margem_y, destino_y))

    # target_width = menor dimensao (conservador — caso mais dificil de Fitts)
    target_w = min(box["width"], box["height"])

    await mover(page, destino_x, destino_y, target_width=target_w)

    if clicar:
        await clicar_humano(page)

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  API PUBLICA — clicar_humano()
# ══════════════════════════════════════════════════════════════════════════════

async def clicar_humano(page):
    """
    Clique humanizado.
      - Delay pre-clique (reacao cognitiva)
      - Drift decidido ANTES do mousedown (ponto de partida correto)
      - mousedown via CDP com force ~0.5 (default real do Chrome)
      - Duracao lognormal ~70ms
      - Drift aplicado mid-press quando escolhido (dedo escorrega)
      - mouseup via CDP com force=0
      - Delay pos-clique (feedback visual)
    """
    state = _get_state(page)

    # delay pre-clique
    await asyncio.sleep(random.uniform(0.06, 0.22))

    # decisao de drift ANTES do mousedown (pra aplicar do ponto correto)
    drift_x = random.gauss(0, 0.6)
    drift_y = random.gauss(0, 0.6)
    apply_drift = abs(drift_x) > 0.3 or abs(drift_y) > 0.3

    # duracao: lognormal, mediana ~70ms, range 35-220ms
    duracao = random.lognormvariate(math.log(0.07), 0.35)
    duracao = max(0.035, min(0.22, duracao))

    # pressure realista durante mousedown (~0.5 — default Chrome)
    pressure = random.uniform(0.45, 0.55)

    await _dispatch_mouse(
        page, state, "mousePressed",
        state.x, state.y,
        button="left", buttons=1,
        force=pressure, click_count=1,
    )

    if apply_drift:
        await asyncio.sleep(duracao * 0.4)
        state.x += drift_x
        state.y += drift_y
        tx, ty = state.tick_tremor(intensidade=0.5)
        await _dispatch_mouse(
            page, state, "mouseMoved",
            state.x + tx, state.y + ty,
            buttons=1, force=pressure,
        )
        await asyncio.sleep(duracao * 0.6)
    else:
        await asyncio.sleep(duracao)

    await _dispatch_mouse(
        page, state, "mouseReleased",
        state.x, state.y,
        button="left", buttons=0,
        force=0.0, click_count=1,
    )

    # delay pos-clique
    await asyncio.sleep(random.uniform(0.08, 0.30))
    state.last_move_end_ts = time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  API PUBLICA — hover_elemento()
# ══════════════════════════════════════════════════════════════════════════════

async def hover_elemento(page, elemento, tempo_hover=None):
    """
    Hover humanizado sobre um elemento — move, pausa (simulando leitura)
    e opcionalmente emite micro-movimentos enquanto espera (mao nao fica parada).
    """
    box = await elemento.bounding_box()
    if not box:
        return False

    state = _get_state(page)

    destino_x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
    destino_y = box["y"] + box["height"] * random.uniform(0.2, 0.8)

    target_w = min(box["width"], box["height"])
    await mover(page, destino_x, destino_y, target_width=target_w)

    if tempo_hover is None:
        tempo_hover = random.uniform(0.5, 1.8)
    await asyncio.sleep(tempo_hover)

    # micro-movimentos durante hover longo
    if tempo_hover > 0.8 and random.random() < 0.5:
        for _ in range(random.randint(1, 3)):
            mx = destino_x + random.gauss(0, 2)
            my = destino_y + random.gauss(0, 2)
            tx, ty = state.tick_tremor(intensidade=0.6)
            await _dispatch_mouse(page, state, "mouseMoved", mx + tx, my + ty)
            state.x = mx
            state.y = my
            await asyncio.sleep(random.uniform(0.2, 0.6))

    state.last_move_end_ts = time.time()
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  API PUBLICA — pressionar_tecla()
# ══════════════════════════════════════════════════════════════════════════════

async def pressionar_tecla(page, tecla: str, delay_pre=None, delay_pos=None):
    """
    Keypress humanizado (keydown→keyup com hold lognormal).
    Nao toca no estado de mouse (teclado tem pipeline proprio).
    """
    if delay_pre is None:
        delay_pre = random.uniform(0.1, 0.4)
    await asyncio.sleep(delay_pre)

    hold_time = random.lognormvariate(math.log(0.075), 0.3)
    hold_time = max(0.03, min(0.18, hold_time))

    await page.keyboard.down(tecla)
    await asyncio.sleep(hold_time)
    await page.keyboard.up(tecla)

    if delay_pos is None:
        delay_pos = random.uniform(0.05, 0.2)
    await asyncio.sleep(delay_pos)


# ══════════════════════════════════════════════════════════════════════════════
#  API PUBLICA — scroll_suave()
# ══════════════════════════════════════════════════════════════════════════════

async def scroll_suave(page, direcao="down", distancia=None):
    """
    Scroll suave via CDP mouseWheel, ancorado na posicao atual do cursor.
    Desaceleracao progressiva + diff compensatorio clampado em ±30%.
    """
    state = _get_state(page)

    if distancia is None:
        distancia = random.randint(80, 250)
    if direcao == "up":
        distancia = -distancia

    parcelas = random.randint(3, 7)
    total = 0.0

    for i in range(parcelas):
        fator = 1.0 - (i / parcelas) * 0.6
        parcela = (distancia / parcelas) * fator + random.uniform(-3, 3)
        total += parcela
        await _dispatch_wheel(page, state, state.x, state.y, delta_y=parcela)
        delay = random.uniform(0.03, 0.06) + (i / parcelas) * 0.04
        await asyncio.sleep(delay)

    # diff compensatorio — CLAMPADO em ±30% da distancia total
    diff = distancia - total
    max_comp = abs(distancia) * 0.3
    diff = max(-max_comp, min(max_comp, diff))
    if abs(diff) > 5:
        await asyncio.sleep(random.uniform(0.05, 0.1))
        await _dispatch_wheel(page, state, state.x, state.y, delta_y=diff)

    await asyncio.sleep(random.uniform(0.15, 0.45))


# ══════════════════════════════════════════════════════════════════════════════
#  CURSOR VISUAL — Shadow DOM closed via Isolated World
# ══════════════════════════════════════════════════════════════════════════════

_CURSOR_JS = """(() => {
    if (document.querySelector('.__tw')) return;
    const host = document.createElement('div');
    host.className = '__tw';
    host.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;' +
        'z-index:2147483647;pointer-events:none;overflow:visible;';
    const shadow = host.attachShadow({ mode: 'closed' });
    const dot = document.createElement('div');
    dot.style.cssText = 'position:fixed;width:20px;height:20px;border-radius:50%;' +
        'background:rgba(255,50,50,0.7);border:2px solid rgba(255,255,255,0.9);' +
        'box-shadow:0 0 6px rgba(0,0,0,0.4);transform:translate(-50%,-50%);' +
        'pointer-events:none;left:-50px;top:-50px;';
    shadow.appendChild(dot);
    document.documentElement.appendChild(host);
    document.addEventListener('mousemove', e => {
        dot.style.left = e.clientX + 'px';
        dot.style.top = e.clientY + 'px';
    }, true);
})()"""


async def instalar_cursor_visivel(page):
    """Injeta bolinha vermelha no Shadow DOM — segue automaticamente mousemove."""
    try:
        from browser_engine import cdp_eval
        await cdp_eval(page, _CURSOR_JS)
    except Exception:
        pass
