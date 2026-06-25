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
import os
import threading

import paths

_lock = threading.Lock()
_pages = {}   # n -> (page, canal)
_shots = {}   # n -> (base64_jpeg, canal)
_ativo = True   # GUI desliga quando NAO esta na aba Preview -> nao captura (alivia CDP/UI)
PREVIEW_DIR = paths.arquivo("preview")   # engine grava slot_N.jpg aqui; a GUI (outro proc) le


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

    try:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
    except OSError:
        pass

    def _gravar(n, raw):
        # grava atomico: .tmp -> replace (a GUI, outro processo, nunca le pela metade)
        dst = os.path.join(PREVIEW_DIR, f"slot_{n}.jpg")
        try:
            with open(dst + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(dst + ".tmp", dst)
        except OSError:
            pass

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
        _gravar(n, raw)                          # disponibiliza p/ a GUI (processo separado)

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
            # limpa JPEGs de slots que sairam (perfil fechado)
            try:
                ativos = {f"slot_{n}.jpg" for n, _, _ in itens}
                for fn in os.listdir(PREVIEW_DIR):
                    if fn.startswith("slot_") and fn.endswith(".jpg") and fn not in ativos:
                        try:
                            os.remove(os.path.join(PREVIEW_DIR, fn))
                        except OSError:
                            pass
            except OSError:
                pass
            await asyncio.sleep(intervalo)
    except asyncio.CancelledError:
        pass
