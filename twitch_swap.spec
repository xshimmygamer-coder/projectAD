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

# Empacota Flet (core + cliente WEB + desktop) + Playwright (driver) + Pillow + Patchright.
# flet_web traz o app web (web/index.html, main.dart.js...) — sem ele o modo web da 500.
# flet_desktop traz o cliente Flutter (a JANELA nativa) — SEM ele o .exe sobe mas a janela
# nunca aparece. Por isso flet/flet_web/flet_desktop sao ESSENCIAIS: se faltarem, o build
# DEVE falhar alto (instale com `pip install "flet[all]==0.28.3"`), nao sair quebrado em silencio.
for pkg in ("flet", "flet_web", "flet_desktop"):
    d, b, h = collect_all(pkg)   # ModuleNotFoundError aqui = pare e instale o extra que falta
    datas += d
    binaries += b
    hiddenimports += h
# Opcionais: ausencia nao quebra o app (Patchright cai p/ playwright; sem Pillow o preview degrada).
for pkg in ("playwright", "PIL", "patchright"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# msvcp140.dll (runtime C++ do MSVC): o _greenlet.pyd do Playwright DEPENDE dele. O Python
# so traz vcruntime140*, NAO o msvcp140 -> sem ele a RUN falha com "DLL load failed while
# importing _greenlet". O PyInstaller so o bundla se achar no sistema; entao copiamos
# explicitamente do System32 (requer o VC++ Redistributable x64 instalado na maquina de
# build) p/ a RAIZ do bundle, onde o greenlet procura. Assim o .exe roda em PCs SEM o redist.
import os as _os
_msvcp = _os.path.join(_os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll")
if _os.path.exists(_msvcp):
    binaries += [(_msvcp, ".")]
else:
    raise SystemExit("msvcp140.dll nao encontrado no System32 — instale o VC++ "
                     "Redistributable x64 (https://aka.ms/vs/17/release/vc_redist.x64.exe) "
                     "antes do build.")

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
