"""
Modulo de NAVEGACAO ao canal alvo (Twitch) via CDP. Replica o fluxo do MURI_PRO
(ir_para_canal_alvo -> migrar_pela_sidebar -> _navegar_por_busca -> navegar):

  TIER 1  HOMEPAGE/SIDEBAR : procura o canal por TODA a home + sidebar esquerda
          (cards da home, "Followed", "Recommended"); expande sidebar colapsada,
          clica "Show More", rola, e CLICA no link do canal (navegacao organica).
  TIER 2  BUSCA            : abre a busca da Twitch, digita o canal (humanizado),
          clica no resultado.
  TIER 3  URL DIRETO       : page.goto / CDP Page.navigate (fallback).

Seletores iguais aos do MURI_PRO. Cliques/hover/scroll/digitacao usam mouse_humano
(movimento de mouse REAL via CDP: Fitts/Bezier/tremor) — portado do MURI_PRO. Inclui
DESMUTAR (clique real no botao de mute) e RESGATAR BAU (community points). Patchright
preferido no import (anti-deteccao), cai pra playwright puro.

Teste isolado (perfil JA aberto, com a porta CDP do AdsPower):
  python navegacao.py <debug_port> <canal> [rotulo]
  ex.: python navegacao.py 20323 vitinho "k1bdjmcm"

Requer:  pip install playwright   (ou patchright, preferido p/ anti-deteccao)
"""
import asyncio
import json
import random
import sys

import mouse_humano

HOME = "https://www.twitch.tv/"

# ── Seletores (iguais ao MURI_PRO) ──────────────────────────────────────────
SEL_SIDEBAR_COLLAPSED = ".side-nav--collapsed"
SEL_ARROW = 'button[data-a-target="side-nav-arrow"]'
SEL_SHOW_MORE = ('button[data-a-target="side-nav-show-more-button"], '
                 '.side-nav button:has-text("Show More"), '
                 '.side-nav button:has-text("Mostrar mais")')
SEL_SIDEBAR = '.side-nav-section, .side-bar-contents, [data-a-target="side-nav-bar"]'
SEL_SEARCH_LINK = ('a[data-a-target="search-link"], a[href="/search"], '
                   '[data-a-target="nav-search-link"]')
SEL_SEARCH_INPUT = ('input[data-a-target="tw-input"], input[type="search"], '
                    'input[aria-label*="Search"], input[aria-label*="Pesquisar"]')
SEL_MUTE = ('button[data-a-target="player-mute-unmute-button"], '
            'button[data-a-target="player-volume-unmute-button"]')
SEL_OVERLAY = ('button[data-a-target="content-classification-gate-overlay-start-watching-button"], '
               'button[data-a-target="player-overlay-mature-accept"], '
               'button:has-text("Start Watching"), '
               'button:has-text("Começar a assistir"), '
               'button:has-text("Continuar assistindo"), '
               'button:has-text("Continue Watching")')
SEL_BAU = ('button[aria-label*="laim" i], '       # Claim / reivindicar
           'button[aria-label*="esgatar" i], '    # Resgatar
           'button[aria-label*="onus" i]')        # Bonus
# Overlay de anuncio do player (reflete o que o VIEWER ve — mais preciso p/ o FIM do ad)
SEL_AD_OVERLAY = ('[data-a-target="video-ad-label"], '
                  '[data-a-target="video-ad-countdown"], '
                  '[data-a-target="player-ad-notice"]')

# digitacao humana (typos realistas) — portado do chat_writer.py do MURI_PRO
TECLAS_ADJACENTES = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr",
    "f": "dg", "g": "fh", "h": "gj", "i": "uo", "j": "hk",
    "k": "jl", "l": "kç", "m": "n", "n": "bm", "o": "ip",
    "p": "o", "q": "wa", "r": "et", "s": "ad", "t": "ry",
    "u": "yi", "v": "cb", "w": "qe", "x": "zc", "y": "tu", "z": "x",
}
CHANCE_TYPO = 0.03

def _sel_link(canal):
    # casa o link do canal em QUALQUER lugar (cards da home + sidebar). 'i' = case-insensitive.
    return f'a[href="/{canal}" i]'

# ── Helpers ─────────────────────────────────────────────────────────────────
def _no_canal(url, canal):
    u = (url or "").lower()
    return f"/{canal}" in u and "twitch.tv" in u

async def _clicar(page, loc):
    """Clique com MOUSE REAL (mouse_humano: Fitts/Bezier/tremor via CDP).
    scroll-into-view antes; fallback pro click do Playwright se falhar."""
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        if await mouse_humano.mover_para_elemento(page, loc, clicar=True):
            return True
    except Exception:
        pass
    try:
        await loc.click(timeout=5000)
        return True
    except Exception:
        return False

async def digitar_humano(page, texto):
    """Digita char-a-char com velocidade humana + typos realistas (portado do MURI_PRO)."""
    for ch in texto:
        is_ascii = ord(ch) < 128
        if is_ascii and random.random() < CHANCE_TYPO and ch.lower() in TECLAS_ADJACENTES:
            errada = random.choice(TECLAS_ADJACENTES[ch.lower()])
            await page.keyboard.press(errada)
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await asyncio.sleep(random.uniform(0.2, 0.5))   # "percebi o erro"
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.1))
        if is_ascii:
            await page.keyboard.press(ch)
        else:
            await page.keyboard.type(ch)
        base = random.uniform(0.04, 0.12)
        if ch in " .,!?":
            base += random.uniform(0.05, 0.2)
        if random.random() < 0.15:
            base = random.uniform(0.02, 0.04)
        await asyncio.sleep(base)

class ProxySemRede(Exception):
    """O proxy do perfil esta sem conectividade (a Twitch nao carrega)."""

async def tem_rede(page, timeout=20000, tentativas=2):
    """True se o proxy tem rede: tenta carregar a home da Twitch e confere se
    realmente chegou (url = twitch.tv; net-error cai em chrome-error -> nao casa).
    Faz ate `tentativas` (proxies residenciais oscilam). False = sem rede."""
    for _ in range(tentativas):
        try:
            await page.goto(HOME, wait_until="domcontentloaded", timeout=timeout)
            if "twitch.tv" in (page.url or "").lower():
                return True
        except Exception:
            pass
        await asyncio.sleep(1.5)
    return False

async def aplicar_preferencias(page, qualidade="160p30", dark=True, rotulo=""):
    """Pre-seta tema + qualidade no localStorage da Twitch via INIT SCRIPT (roda ANTES
    do app da Twitch ler) -> a pagina ja carrega dark + na qualidade alvo, desde o
    primeiro frame. Aplica em TODA navegacao e sobrevive ao delcache (que limpa o
    localStorage a cada ciclo). Chamar logo apos conectar, ANTES de navegar."""
    sets = []
    if dark:
        sets.append(("twilight.theme", "1"))
    if qualidade:
        sets.append(("video-quality", json.dumps({"default": qualidade})))
    if not sets:
        return
    linhas = "".join(f"localStorage.setItem({json.dumps(k)},{json.dumps(v)});" for k, v in sets)
    script = "try{" + linhas + "}catch(e){}"
    try:
        await page.context.add_init_script(script)
    except Exception as e:
        print(f"  [{rotulo}] prefs (tema/160p) falhou: {str(e)[:60]}", flush=True)

async def navegar_url(page, url, timeout=30000):
    """TIER 3: page.goto e, se falhar, CDP Page.navigate direto (sem Runtime)."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return
    except Exception:
        pass
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.navigate", {"url": url})
        await asyncio.sleep(1)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception as e:
        print(f"  [navegar] {url[:40]} falhou: {str(e)[:40]}", flush=True)
        await asyncio.sleep(3)

# ── TIER 1: homepage / sidebar (ENXUTO) ─────────────────────────────────────
# Conta nova quase nunca SEGUE o canal (logo nao aparece na sidebar). Entao aqui so
# fazemos UMA checagem rapida: se o link do canal ja estiver visivel na home/sidebar
# (ex.: canal grande em destaque, ou conta que segue), clica; senao vai DIRETO pra
# busca — sem o ritual lento de expandir/show-more/rolar (que prendia ~10s na home).
async def _tier_sidebar(page, canal, log):
    log(f"procurando /{canal} na home/sidebar (rapido)...")
    try:
        await page.wait_for_selector(SEL_SIDEBAR + ", " + SEL_SEARCH_LINK, timeout=8000)
    except Exception:
        pass
    try:
        link = page.locator(_sel_link(canal)).first
        try:
            await link.wait_for(state="visible", timeout=2000)   # curto: aparece ou nao
        except Exception:
            pass
        if await link.count() > 0:
            await asyncio.sleep(random.uniform(0.4, 1.0))
            if await _clicar(page, link):
                await asyncio.sleep(random.uniform(3, 5))
                if _no_canal(page.url, canal):
                    log(f"NO CANAL /{canal} (via home/sidebar)")
                    return True
    except Exception:
        pass
    log("nao esta na home/sidebar -> indo pra busca")
    return False

# ── TIER 2: busca ───────────────────────────────────────────────────────────
async def _tier_busca(page, canal, log):
    log(f"tentando pela busca da Twitch...")
    try:
        btn = page.locator(SEL_SEARCH_LINK).first
        if await btn.count() > 0:
            await _clicar(page, btn)
            await asyncio.sleep(random.uniform(1, 2))

        inp = page.locator(SEL_SEARCH_INPUT).first
        if await inp.count() == 0:
            return False
        await _clicar(page, inp)
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await digitar_humano(page, canal)
        await asyncio.sleep(random.uniform(1.2, 2.2))

        # clica no resultado do canal; se nao houver, tenta Enter e reprocura
        for usar_enter in (False, True):
            if usar_enter:
                await page.keyboard.press("Enter")
                await asyncio.sleep(random.uniform(2, 3))
            res = page.locator(_sel_link(canal)).first
            try:
                await res.wait_for(state="visible", timeout=4000)
            except Exception:
                pass
            if await res.count() > 0:
                await _clicar(page, res)
                await asyncio.sleep(random.uniform(3, 5))
                if _no_canal(page.url, canal):
                    log(f"NO CANAL /{canal} (via busca{'/enter' if usar_enter else ''})")
                    return True
    except Exception as e:
        log(f"busca falhou: {str(e)[:60]}")
    return False

# ── Player: fechar overlay + DESMUTAR ───────────────────────────────────────
async def _fechar_overlay(page):
    """Fecha gates de conteudo (mature / 'Start Watching') que travam o player."""
    try:
        btn = page.locator(SEL_OVERLAY).first
        if await btn.count() > 0 and await btn.is_visible():
            await _clicar(page, btn)
            await asyncio.sleep(random.uniform(1.5, 3))
            return True
    except Exception:
        pass
    return False

# Banners de consentimento em varios idiomas (EN/PT/ES). has-text = substring
# case-insensitive. Cobre cookies ("Proceed"/"Prosseguir"/"Continuar"/"Proceder")
# e Termos/"Heads Up" ("Accept"/"Aceitar"/"Aceptar"/"Concordar"/"De acuerdo").
SEL_BANNERS = (
    'button[data-a-target="consent-banner-accept"], '
    'button:has-text("Proceed"), '
    'button:has-text("Prosseguir"), '
    'button:has-text("Continuar"), '
    'button:has-text("Proceder"), '
    'button:has-text("Accept"), '
    'button:has-text("Aceitar"), '
    'button:has-text("Aceptar"), '
    'button:has-text("Concordar"), '
    'button:has-text("De acuerdo"), '
    'button:has-text("Aceito")'
)

async def fechar_banners(page, rotulo=""):
    """Fecha banners de consentimento (cookies 'Proceed' / ToS 'Accept') que sujam a
    tela. Clica com mouse real apenas se estiverem visiveis. Best-effort, nao quebra."""
    try:
        loc = page.locator(SEL_BANNERS)
        n = await loc.count()
    except Exception:
        return
    fechou = 0
    for i in range(min(n, 3)):
        try:
            b = loc.nth(i)
            if await b.is_visible():
                if await _clicar(page, b):
                    fechou += 1
                    await asyncio.sleep(random.uniform(0.3, 0.7))
        except Exception:
            pass
    if fechou:
        print(f"  [{rotulo}] {fechou} banner(s) de consentimento fechado(s)", flush=True)

async def desmutar(page, rotulo=""):
    """Desmuta a stream SE estiver mutada (replica o MURI_PRO):
    checa o aria-label do botao de mute; 'unmute'/'ativar som' = mutado -> clica.
    Sem botao visivel, autoplay normalmente vem MUTADO -> best-effort tecla 'm'."""
    def log(m):
        print(f"  [{rotulo}] {m}", flush=True)
    try:
        await _fechar_overlay(page)
        # hover no player (mouse real) revela os controles e o botao de mute
        video = page.locator("video").first
        if await video.count() > 0:
            try:
                await mouse_humano.hover_elemento(page, video,
                                                  tempo_hover=random.uniform(0.6, 1.4))
            except Exception:
                pass

        btn = page.locator(SEL_MUTE).first
        if await btn.count() > 0:
            label = (await btn.get_attribute("aria-label") or "").lower()
            mutado = ("unmute" in label) or ("ativar som" in label)
            if not mutado:
                log("som ja ativo")
                return True
            log("player mutado -> desmutando (mouse real)")
            await _clicar(page, btn)   # mouse_humano move+click no botao de mute
            await asyncio.sleep(random.uniform(0.3, 0.7))
            label2 = (await btn.get_attribute("aria-label") or "").lower()
            if ("unmute" in label2) or ("ativar som" in label2):   # ainda mutado: tecla 'm'
                await mouse_humano.pressionar_tecla(page, "m")
                await asyncio.sleep(0.4)
            return True

        # sem botao: assume mutado (autoplay) -> tecla 'm' (best-effort)
        log("botao de mute ausente — best-effort tecla 'm'")
        await mouse_humano.pressionar_tecla(page, "m")
        await asyncio.sleep(0.4)
        return True
    except Exception as e:
        log(f"unmute falhou: {str(e)[:60]}")
        return False

async def ad_na_tela(page):
    """True se o overlay de anuncio do player esta visivel (label/countdown). Reflete
    o que o VIEWER realmente ve — confirmacao precisa de que o ad AINDA esta rolando."""
    try:
        ov = page.locator(SEL_AD_OVERLAY).first
        return await ov.count() > 0 and await ov.is_visible()
    except Exception:
        return False


# Slate roxo do anuncio (SSAI): o "Commercial break in progress" e FRAME DE VIDEO (pixels com
# o logo da Twitch), NAO texto na DOM -> get_by_text nunca acha. Detecta por COR: durante um
# ad break, o player fica dominado pelo gradiente roxo/azul do slate (medido ~89% nesse caso).
# Independe de idioma. Sinaliza perfil 'ruim' (experiencia degradada) -> abandonar e reciclar.
SLATE_FRAC = 0.72   # fracao minima de pixels azul/roxo no player p/ considerar slate

def _frac_roxo(raw):
    import io, colorsys
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB").resize((48, 32))
    px = list(im.getdata())
    if not px:
        return 0.0
    n = 0
    for r, g, b in px:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if 0.55 <= h <= 0.83 and s >= 0.20 and v >= 0.12:
            n += 1
    return n / len(px)

async def slate_publicidade(page):
    """True se o player esta mostrando o slate roxo do anuncio. Barato primeiro (tem ad na
    tela?), e SO entao tira screenshot do player e mede a fracao de roxo (gate anti-falso-
    positivo: so considera slate se houver ad em andamento + player dominado pelo roxo)."""
    try:
        if not await ad_na_tela(page):           # sem overlay de ad -> nao e slate
            return False
        alvo = None
        for sel in ('[data-a-target="video-player"]', '.video-player', '.persistent-player'):
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0:
                    alvo = loc
                    break
            except Exception:
                pass
        raw = await (alvo.screenshot(type="jpeg", timeout=5000) if alvo is not None
                     else page.screenshot(type="jpeg", timeout=5000))
        return (await asyncio.to_thread(_frac_roxo, raw)) >= SLATE_FRAC
    except Exception:
        return False

async def resgatar_bau(page, rotulo=""):
    """Coleta o bau de pontos (community points) se estiver disponivel — MOUSE REAL.
    Seletores + fluxo do MURI_PRO (_coletar_bau_rapido)."""
    try:
        bau = page.locator(SEL_BAU).first
        if await bau.count() == 0:
            return False
        try:
            if not await bau.is_visible():
                return False
        except Exception:
            pass
        if await _clicar(page, bau):
            print(f"  [{rotulo}] bau coletado", flush=True)
            return True
    except Exception:
        pass
    return False

# ── Orquestrador da navegacao ───────────────────────────────────────────────
async def ir_para_canal(page, canal, rotulo="", timeout=30000, desmutar_apos=True,
                        comecar_da_home=True):
    """3 camadas: homepage/sidebar -> busca -> URL direto. Ao chegar, DESMUTA a
    stream (se desmutar_apos). Retorna True se chegou no canal.

    comecar_da_home=True: SEMPRE volta pra home antes de procurar — mesmo que o
    Chromium tenha restaurado a aba do canal (session-restore). Sem isso, o perfil
    abriria ja no canal e o fluxo organico (sidebar->busca) seria pulado."""
    canal = canal.strip().lstrip("/").lower()
    def log(m):
        print(f"  [{rotulo}] {m}", flush=True)

    # SEMPRE parte da home: garante o fluxo organico (sidebar/homepage -> busca -> URL),
    # ignorando qualquer aba de canal restaurada pela sessao anterior.
    if comecar_da_home or "twitch.tv" not in (page.url or "").lower():
        log("indo pra home da Twitch...")
        await navegar_url(page, HOME, timeout)
        await asyncio.sleep(random.uniform(2, 4))

    chegou = False
    if await _tier_sidebar(page, canal, log):
        chegou = True
    elif await _tier_busca(page, canal, log):
        chegou = True
    else:
        log(f"fallback: URL direto /{canal}")
        await navegar_url(page, HOME + canal, timeout)
        await asyncio.sleep(random.uniform(2, 4))
        chegou = _no_canal(page.url, canal)
        log(f"NO CANAL /{canal} (via URL direto)" if chegou else f"FALHOU chegar em /{canal}")

    if chegou:
        await fechar_banners(page, rotulo)            # tira os banners de consentimento
        if desmutar_apos:
            await asyncio.sleep(random.uniform(1.5, 3))   # deixa o player carregar
            await desmutar(page, rotulo)
    return chegou

# ── CDP / conexao (standalone) ──────────────────────────────────────────────
def _get_async_playwright():
    """Patchright preferido (anti-deteccao); cai pra playwright puro."""
    try:
        from patchright.async_api import async_playwright
        return async_playwright, "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        return async_playwright, "playwright"

async def _pegar_page(browser):
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    for pg in ctx.pages:
        if "twitch.tv" in (pg.url or "").lower():
            return pg
    if ctx.pages:
        return ctx.pages[0]
    return await ctx.new_page()

async def conectar_e_navegar(cdp_endpoint, canal, rotulo=""):
    async_playwright, engine = _get_async_playwright()
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        print(f"[{rotulo or canal}] conectado via CDP ({engine}) em {cdp_endpoint}", flush=True)
        page = await _pegar_page(browser)
        ok = await ir_para_canal(page, canal, rotulo or canal)
        print(f"[{rotulo or canal}] navegacao: {'OK' if ok else 'FALHOU'}", flush=True)
        return ok

def _cdp_url(arg):
    if arg.startswith("http"):
        return arg
    if ":" in arg:
        return f"http://{arg}"
    return f"http://127.0.0.1:{arg}"

def main():
    if len(sys.argv) < 3:
        print("uso: python navegacao.py <debug_port|host:port|http-url> <canal> [rotulo]")
        sys.exit(2)
    endpoint = _cdp_url(sys.argv[1])
    canal = sys.argv[2]
    rotulo = sys.argv[3] if len(sys.argv) > 3 else canal
    asyncio.run(conectar_e_navegar(endpoint, canal, rotulo))

if __name__ == "__main__":
    main()
