# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec do twitch_swap (GUI Flet). Build:  pyinstaller --noconfirm twitch_swap.spec
# Gera dist\twitch_swap\twitch_swap.exe (onedir — mais robusto p/ Flet + Playwright).
from PyInstaller.utils.hooks import collect_all

datas = [("assets/banner.png", "assets"), ("assets/icone.ico", "assets")]
binaries = []
hiddenimports = [
    # win32 (preview/janelas) + Pillow (downscale do preview)
    "win32gui", "win32con", "win32process", "win32api", "pywintypes",
    # modulos do projeto
    "orquestrador", "navegacao", "ad_detector", "swap", "preview",
    "mouse_humano", "config_store", "eventos", "paths",
]

# Empacota Flet (cliente desktop) + Playwright (driver) + Pillow + Patchright se houver.
for pkg in ("flet", "flet_desktop", "playwright", "PIL", "patchright"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MURIADS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # GUI (sem janela de console)
    disable_windowed_traceback=False,
    icon="assets/icone.ico",   # icone do .exe (gerado da assets/logo.png)
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MURIADS",
)
