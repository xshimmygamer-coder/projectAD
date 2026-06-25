"""
Etapa 1 — Rotacao de contas/proxies/fingerprint em perfis AdsPower (CDP-free).
Cada perfil (slot) roda em loop: pega conta+proxy livres, gera fingerprint,
limpa cache, seta cookie+proxy+fp, abre na home da Twitch, espera a sessao,
fecha, devolve conta+proxy pra pool, repete.

Uso:  python swap.py
Pare com Ctrl+C (fecha todos os perfis no fim).
"""
import json
import queue
import random
import re
import sys
import threading
import time

import requests

import config_store
import paths

# --------------------------- CONFIG ---------------------------
# AdsPower vem do settings.json (escrito pela GUI), com fallback p/ os defaults.
BASE        = config_store.get("adspower", "base", "http://local.adspower.net:50325")
API_KEY     = config_store.get("adspower", "api_key", "COLOQUE_SUA_API_KEY_AQUI")
PERFIS_GROUP_ID    = config_store.get("adspower", "group_id", "")
PERFIS_FILTRO_NOME = config_store.get("adspower", "filtro_nome", "")
HOME        = "https://www.twitch.tv/"
SESSAO_MIN_S = 600          # (usado só pelo swap.py standalone; o orquestrador tem o seu)
SESSAO_MAX_S = 900
# Limpeza ANTES de setar novo proxy+cookie. _FULL tenta zerar TUDO; se o AdsPower rejeitar
# algum tipo, o delcache cai pro _SEGURO (que muda a conta de verdade). Ver delcache().
# Conjunto COMPLETO validado contra a API do AdsPower (todos retornam code:0) -> wipe total
# antes de injetar novo proxy+cookie.
DELCACHE_TIPOS = ["cache", "cookie", "history", "local_storage", "session_storage",
                  "indexeddb", "service_worker", "cache_storage", "file_system",
                  "web_sql", "form_data", "password", "media_licenses", "download_history"]
DELCACHE_TIPOS_SEGURO = ["cookie", "local_storage", "indexeddb"]
_delcache_full_ok = True   # latch: desliga o _FULL na sessao se for recusado (evita custo duplo)
BROWSER_VERSION = "146"
COOKIE_EXP  = 1893456000    # expiracao do auth-token (unix seg, futuro)

# Arquivos de runtime resolvidos pela base (funciona empacotado em .exe tambem).
ARQ_TOKENS  = paths.arquivo("tokens.txt")        # 1 auth-token por linha (apelido,token opcional)
ARQ_CONTAS  = paths.arquivo("contas_pool.json")  # GERADO a partir de tokens.txt
ARQ_PROXIES = paths.arquivo("proxies_pool.txt")  # host:port ou host:port:user:pass (1/linha)

def aplicar_config_adspower():
    """Re-le o settings.json (apos a GUI salvar) e atualiza BASE/API_KEY/filtros."""
    global BASE, API_KEY, PERFIS_GROUP_ID, PERFIS_FILTRO_NOME
    config_store.recarregar()
    BASE = config_store.get("adspower", "base", BASE)
    API_KEY = config_store.get("adspower", "api_key", API_KEY)
    PERFIS_GROUP_ID = config_store.get("adspower", "group_id", "")
    PERFIS_FILTRO_NOME = config_store.get("adspower", "filtro_nome", "")

# Pool de fingerprints COERENTES (GPU + resolucoes + nucleos plausiveis juntos).
FINGERPRINTS = [
    {"vendor": "Google Inc. (Intel)",  "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "resolutions": ["1920_1080", "1536_864", "1366_768"], "cores": [4, 8]},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "resolutions": ["1920_1080", "2560_1440"], "cores": [8, 12, 16]},
    {"vendor": "Google Inc. (AMD)",    "renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "resolutions": ["1920_1080", "1600_900"], "cores": [8, 12]},
]

# --------------------------- AdsPower API ---------------------------
def _post(path, payload):
    r = requests.post(f"{BASE}{path}", params={"api_key": API_KEY},
                      json=payload, headers={"Content-Type": "application/json"}, timeout=60)
    try:
        return r.json()
    except Exception:
        return {"code": -1, "msg": f"HTTP {r.status_code}: {r.text[:120]}"}

def _get(path, params):
    params = {**params, "api_key": API_KEY}
    r = requests.get(f"{BASE}{path}", params=params, timeout=90)
    try:
        return r.json()
    except Exception:
        return {"code": -1, "msg": f"HTTP {r.status_code}: {r.text[:120]}"}

def stop(uid):
    return _get("/api/v1/browser/stop", {"user_id": uid})

def delcache(uid):
    """Limpa o perfil antes de injetar novo proxy+cookie. Tenta o conjunto FULL (zera tudo);
    se o AdsPower recusar (tipo invalido), cai pro SEGURO e trava o FULL p/ a sessao (evita
    pagar 2 chamadas por ciclo)."""
    global _delcache_full_ok
    if _delcache_full_ok:
        r = _post("/api/v2/browser-profile/delete-cache",
                  {"profile_id": [uid], "type": DELCACHE_TIPOS})
        if r.get("code") == 0:
            return r
        r2 = _post("/api/v2/browser-profile/delete-cache",
                   {"profile_id": [uid], "type": DELCACHE_TIPOS_SEGURO})
        if r2.get("code") == 0:        # full falhou mas seguro passou => tipo invalido
            _delcache_full_ok = False
        return r2                       # se ambos falharam (ex.: perfil aberto), abrir_perfil re-tenta
    return _post("/api/v2/browser-profile/delete-cache",
                 {"profile_id": [uid], "type": DELCACHE_TIPOS_SEGURO})

def cookie_authtoken(val):
    return [{"id": 1, "name": "auth-token", "value": val, "domain": ".twitch.tv",
             "path": "/", "secure": True, "httpOnly": False,
             "sameSite": "no_restriction", "expirationDate": COOKIE_EXP}]

def random_fp_config():
    fp = random.choice(FINGERPRINTS)
    os_v = random.choice(["Windows 10", "Windows 11"])
    return {
        "automatic_timezone": "1", "language_switch": "1", "page_language_switch": "1",
        "location": "allow", "location_switch": "1", "webrtc": "proxy",
        "screen_resolution": random.choice(fp["resolutions"]),
        "fonts": ["all"],
        "canvas": "1", "webgl_image": "1", "webgl": "2", "audio": "1",
        "media_devices": "1", "client_rects": "1", "speech_switch": "1",
        "hardware_concurrency": str(random.choice(fp["cores"])), "device_memory": "8",
        "webgl_config": {"unmasked_vendor": fp["vendor"], "unmasked_renderer": fp["renderer"],
                         "webgpu": {"webgpu_switch": "1"}},
        "browser_kernel_config": {"version": BROWSER_VERSION, "type": "chrome"},
        "random_ua": {"ua_browser": ["chrome"], "ua_version": [BROWSER_VERSION],
                      "ua_system_version": [os_v]},
        "flash": "block",
    }

def proxy_config(proxy):
    parts = proxy.split(":")
    host, port = parts[0], parts[1]
    user = parts[2] if len(parts) > 2 else ""
    pw   = parts[3] if len(parts) > 3 else ""
    return {"proxy_soft": "other", "proxy_type": "socks5",
            "proxy_host": host, "proxy_port": port,
            "proxy_user": user, "proxy_password": pw}

def update(uid, auth_token, proxy):
    return _post("/api/v1/user/update", {
        "user_id": uid,
        "open_urls": [HOME],
        "user_proxy_config": proxy_config(proxy),
        "cookie": json.dumps(cookie_authtoken(auth_token), ensure_ascii=False),
        "fingerprint_config": random_fp_config(),
        "ignore_cookie_error": "1",
    })

def start(uid):
    return _get("/api/v1/browser/start", {"user_id": uid, "open_tabs": 0, "ip_tab": 0})

# --------------------------- Orquestrador ---------------------------
_print_lock = threading.Lock()
def log(msg):
    with _print_lock:
        print(msg, flush=True)

def abrir_um(uid, conta, proxy):
    """stop -> delcache -> update(cookie+proxy+fp) -> start. Retorna True se start OK."""
    stop(uid); time.sleep(1)
    d = delcache(uid)
    if d.get("code") != 0:
        log(f"  [{uid}] delcache FALHOU: {d.get('msg')}"); return False
    d = update(uid, conta["auth_token"], proxy)
    if d.get("code") != 0:
        log(f"  [{uid}] update FALHOU: {d.get('msg')}"); return False
    d = start(uid)
    if d.get("code") != 0:
        log(f"  [{uid}] start FALHOU: {d.get('msg')}"); return False
    return True

def slot_loop(uid, contas_q, proxies_q, stop_event):
    while not stop_event.is_set():
        conta = contas_q.get()          # bloqueia ate ter conta livre
        proxy = proxies_q.get()         # bloqueia ate ter proxy livre
        try:
            ok = abrir_um(uid, conta, proxy)
            if ok:
                dur = random.randint(SESSAO_MIN_S, SESSAO_MAX_S)
                log(f"  [{uid}] ON conta={conta['id']} proxy={proxy.split(':')[0]} por {dur//60}min")
                stop_event.wait(dur)    # dorme a sessao (interrompivel)
                stop(uid)
                log(f"  [{uid}] OFF conta={conta['id']}")
            else:
                time.sleep(5)           # falhou: respira antes de tentar outra
        finally:
            contas_q.put(conta)         # devolve pra pool (vai pro fim da fila = rotaciona)
            proxies_q.put(proxy)

def construir_contas_pool():
    """Le tokens.txt e (re)gera contas_pool.json. 1 auth-token por linha; linhas vazias
    e '#comentario' ignoradas; tokens duplicados removidos (mantem a 1a ocorrencia).
    Aceita 'apelido,token' (opcional); sem apelido, gera c001, c002, ...
    Retorna a lista de contas, ou None se tokens.txt nao existir (cai no json existente)."""
    try:
        with open(ARQ_TOKENS, encoding="utf-8") as f:
            linhas = [l.strip() for l in f]
    except FileNotFoundError:
        return None
    contas, vistos, n = [], set(), 0
    for linha in linhas:
        if not linha or linha.startswith("#"):
            continue
        apelido, token = None, linha
        if "," in linha:
            a, t = linha.split(",", 1)
            apelido, token = a.strip(), t.strip()
        token = token.strip()
        if not token or token in vistos:
            continue
        vistos.add(token)
        n += 1
        contas.append({"id": apelido or f"c{n:03d}", "auth_token": token})
    with open(ARQ_CONTAS, "w", encoding="utf-8") as f:
        json.dump(contas, f, ensure_ascii=False, indent=2)
    return contas

def _idx_nome(nome):
    """'MURI 012' -> 12; sem numero -> grande (vai pro fim). Ordenacao estavel."""
    m = re.search(r"(\d+)", nome or "")
    try:
        return int(m.group(1)) if m else 10 ** 9
    except (TypeError, ValueError):
        return 10 ** 9

def listar_grupos(timeout=30):
    """Lista os grupos do AdsPower via /api/v1/group/list. Retorna
    [{"group_id","group_name"}]. Levanta RuntimeError se a API responder erro."""
    PAGE = 100
    url = f"{BASE}/api/v1/group/list"
    grupos, pagina = [], 1
    while True:
        params = {"api_key": API_KEY, "page": pagina, "page_size": PAGE}
        r = requests.get(url, params=params, timeout=timeout)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(d.get("msg", "erro group/list"))
        data = d.get("data") or {}
        lista = data.get("list") or []
        if not lista:
            break
        for g in lista:
            gid = g.get("group_id") or g.get("id") or ""
            grupos.append({"group_id": str(gid), "group_name": g.get("group_name", "")})
        total = int(data.get("total", len(grupos)))
        if len(grupos) >= total or len(lista) < PAGE:
            break
        pagina += 1
    return grupos

def _coletar_perfis(group_id, timeout):
    """Pagina /api/v1/user/list (de UM group_id, ou todos se vazio). Retorna [{user_id,name}]."""
    PAGE = 100
    url = f"{BASE}/api/v1/user/list"
    todos, pagina = [], 1
    while True:
        params = {"api_key": API_KEY, "page": pagina, "page_size": PAGE}
        if group_id:
            params["group_id"] = group_id
        r = requests.get(url, params=params, timeout=timeout)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(d.get("msg", "erro user/list"))
        data = d.get("data") or {}
        lista = data.get("list") or []
        if not lista:
            break
        for p in lista:
            uid = p.get("user_id") or p.get("id") or ""
            if uid:
                todos.append({"user_id": uid, "name": p.get("name", "")})
        total = int(data.get("total", len(todos)))
        if len(todos) >= total:
            break
        pagina += 1
        time.sleep(1.2)          # AdsPower limita ~1 req/s -> espaca as paginas
    return todos


def listar_perfis(group_id=None, filtro_nome=None, timeout=30):
    """Detecta os perfis do AdsPower. group_id pode ser "" (TODOS), um id, ou VARIOS
    separados por virgula (ex.: "9906242,9906199") -> consulta cada grupo e mescla (dedup).
    Retorna user_ids ordenados pelo numero no nome (MURI 012 -> 12). group_id/filtro_nome=None
    -> usa os globais (atualizados pelo config)."""
    if group_id is None:
        group_id = PERFIS_GROUP_ID
    if filtro_nome is None:
        filtro_nome = PERFIS_FILTRO_NOME
    gids = [g.strip() for g in str(group_id or "").split(",") if g.strip()]
    if not gids:
        todos = _coletar_perfis("", timeout)              # nenhum selecionado = todos os grupos
    else:
        todos, vistos = [], set()
        for i, gid in enumerate(gids):                     # mescla varios grupos, sem duplicar
            if i:
                time.sleep(1.2)                            # espaca as chamadas (rate limit AdsPower)
            for p in _coletar_perfis(gid, timeout):
                if p["user_id"] not in vistos:
                    vistos.add(p["user_id"]); todos.append(p)
    if filtro_nome:
        pref = filtro_nome.strip().lower()
        todos = [p for p in todos if p["name"].lower().startswith(pref)]
    todos.sort(key=lambda p: (_idx_nome(p["name"]), p["name"]))
    return [p["user_id"] for p in todos]

def carregar():
    contas = construir_contas_pool()        # tokens.txt -> contas_pool.json
    if contas is None:                       # sem tokens.txt: usa o json existente
        log(f"{ARQ_TOKENS} nao encontrado — usando {ARQ_CONTAS} existente.")
        with open(ARQ_CONTAS, encoding="utf-8") as f:
            contas = json.load(f)
    else:
        log(f"{len(contas)} contas geradas de {ARQ_TOKENS} -> {ARQ_CONTAS}")
    with open(ARQ_PROXIES, encoding="utf-8") as f:
        proxies = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    # Perfis: detectados via AdsPower API (sem fallback).
    perfis = []
    try:
        perfis = listar_perfis()
        log(f"{len(perfis)} perfis detectados via AdsPower API")
    except Exception as e:
        log(f"falha ao listar perfis via API: {str(e)[:100]}")
    return contas, proxies, perfis

def main():
    contas, proxies, perfis = carregar()
    log(f"Perfis={len(perfis)}  Contas={len(contas)}  Proxies={len(proxies)}")
    if len(proxies) < len(perfis):
        log(f"AVISO: menos proxies ({len(proxies)}) que perfis ({len(perfis)}) — slots vao esperar proxy livre.")

    contas_q, proxies_q = queue.Queue(), queue.Queue()
    for c in contas:  contas_q.put(c)
    for p in proxies: proxies_q.put(p)

    stop_event = threading.Event()
    threads = [threading.Thread(target=slot_loop, args=(u, contas_q, proxies_q, stop_event),
                                daemon=True) for u in perfis]
    for t in threads: t.start()
    log("Rodando. Ctrl+C para parar.")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("\nParando — fechando perfis...")
        stop_event.set()
        for u in perfis: stop(u)
        log("OK.")

if __name__ == "__main__":
    main()
