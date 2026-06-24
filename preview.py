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


async def capturador(intervalo=2.0, largura=320, quality=35):
    """Loop: screenshot (jpeg) de cada perfil ativo, downscale e guarda em base64."""
    try:
        from PIL import Image
    except Exception:
        Image = None

    async def cap(n, page, canal):
        try:
            raw = await page.screenshot(type="jpeg", quality=quality)
        except Exception:
            return
        if Image is not None:
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                w, h = im.size
                if w > largura:
                    im = im.resize((largura, max(1, int(h * largura / w))))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality)
                raw = buf.getvalue()
            except Exception:
                pass
        b64 = base64.b64encode(raw).decode()
        with _lock:
            _shots[n] = (b64, canal)

    try:
        while True:
            with _lock:
                itens = [(n, p, c) for n, (p, c) in _pages.items()]
            if itens:
                await asyncio.gather(*[cap(n, p, c) for n, p, c in itens])
            await asyncio.sleep(intervalo)
    except asyncio.CancelledError:
        pass
