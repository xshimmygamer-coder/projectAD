"""
LOGIN ADSPOWER — script ISOLADO (separado da GUI do MURIADS).

Objetivo: logar contas nos perfis do AdsPower e DEIXAR OS PERFIS ABERTOS (warmup /
verificacao manual). NAO faz rodizio, NAO seta proxy, NAO mexe no fingerprint e
NAO fecha o perfil depois de abrir.

Receita de injecao (a mesma validada no swap.py do MURIADS), por perfil FECHADO:
    stop -> delete-cache (V2, por perfil) -> user/update (cookie auth-token + open_urls)
    -> browser/start   ... e PRONTO: deixa aberto.
O delete-cache e a peca-chave: cookies/localStorage antigos persistem em disco, e sem
limpar o Twitch le a sessao ANTIGA (a conta nao troca).

Diferencas vs. o swap.py:
    - sem `user_proxy_config`  (os proxies ja estao setados nos perfis)
    - sem `fingerprint_config` (login puro — mantem o fingerprint atual do perfil)
    - NAO chama stop no fim (mantem o navegador aberto)

Uso:  python "LOGIN ADSPOWER/login_adspower.py" [limite_opcional]
      (AdsPower aberto, Local API em local.adspower.net:50325; settings.json com a api_key)
"""
import json
import os
import sys
import time

# Reusa a logica de API do projeto (swap.py na raiz). Insere a pasta PAI no sys.path
# para importar os modulos do MURIADS mesmo rodando de dentro de "LOGIN ADSPOWER/".
_AQUI = os.path.dirname(os.path.abspath(__file__))
_RAIZ = os.path.dirname(_AQUI)
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402

import swap  # noqa: E402  (precisa do sys.path acima)

# --- FIX raiz do WinError 10048 (esgotamento de portas efemeras) ---------------
# O swap.py usa requests.post/get SOLTOS -> cada chamada abre um socket NOVO e o deixa em
# TIME_WAIT; em ~4 chamadas/perfil x N perfis o range dinamico de portas esgota e estoura
# "Only one usage of each socket address". Solucao: UMA requests.Session com keep-alive,
# reusada por TODAS as chamadas. Como o swap chama `requests.post`/`requests.get` (e a Session
# expoe os mesmos metodos), basta apontar o nome `requests` do swap p/ a nossa Session.
SESSION = requests.Session()
_retry = Retry(total=3, connect=3, backoff_factor=0.5,
               status_forcelist=(429, 500, 502, 503, 504),
               allowed_methods=frozenset(["GET", "POST"]))
_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=_retry)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)
swap.requests = SESSION   # monkeypatch: swap._post/_get/listar_perfis passam a reusar a conexao

# auth-token.txt fica AO LADO deste script (nao usa paths.base_dir, que aponta p/ a raiz).
ARQ_TOKENS = os.path.join(_AQUI, "auth-token.txt")
PAUSA_ENTRE_ABERTURAS_S = 1.5   # suaviza API/CDP entre uma abertura e a proxima
API_MIN_INTERVALO_S = 0.6       # intervalo minimo entre chamadas AdsPower (anti-burst)
_ult_chamada = [0.0]            # timestamp da ultima chamada (lista p/ mutar no closure)


def _throttle():
    """Garante >= API_MIN_INTERVALO_S desde a ultima chamada AdsPower (espelha _ads() do
    orquestrador). Suaviza o burst que ajuda a saturar a Local API."""
    espera = API_MIN_INTERVALO_S - (time.monotonic() - _ult_chamada[0])
    if espera > 0:
        time.sleep(espera)
    _ult_chamada[0] = time.monotonic()


def ler_tokens(arquivo):
    """Le auth-token.txt: 1 token por linha; ignora vazias e '#'; dedup (1a ocorrencia);
    aceita 'apelido,token' (apelido so p/ log). Retorna [{"id","auth_token"}].
    Espelha swap.construir_contas_pool, mas SEM gravar contas_pool.json."""
    try:
        with open(arquivo, encoding="utf-8") as f:
            linhas = [l.strip() for l in f]
    except FileNotFoundError:
        return []
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
    return contas


def update_sem_proxy(uid, auth_token):
    """user/update injetando SO o cookie auth-token + open_urls (home da Twitch).
    SEM user_proxy_config e SEM fingerprint_config -> AdsPower mantem proxy/fp atuais."""
    return swap._post("/api/v1/user/update", {
        "user_id": uid,
        "open_urls": [swap.HOME],
        "cookie": json.dumps(swap.cookie_authtoken(auth_token), ensure_ascii=False),
        "ignore_cookie_error": "1",
    })


def abrir_e_manter(uid, conta):
    """stop -> delcache -> update(cookie) -> start. Em sucesso NAO fecha (mantem aberto).
    Todo erro (inclusive excecao de rede) vira False + log — nunca derruba o lote."""
    tag = f"[{conta['id']} / {uid}]"
    try:
        _throttle(); swap.stop(uid)
        time.sleep(1)
        _throttle(); d = swap.delcache(uid)
        if d.get("code") != 0:
            print(f"  {tag} delcache FALHOU: {d.get('msg')}", flush=True)
            return False
        _throttle(); d = update_sem_proxy(uid, conta["auth_token"])
        if d.get("code") != 0:
            print(f"  {tag} update FALHOU: {d.get('msg')}", flush=True)
            return False
        _throttle(); d = swap.start(uid)
        if d.get("code") != 0:
            print(f"  {tag} start FALHOU: {d.get('msg')}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"  {tag} ERRO: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return False


def main():
    swap.aplicar_config_adspower()   # carrega BASE/API_KEY/group_id do settings.json

    contas = ler_tokens(ARQ_TOKENS)
    if not contas:
        print(f"Nenhum token em {ARQ_TOKENS}. Cole 1 auth-token por linha e rode de novo.")
        return
    print(f"{len(contas)} token(s) lido(s) de {ARQ_TOKENS}")

    try:
        perfis = swap.listar_perfis()
    except Exception as e:
        print(f"Falha ao listar perfis via AdsPower API: {str(e)[:120]}")
        return
    print(f"{len(perfis)} perfil(s) detectado(s) via AdsPower API")

    if not perfis:
        print("Sem perfis detectados (cheque api_key/group_id no settings.json e o AdsPower aberto).")
        return

    n = min(len(contas), len(perfis))
    # limite opcional por argv (ex.: abrir so os 5 primeiros)
    if len(sys.argv) > 1:
        try:
            n = min(n, int(sys.argv[1]))
        except ValueError:
            print(f"Argumento '{sys.argv[1]}' invalido — ignorando o limite.")
    if len(contas) > len(perfis):
        print(f"AVISO: {len(contas) - len(perfis)} token(s) sem perfil correspondente — nao serao logados.")
    elif len(perfis) > len(contas):
        print(f"AVISO: {len(perfis) - len(contas)} perfil(s) sem token — ficarao de fora.")
    print(f"Logando {n} conta(s) em {n} perfil(s) — pareamento sequencial. Os perfis ficam ABERTOS.\n")

    ok = 0
    for i in range(n):
        conta, uid = contas[i], perfis[i]
        print(f"[{i + 1}/{n}] conta={conta['id']} -> perfil={uid}: injetando cookie + abrindo...",
              flush=True)
        if abrir_e_manter(uid, conta):
            ok += 1
            print(f"  OK — perfil {uid} ABERTO e logado.", flush=True)
        else:
            print(f"  FALHA — perfil {uid} nao abriu (segue para o proximo).", flush=True)
        if i < n - 1:
            time.sleep(PAUSA_ENTRE_ABERTURAS_S)

    falhas = n - ok
    print(f"\nPronto: {ok} de {n} perfil(s) logado(s) e ABERTO(s)"
          + (f" — {falhas} FALHA(s) (rode de novo p/ tentar os que faltaram)." if falhas else ".")
          + "\nOs navegadores continuam abertos (este script NAO os fecha).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Mantem aberto de proposito: NAO fecha os perfis ja abertos ao interromper.
        print("\nInterrompido. Os perfis ja abertos continuam abertos.")
