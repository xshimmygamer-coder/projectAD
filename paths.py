"""
Resolucao de caminhos — funciona tanto rodando do codigo quanto empacotado (.exe).
Todos os arquivos de runtime (settings.json, tokens.txt, proxies_pool.txt, logs, assets)
ficam AO LADO do executavel/projeto.
"""
import os
import sys


def base_dir():
    """Pasta base: do executavel se empacotado (PyInstaller/flet pack), senao do script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def arquivo(nome):
    """Caminho absoluto de um arquivo de runtime na base."""
    return os.path.join(base_dir(), nome)


def asset(nome):
    """Caminho de um asset embarcado (pasta assets/)."""
    return os.path.join(base_dir(), "assets", nome)
