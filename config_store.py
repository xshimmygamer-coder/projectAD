"""
Armazenamento de configuracao da GUI em settings.json (na base do projeto/exe).
Um unico dict com duas secoes:
  - "adspower": {api_key, base, group_id, filtro_nome}
  - "run":      {canais, n_perfis, sessao_min_s, sessao_max_s, grace_min_s, grace_max_s,
                 ad_margem_s, api_intervalo_s, stagger_s, bau, bau_check_s,
                 taskview, taskview_proc, taskview_intervalo, timeout_nav_ms, timeout_rede_ms}
settings.json e SEGREDO (api_key) -> gitignored.
"""
import json
import threading

import paths

ARQ = "settings.json"
_lock = threading.Lock()
_cache = None


def carregar():
    """Le settings.json (cacheia). Retorna {} se nao existir/invalido."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            with open(paths.arquivo(ARQ), encoding="utf-8") as f:
                _cache = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            _cache = {}
        return _cache


def recarregar():
    """Forca releitura do disco (apos a GUI salvar)."""
    global _cache
    with _lock:
        _cache = None
    return carregar()


def get(secao, chave, default=None):
    d = carregar().get(secao, {}) or {}
    val = d.get(chave, default)
    return default if val is None else val


def salvar_secao(secao, dados):
    """Mescla/atualiza uma secao e grava no disco."""
    with _lock:
        atual = {}
        try:
            with open(paths.arquivo(ARQ), encoding="utf-8") as f:
                atual = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            atual = {}
        atual[secao] = dados
        with open(paths.arquivo(ARQ), "w", encoding="utf-8") as f:
            json.dump(atual, f, ensure_ascii=False, indent=2)
        global _cache
        _cache = atual
    return True
