"""
proxy_checker.py — checker de REPUTACAO de proxies (portado do MURIPRO2).

Pipeline por proxy (SOCKS5/HTTP), igual ao MURIPRO2:
  1) ECHO do IP de saida via proxy  (echo_url configuravel; default ipinfo.io/ip)
     -> prova que roteia + captura o IP de saida. (E o mesmo "echo de IP" do IP Watch.)
  2) IPinfo MASTER gate  (api.ipinfo.io/lookup/{ip}, Bearer token)
     -> reprova se QUALQUER flag: is_vpn/is_proxy/is_tor/is_relay/is_res_proxy/is_hosting
        ou geo fora do permitido. Sem token nao roda (reprova por erro de API).
  3) DNSBL  (6 listas, dnspython) — sanity check final.
  4) AbuseIPDB (opcional, multi-key) — score >= limite reprova.

Diferenca p/ o MURIPRO2: aqui NAO ha config global nem subprocess/stdout. E um modulo
IMPORTAVEL: a GUI chama `checar_lista(proxies, cfg, progress_cb, parar)` numa thread.

  aprovados, reprovados = checar_lista(proxies, cfg, progress_cb=None, parar=None)
    - proxies : lista de strings "host:port" | "host:port:user:senha" | "socks5://..."
    - cfg     : dict (ver _norm_cfg p/ chaves/defaults)
    - progress_cb(feito, total, status, proxy, info): chamado a cada proxy concluido.
    - parar   : threading.Event p/ cancelar no meio.
    - retorno : (aprovados, reprovados) — listas de dicts {status, proxy, info, ip_saida, bruto}
                status: "BOM" (aprovado) | "RUIM" (reprovado) | "ERRO" (falha de conexao/echo)

Deps: requests[socks] (PySocks p/ SOCKS5) + dnspython.
"""

import concurrent.futures
import threading
import time

import requests


# ── DNSBLs (mesmas do MURIPRO2) ──────────────────────────────────────────────
DNSBL_ZONES = [
    ("SpamCop",      "bl.spamcop.net"),
    ("Barracuda",    "b.barracudacentral.org"),
    ("PSBL",         "psbl.surriel.com"),
    ("UCEPROTECT-1", "dnsbl-1.uceprotect.net"),
    ("Mailspike",    "bl.mailspike.net"),
    ("DroneBL",      "dnsbl.dronebl.org"),
]

# backoff das keys do AbuseIPDB (apos 429/cota) — global, protegido por lock
_abuse_backoff = {}
_abuse_lock = threading.Lock()


# ── CONFIG ───────────────────────────────────────────────────────────────────
def _norm_cfg(cfg):
    """Aplica defaults sobre o dict de config vindo do settings.json["proxy_check"]."""
    cfg = dict(cfg or {})
    def _int(k, d):
        try:
            return int(float(cfg.get(k, d)))
        except (TypeError, ValueError):
            return d
    keys = cfg.get("abuseipdb_keys") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    return {
        "protocolo_default": (cfg.get("protocolo_default") or "socks5").strip().lower(),
        "echo_url":          (cfg.get("echo_url") or "https://ipinfo.io/ip").strip(),
        "ipinfo_token":      (cfg.get("ipinfo_token") or "").strip(),
        "timeout_proxy":     _int("timeout_proxy", 12),
        "timeout_api":       _int("timeout_api", 8),
        "threads":           max(1, _int("threads", 20)),
        "dnsbl_enabled":     bool(cfg.get("dnsbl_enabled", True)),
        "dnsbl_timeout":     _int("dnsbl_timeout", 4),
        "dns_servers":       list(cfg.get("dns_servers") or []),
        "abuseipdb_keys":    [k for k in keys if k],
        "abuseipdb_enabled": bool(cfg.get("abuseipdb_enabled", False)) and bool([k for k in keys if k]),
        "abuseipdb_score_max": _int("abuseipdb_score_max", 50),
        "paises":            [str(x).strip() for x in (cfg.get("paises") or []) if str(x).strip()],
        "estados":           [str(x).strip() for x in (cfg.get("estados") or []) if str(x).strip()],
        "zipcodes":          [str(x).strip() for x in (cfg.get("zipcodes") or []) if str(x).strip()],
    }


# ── PARSE DO PROXY (do MURIPRO2: formatar_proxy) ─────────────────────────────
def formatar_proxy(linha, protocolo_default="socks5"):
    """Detecta o protocolo pelo prefixo e devolve URL pronta pro requests."""
    linha = (linha or "").strip()
    if linha.startswith("socks5://"):
        return linha.replace("socks5://", "socks5h://", 1)
    if linha.startswith(("socks5h://", "http://", "https://")):
        return linha
    partes = linha.split(":")
    if len(partes) == 2:
        host, port = partes
        auth = ""
    elif len(partes) == 4:
        host, port, user, pwd = partes
        auth = f"{user}:{pwd}@"
    else:
        return None
    esquema = "socks5h" if protocolo_default == "socks5" else protocolo_default
    return f"{esquema}://{auth}{host}:{port}"


# ── ECHO do IP de saida (do IP Watch: medir_ip_saida) ────────────────────────
def medir_ip_saida(proxies_cfg, echo_url, timeout, tentativas=2):
    """Faz GET no echo_url ATRAVES do proxy e devolve (ip, erro).
    erro: None=ok; "429"=echo saturado (rate-limit); "timeout"/"<motivo>"=falha."""
    ultimo = "sem resposta"
    for _ in range(max(1, tentativas)):
        try:
            r = requests.get(echo_url, proxies=proxies_cfg, timeout=timeout)
            if r.status_code == 429:
                return None, "429"
            if r.status_code != 200:
                ultimo = f"HTTP {r.status_code}"
                continue
            ip = (r.text or "").strip().strip('"')
            if ip:
                return ip, None
            ultimo = "resposta vazia"
        except requests.exceptions.Timeout:
            ultimo = "timeout"
        except Exception as e:
            ultimo = f"{type(e).__name__}: {str(e)[:60]}"
    return None, ultimo


# ── CONSULTAS (IPinfo / DNSBL / AbuseIPDB) ───────────────────────────────────
def consultar_ipinfo(ip, token, timeout):
    """JSON do IPinfo (api.ipinfo.io/lookup/{ip}, Bearer) ou {'_erro': ...}."""
    if not token or token.startswith("SUA_KEY"):
        return {"_erro": "IPINFO_TOKEN nao configurado"}
    try:
        r = requests.get(f"https://api.ipinfo.io/lookup/{ip}",
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/json"},
                         timeout=timeout)
        if r.status_code != 200:
            return {"_erro": f"HTTP {r.status_code}", "_body": r.text[:200]}
        return r.json()
    except Exception as e:
        return {"_erro": f"{type(e).__name__}: {str(e)[:80]}"}


def _ip_reverso(ip):
    return ".".join(reversed(ip.split(".")))


def _dns_a_query(resolver, host):
    """'listed:<ips>' se tiver A record, 'clean' se NXDOMAIN/NoAnswer, ou 'erro:<tipo>'."""
    try:
        import dns.resolver as _dr
        import dns.exception as _de
        ans = resolver.resolve(host, "A")
        return "listed:" + ",".join(str(r) for r in ans)
    except _dr.NXDOMAIN:
        return "clean"
    except _dr.NoAnswer:
        return "clean"
    except _de.Timeout:
        return "erro:timeout"
    except _dr.NoNameservers:
        return "erro:no_nameservers"
    except Exception as e:
        return f"erro:{type(e).__name__}"


def consultar_dnsbl(ip, timeout=4, dns_servers=None):
    """Consulta as 6 DNSBLs em paralelo. Retorna {nome: 'listed:...'|'clean'|'erro:...'}
    ou {'_erro': ...}."""
    try:
        import dns.resolver
    except ImportError:
        return {"_erro": "dnspython nao instalado (pip install dnspython)"}
    if not ip or ip.count(".") != 3:
        return {"_erro": f"IP invalido: {ip}"}
    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    if dns_servers:
        resolver.nameservers = list(dns_servers)
    rev = _ip_reverso(ip)
    resultados = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(DNSBL_ZONES)) as ex:
        futs = {ex.submit(_dns_a_query, resolver, f"{rev}.{zona}"): nome
                for nome, zona in DNSBL_ZONES}
        for f in concurrent.futures.as_completed(futs):
            resultados[futs[f]] = f.result()
    return resultados


def consultar_abuseipdb(ip, keys, timeout):
    """abuseConfidenceScore (0-100). MULTI-KEY com backoff em 429. FAIL-OPEN:
    sem-key/exauridas/erro -> (-1, -1) (nao reprova)."""
    if not keys:
        return (-1, -1)
    agora = time.time()
    for key in keys:
        with _abuse_lock:
            if _abuse_backoff.get(key, 0) > agora:
                continue
        try:
            r = requests.get("https://api.abuseipdb.com/api/v2/check",
                             headers={"Key": key, "Accept": "application/json"},
                             params={"ipAddress": ip, "maxAgeInDays": 90}, timeout=timeout)
            if r.status_code == 429:
                with _abuse_lock:
                    _abuse_backoff[key] = agora + 3600
                continue
            d = r.json().get("data", {})
            return (int(d.get("abuseConfidenceScore", 0) or 0),
                    int(d.get("totalReports", 0) or 0))
        except Exception:
            continue
    return (-1, -1)


# ── WORKER POR PROXY (do MURIPRO2: checar_proxy) ─────────────────────────────
def checar_proxy(proxy_original, cfg, parar=None):
    if parar is not None and parar.is_set():
        return {"status": "ERRO", "proxy": proxy_original, "info": "cancelado", "ip_saida": None}

    proxy_fmt = formatar_proxy(proxy_original, cfg["protocolo_default"])
    if not proxy_fmt:
        return {"status": "ERRO", "proxy": proxy_original, "info": "Formato invalido", "ip_saida": None}
    proxies_cfg = {"http": proxy_fmt, "https": proxy_fmt}

    # 1) ECHO: IP de saida via proxy
    ip_saida, erro = medir_ip_saida(proxies_cfg, cfg["echo_url"], cfg["timeout_proxy"])
    if erro == "429":
        return {"status": "ERRO", "proxy": proxy_original, "ip_saida": None,
                "info": "Echo 429 (rate-limit) — use um echo URL privado p/ lotes grandes"}
    if not ip_saida:
        return {"status": "ERRO", "proxy": proxy_original, "ip_saida": None,
                "info": f"Sem IP de saida ({erro})"}

    # 2) IPinfo MASTER (gate-keeper)
    js = consultar_ipinfo(ip_saida, cfg["ipinfo_token"], cfg["timeout_api"])
    pais   = js.get("country", "N/A")
    estado = js.get("region", "N/A")
    zipc   = js.get("postal", "N/A")
    cidade = js.get("city", "N/A")
    anon   = js.get("anonymous") if isinstance(js.get("anonymous"), dict) else {}
    f_vpn   = bool(anon.get("is_vpn"))
    f_proxy = bool(anon.get("is_proxy"))
    f_tor   = bool(anon.get("is_tor"))
    f_relay = bool(anon.get("is_relay"))
    f_res   = bool(anon.get("is_res_proxy"))
    f_host  = bool(js.get("is_hosting"))
    f_mob   = bool(js.get("is_mobile"))
    asn = js.get("as", {}) if isinstance(js.get("as"), dict) else {}
    asn_name = asn.get("name", js.get("hostname", "N/A"))

    motivo = None
    if "_erro" in js:
        motivo = f"IPinfo erro: {js['_erro']}"
    elif "anonymous" not in js:
        motivo = "IPinfo sem campo 'anonymous' (token sem permissao Privacy?)"

    if not motivo:
        flags = [n for n, v in (("vpn", f_vpn), ("proxy", f_proxy), ("tor", f_tor),
                                ("relay", f_relay), ("res_proxy", f_res), ("hosting", f_host)) if v]
        if flags:
            motivo = f"IPinfo flags=[{','.join(flags)}]"

    if not motivo:
        if cfg["paises"] and pais.upper() not in [p.upper() for p in cfg["paises"]]:
            motivo = f"Pais {pais} fora da lista"
        elif cfg["estados"] and estado.upper() not in [e.upper() for e in cfg["estados"]]:
            motivo = f"Estado {estado} fora da lista"
        elif cfg["zipcodes"] and str(zipc).strip() not in cfg["zipcodes"]:
            motivo = f"ZIP {zipc} fora da lista"

    resumo_flags = (f"IPinfo[vpn={int(f_vpn)},proxy={int(f_proxy)},tor={int(f_tor)},"
                    f"relay={int(f_relay)},res={int(f_res)},host={int(f_host)},mob={int(f_mob)}]")

    # SHORT-CIRCUIT: IPinfo ja reprovou -> nem consulta DNSBL
    if motivo:
        bruto = {"proxy": proxy_original, "ip_saida": ip_saida, "ipinfo": js,
                 "dnsbl": {"_skipped": "ipinfo ja reprovou"}}
        info = f"{motivo} | {pais}:{ip_saida} | {resumo_flags} | DNSBL:skip | {asn_name}"
        return {"status": "RUIM", "proxy": proxy_original, "ip_saida": ip_saida,
                "info": info, "bruto": bruto}

    # 3) DNSBL
    dnsbl_res = consultar_dnsbl(ip_saida, cfg["dnsbl_timeout"], cfg["dns_servers"]) \
        if cfg["dnsbl_enabled"] else {"_skipped": "DNSBL off"}
    listadas = []
    if isinstance(dnsbl_res, dict) and "_erro" not in dnsbl_res:
        listadas = [n for n, v in dnsbl_res.items()
                    if isinstance(v, str) and v.startswith("listed")]
    if cfg["dnsbl_enabled"]:
        if isinstance(dnsbl_res, dict) and "_erro" in dnsbl_res:
            motivo = f"DNSBL erro: {dnsbl_res['_erro']}"
        elif listadas:
            motivo = f"DNSBL listed em: {', '.join(listadas)}"

    # 4) AbuseIPDB (opcional, fail-open)
    api_resumo = ""
    if not motivo and cfg["abuseipdb_enabled"]:
        score, _rep = consultar_abuseipdb(ip_saida, cfg["abuseipdb_keys"], cfg["timeout_api"])
        if score >= 0:
            api_resumo = f" | AbuseIPDB={score}"
        if score >= cfg["abuseipdb_score_max"]:
            motivo = f"AbuseIPDB score={score} (>= {cfg['abuseipdb_score_max']})"

    dnsbl_resumo = ("OK" if (cfg["dnsbl_enabled"] and not listadas
                             and isinstance(dnsbl_res, dict) and "_erro" not in dnsbl_res)
                    else (",".join(listadas) if listadas else "skip"))
    info = (f"{pais}/{estado}/{cidade}:{ip_saida} | {resumo_flags} | "
            f"DNSBL:{dnsbl_resumo} | {asn_name}{api_resumo}")
    bruto = {"proxy": proxy_original, "ip_saida": ip_saida, "ipinfo": js, "dnsbl": dnsbl_res}

    if motivo:
        return {"status": "RUIM", "proxy": proxy_original, "ip_saida": ip_saida,
                "info": f"{motivo} | {info}", "bruto": bruto}
    return {"status": "BOM", "proxy": proxy_original, "ip_saida": ip_saida,
            "info": info, "bruto": bruto}


# ── RUNNER (lista) ───────────────────────────────────────────────────────────
def checar_lista(proxies, cfg, progress_cb=None, parar=None):
    """Checa a lista em paralelo. Retorna (aprovados, reprovados). Dedup preservando ordem.
    Cancela com `parar` (threading.Event): para de iniciar novas e ignora as restantes."""
    cfg = _norm_cfg(cfg)
    vistos, lista = set(), []
    for p in proxies:
        p = (p or "").strip()
        if p and not p.startswith("#") and p not in vistos:
            vistos.add(p)
            lista.append(p)
    total = len(lista)
    aprovados, reprovados = [], []
    if total == 0:
        return aprovados, reprovados

    feito = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["threads"]) as ex:
        futs = {ex.submit(checar_proxy, p, cfg, parar): p for p in lista}
        for fut in concurrent.futures.as_completed(futs):
            if parar is not None and parar.is_set():
                for f in futs:
                    f.cancel()   # evita iniciar os que ainda estao na fila
                break
            try:
                r = fut.result()
            except Exception as e:
                r = {"status": "ERRO", "proxy": futs[fut], "ip_saida": None,
                     "info": f"{type(e).__name__}: {str(e)[:60]}"}
            feito += 1
            (aprovados if r["status"] == "BOM" else reprovados).append(r)
            if progress_cb:
                try:
                    progress_cb(feito, total, r["status"], r["proxy"], r.get("info", ""))
                except Exception:
                    pass
    return aprovados, reprovados
