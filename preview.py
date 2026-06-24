"""
Preview dos perfis via CDP (screenshots) — exibido DENTRO da GUI (aba Preview).
Substitui o TaskView DWM. Captura page.screenshot (jpeg) de cada perfil ativo a cada
INTERVALO, faz downscale (Pillow) e guarda em base64 num store thread-safe. Funciona
mesmo com a janela minimizada/coberta (CDP renderiza a pagina).

Orquestrador: registrar(n, page, canal) ao navegar / desregistrar(n) ao fechar; roda
capturador() como task. A GUI le get_shots() e desenha a grade.
"""
import asyncio
import base64
import io
import threading

_lock = threading.Lock()
_pages = {}   # n -> (page, canal)
_shots = {}   # n -> (base64_jpeg, canal)
_ativo = True   # GUI desliga quando NAO esta na aba Preview -> nao captura (alivia CDP/UI)


def set_ativo(v):
    """Liga/desliga a captura (a GUI chama conforme a aba Preview estar visivel)."""
    global _ativo
    _ativo = bool(v)


def registrar(n, page, canal=""):
    with _lock:
        _pages[n] = (page, canal)


def desregistrar(n):
    with _lock:
        _pages.pop(n, None)
        _shots.pop(n, None)


def limpar():
    with _lock:
        _pages.clear()
        _shots.clear()


def get_shots():
    """{n: (base64, canal)} — copia p/ a GUI desenhar."""
    with _lock:
        return dict(_shots)


def _downscale(raw, largura, quality):
    """Reduz o jpeg (CPU) — roda em thread separada p/ nao travar o event loop."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    if w > largura:
        im = im.resize((largura, max(1, int(h * largura / w))))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


async def capturador(intervalo=2.0, largura=320, quality=35, max_simult=6, shot_timeout=4.0):
    """Loop: screenshot (jpeg) de cada perfil ativo -> downscale -> base64. Resiliente:
      - timeout por screenshot (pagina travada/erro NAO congela o resto);
      - no maximo `max_simult` capturas simultaneas (nao satura o CDP);
      - downscale (PIL) em thread (nao bloqueia o event loop)."""
    try:
        import PIL  # noqa: F401
        tem_pil = True
    except Exception:
        tem_pil = False
    sem = asyncio.Semaphore(max_simult)

    async def cap(n, page, canal):
        async with sem:
            try:
                raw = await page.screenshot(type="jpeg", quality=quality,
                                            timeout=int(shot_timeout * 1000))
            except Exception:
                return   # travada/fechando/erro -> pula este frame, nao trava os outros
        if tem_pil:
            try:
                raw = await asyncio.to_thread(_downscale, raw, largura, quality)
            except Exception:
                pass
        with _lock:
            _shots[n] = (base64.b64encode(raw).decode(), canal)

    try:
        while True:
            if not _ativo:                       # aba Preview fechada -> nao captura nada
                await asyncio.sleep(intervalo)
                continue
            with _lock:
                itens = [(n, p, c) for n, (p, c) in _pages.items()]
            if itens:
                await asyncio.gather(*[cap(n, p, c) for n, p, c in itens],
                                     return_exceptions=True)
            await asyncio.sleep(intervalo)
    except asyncio.CancelledError:
        pass
