"""
Resolucao de caminhos — funciona tanto rodando do codigo quanto empacotado (.exe).
Todos os arquivos de runtime (settings.json, tokens.txt, proxies_pool.txt, logs, assets)
ficam AO LADO do executavel/projeto.
"""
import os
import sys


def base_dir():
    """Pasta base p/ arquivos de RUNTIME (settings/tokens/proxies/logs): ao lado do
    executavel se empacotado (PyInstaller/flet pack), senao do script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir():
    """Pasta dos assets EMBARCADOS. No .exe (PyInstaller) eles vao p/ sys._MEIPASS
    (a pasta _internal); rodando do codigo, e a propria base."""
    return getattr(sys, "_MEIPASS", base_dir())


def arquivo(nome):
    """Caminho absoluto de um arquivo de RUNTIME na base (gravavel, ao lado do exe)."""
    return os.path.join(base_dir(), nome)


def asset(nome):
    """Caminho de um asset EMBARCADO (assets/<nome>) — bundle-aware."""
    return os.path.join(_bundle_dir(), "assets", nome)


def assets_dir():
    """Pasta de assets embarcados (p/ o assets_dir do Flet) — bundle-aware."""
    return os.path.join(_bundle_dir(), "assets")
