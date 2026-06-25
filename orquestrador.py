"""
Orquestrador CDP — fluxo completo:
  1. le tokens.txt (cookies) + proxies_pool.txt + perfis.txt
  2. abre X perfis (delcache -> update[cookie+proxy+fingerprint] -> start), com start
     ESCALONADO (STAGGER_START_S) e RATE LIMIT global na API AdsPower (<120 RPM)
  3. conecta via CDP, CHECA a rede do proxy (residencial cai muito) — sem rede:
     fecha o perfil e reinicia o ciclo com outro proxy; com rede: NAVEGA pro canal
     alvo (sidebar -> busca -> URL)
  4. fica no canal por SESSAO_MIN..MAX s; SE cair pausa comercial: nunca fecha no meio
     do anuncio e, ao terminar, fecha apos espera RANDOMIZADA (GRACE_POS_AD_MIN..MAX) —
     IGNORANDO o tempo base (pode fechar antes do minimo)
  5. fecha o perfil, devolve conta+proxy pra pool, repete (rotaciona IP e cookie)

Reusa swap.py (AdsPower), navegacao.py (navegacao) e ad_detector.py (anuncios).

Tambem sobe o TaskView (preview AO VIVO dos perfis, DWM) num processo separado.

Multi-canal: CANAIS = lista; cada perfil fica FIXO num canal (perfil i -> CANAIS[i % M]).

Uso:  python orquestrador.py [n_perfis] [canais separados por virgula]
  ex.: python orquestrador.py 6 vitinho,gaules,loud
Pare com Ctrl+C (fecha os perfis abertos e o TaskView).

Requer:  pip install requests playwright pywin32   (ou patchright p/ anti-deteccao)
"""
import asyncio
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import ad_detector as ad
import config_store
import eventos
import navegacao
import paths
import preview
import swap

# ─────────────────────────── CONFIG ───────────────────────────
# Canais alvo. Distribuicao FIXA por perfil (balanceada): perfil i -> CANAIS[i % M].
# argv[2] sobrescreve (separados por virgula): python orquestrador.py 6 vitinho,gaules,loud
CANAIS         = ["vitinho"]
N_PERFIS       = 0           # 0 = todos os perfis (sobrescreve via argv[1])
SESSAO_MIN_S   = 60          # tempo base no canal (s)
SESSAO_MAX_S   = 240         # ............... (jitter)
# Espera apos o FIM de um anuncio antes de fechar o perfil — INTERVALO RANDOMIZADO.
GRACE_POS_AD_MIN_S = 15
GRACE_POS_AD_MAX_S = 30
# Margem de latencia somada ao fim previsto do ad (o playhead do viewer fica atras
# do live-edge). Evita marcar o ad como encerrado antes da hora.
AD_FIM_MARGEM_S    = 6
TIMEOUT_NAV    = 30000
TIMEOUT_REDE   = 20000        # timeout do check de rede do proxy (ms)
BAU            = True         # coletar o bau de pontos (community points) se aparecer
BAU_CHECK_S    = 30          # intervalo entre checagens de bau durante a sessao
# Preferencias pre-setadas no localStorage (carregam ANTES de chegar no canal):
FORCAR_QUALIDADE = True       # forca a qualidade baixa (menos banda/CPU)
QUALIDADE_ALVO   = "160p30"   # qualidade alvo do player
TEMA_ESCURO      = True       # tema dark da Twitch
# Rate limit da API AdsPower: intervalo minimo entre QUAISQUER 2 chamadas (teto 120 RPM).
# RPM ~= 60 / intervalo. 0.55s -> ~109/min (margem sob os 120). 0.5s = 120/min (no limite).
API_MIN_INTERVALO_S = 0.55    # ~109 chamadas/min (margem sob os 120 RPM)
STAGGER_START_S     = 1.5     # atraso entre a 1a abertura de cada perfil (suaviza burst/CPU)
MAX_ABRINDO         = 4       # max de perfis ABRINDO+navegando ao mesmo tempo (anti-engasgo).
                              # Libera a vaga assim que o perfil ja esta assistindo.
ABRIR_INTERVALO_S   = 0       # respiro MINIMO entre 2 aberturas consecutivas (0 = sem gate).
                              # Controla o "respiro entre batches" de forma explicita.
# Modo BATCH (lotes): abre BATCH_SIZE perfis de uma vez (navegam SIMULTANEAMENTE),
# pausa BATCH_PAUSA_S, abre o proximo lote — INDEPENDENTE de o anterior ter terminado.
# BATCH_SIZE = 0 -> desliga o modo batch (usa o modo rolling: semaforo + intervalo).
BATCH_SIZE          = 0
BATCH_PAUSA_S       = 10
PREVIEW            = True      # preview dos perfis via CDP (aba Preview da GUI)
PREVIEW_INTERVALO  = 2.0       # s entre screenshots de cada perfil
# Cadencia: pausa RANDOMIZADA apos fechar um perfil, antes de reabri-lo (menos
# perfis executando acoes ao mesmo tempo).
PAUSA_REABRIR_MIN_S = 8
PAUSA_REABRIR_MAX_S = 20
# Descarte de proxy: apos N falhas de rede, o proxy sai do rodizio (residencial
# estatico que falha N vezes esta expirado). 0 = nunca descarta (sempre re-tenta).
PROXY_MAX_FALHAS = 3
LOG_CICLOS     = True         # grava 1 linha por ciclo (horario, token, proxy) em...
ARQ_LOG_CICLOS = paths.arquivo("ciclos_log.txt")   # sempre ao lado do exe (independe do CWD)

def _agora():
    return datetime.now(timezone.utc)

# ── Log unico de ciclos (token + proxy usados, sem ID de perfil) ──────────────
_log_lock = threading.Lock()
_proxy_falhas = {}   # proxy -> nº de falhas de rede consecutivas (p/ descarte)
_run_stats = {"ciclos": 0, "anuncios": 0, "interrompidos": 0, "tokens": set(), "ips": set()}

def _registrar_proxy_morto(proxy):
    """Anexa um proxy descartado em proxies_mortos.txt (auditoria)."""
    try:
        with _log_lock:
            with open(paths.arquivo("proxies_mortos.txt"), "a", encoding="utf-8") as f:
                f.write(proxy + "\n")
    except OSError:
        pass

def log_ciclo(ts, linha):
    """Anexa uma linha ao log de ciclos: '<dd/mm HH:MM:SS> > <linha>'. ts = datetime
    local do INICIO do ciclo (momento em que token/proxy foram setados)."""
    if not LOG_CICLOS:
        return
    quando = ts.strftime("%d/%m %H:%M:%S")
    try:
        with _log_lock:
            with open(ARQ_LOG_CICLOS, "a", encoding="utf-8") as f:
                f.write(f"{quando} > {linha}\n")
    except OSError:
        pass

# ── Rate limiter global das chamadas AdsPower (teto ~100 RPM) ──────────────────
_api_lock = asyncio.Lock()
_api_ultimo = 0.0

async def _ads(fn, *args):
    """Chama uma funcao da API do AdsPower respeitando o intervalo minimo GLOBAL.
    Serializa so o 'gate' (o HTTP em si roda concorrente apos passar) -> protege
    contra o burst de abertura e contra loops de falha furarem os 120 RPM."""
    global _api_ultimo
    async with _api_lock:
        espera = API_MIN_INTERVALO_S - (time.monotonic() - _api_ultimo)
        if espera > 0:
            await asyncio.sleep(espera)
        _api_ultimo = time.monotonic()
    return await asyncio.to_thread(fn, *args)

# ── Gate de ABERTURA: respiro minimo entre 2 aberturas consecutivas ───────────
_abrir_lock = asyncio.Lock()
_abrir_ultimo = 0.0

async def _gate_abertura():
    """Garante ABRIR_INTERVALO_S entre o inicio de 2 aberturas (respiro entre aberturas)."""
    global _abrir_ultimo
    if ABRIR_INTERVALO_S <= 0:
        return
    async with _abrir_lock:
        espera = ABRIR_INTERVALO_S - (time.monotonic() - _abrir_ultimo)
        if espera > 0:
            await asyncio.sleep(espera)
        _abrir_ultimo = time.monotonic()

# ── Gate de BATCH: admite BATCH_SIZE de uma vez, pausa BATCH_PAUSA_S, repete ───
_batch_lock = asyncio.Lock()
_batch_n = 0

async def _gate_batch():
    """Libera os perfis em LOTES: a cada BATCH_SIZE liberados, dorme BATCH_PAUSA_S
    antes de liberar o proximo lote. Os do mesmo lote passam juntos -> navegam
    simultaneamente. NAO espera o lote anterior terminar de navegar."""
    global _batch_n
    async with _batch_lock:
        if _batch_n > 0 and BATCH_SIZE > 0 and _batch_n % BATCH_SIZE == 0:
            await asyncio.sleep(BATCH_PAUSA_S)   # respiro entre lotes
        _batch_n += 1

# ─────────────────────────── AdsPower (async wrappers) ───────────────────────────
async def abrir_perfil(uid, conta, proxy):
    """delcache -> update(cookie+proxy+fp) -> start. Retorna debug_port ou None.
    O perfil ja vem fechado do ciclo anterior (finally). Se delcache falhar (perfil
    aberto — ex.: sobra de execucao anterior), fecha e tenta 1x. Sem o stop fixo por
    ciclo -> economiza 1 chamada/ciclo (~20% menos RPM)."""
    d = await _ads(swap.delcache, uid)
    if d.get("code") != 0:
        await _ads(swap.stop, uid)          # delcache exige perfil fechado
        await asyncio.sleep(1)
        d = await _ads(swap.delcache, uid)
        if d.get("code") != 0:
            print(f"  [{uid}] delcache FALHOU: {d.get('msg')}", flush=True); return None
    d = await _ads(swap.update, uid, conta["auth_token"], proxy)
    if d.get("code") != 0:
        print(f"  [{uid}] update FALHOU: {d.get('msg')}", flush=True); return None
    d = await _ads(swap.start, uid)
    if d.get("code") != 0:
        print(f"  [{uid}] start FALHOU: {d.get('msg')}", flush=True); return None
    port = (d.get("data") or {}).get("debug_port")
    try:
        return int(port) if port else None
    except (TypeError, ValueError):
        return None

# ─────────────────────────── Sessao no canal (CDP) ───────────────────────────
async def sessao_no_canal(pw, debug_port, canal, rotulo, slot_n=0, libera_cb=None):
    """Conecta via CDP, navega pro canal, fica a sessao (estende se cair anuncio)."""
    endpoint = f"http://127.0.0.1:{debug_port}"
    browser = await pw.chromium.connect_over_cdp(endpoint)
    t_sess = _agora()
    resumo = {"chegou": False, "teve_ad": False, "ad_info": "", "dur_s": 0}
    try:
        page = await navegacao._pegar_page(browser)
        if PREVIEW:
            preview.registrar(slot_n, page, canal)

        # estado de anuncio do slot, atualizado pelos eventos do detector
        slot = {"em_ad": False, "teve_ad": False, "fechar_apos": None, "ad_info": "",
                "ad_fim_max": None}

        def on_ad(ev):
            ad.evento_padrao(ev)  # loga humano + jsonl
            if ev["tipo"] == "AD_START":
                slot["em_ad"] = True
                slot["teve_ad"] = True
                slot["ad_info"] = f"{ev['roll_type']} {ev['duracao_total_s']:.0f}s"
                # teto do ad: se passar MUITO disso e o AD_END nao veio (manifests pararam,
                # ex.: proxy caiu no meio do ad), o loop destrava o em_ad.
                try:
                    slot["ad_fim_max"] = datetime.fromisoformat(ev["fim_previsto"]) + timedelta(seconds=60)
                except Exception:
                    slot["ad_fim_max"] = _agora() + timedelta(seconds=int(ev["duracao_total_s"]) + 60)
                eventos.emit("ad_on", n=slot_n, canal=canal,
                             dur=int(ev["duracao_total_s"]), roll=ev["roll_type"])
            else:  # AD_END -> fecha o ciclo apos espera RANDOMIZADA do fim do anuncio,
                   # IGNORANDO o tempo base (override, pode fechar antes do minimo).
                slot["em_ad"] = False
                slot["ad_fim_max"] = None
                grace = random.uniform(GRACE_POS_AD_MIN_S, GRACE_POS_AD_MAX_S)
                slot["fechar_apos"] = _agora() + timedelta(seconds=grace)
                print(f"[{rotulo}] anuncio acabou — fechando em {grace:.0f}s", flush=True)
                eventos.emit("ad_off", n=slot_n, canal=canal, grace=int(grace))

        state = ad.AdState(rotulo, on_ad, margem_fim_s=AD_FIM_MARGEM_S)
        ad.anexar_detector(page.context, state)

        # pre-seta tema dark + qualidade 160p ANTES de navegar (carrega ja aplicado)
        if TEMA_ESCURO or FORCAR_QUALIDADE:
            await navegacao.aplicar_preferencias(
                page, qualidade=(QUALIDADE_ALVO if FORCAR_QUALIDADE else ""),
                dark=TEMA_ESCURO, rotulo=rotulo)

        # 0) checar rede do proxy (residenciais caem muito): se a home nao carrega,
        #    o proxy esta sem rede -> aborta a sessao p/ fechar e trocar de proxy.
        if not await navegacao.tem_rede(page, timeout=TIMEOUT_REDE):
            raise navegacao.ProxySemRede()

        # 1) navegar pro canal alvo (ja estamos na home -> sem reload de home)
        resumo["chegou"] = await navegacao.ir_para_canal(page, canal, rotulo,
                                                         timeout=TIMEOUT_NAV,
                                                         comecar_da_home=False)
        eventos.emit("navegou", n=slot_n, canal=canal, ok=bool(resumo["chegou"]))
        if libera_cb:
            libera_cb()   # ja navegou -> libera a vaga de abertura (watch nao segura)

        # 2) sessao base + extensao por anuncio
        dur = random.randint(SESSAO_MIN_S, SESSAO_MAX_S)
        slot["fechar_apos"] = _agora() + timedelta(seconds=dur)
        print(f"[{rotulo}] no canal /{canal} por ~{dur//60}min "
              f"(se cair anuncio: fecha {GRACE_POS_AD_MIN_S}-{GRACE_POS_AD_MAX_S}s apos o "
              f"fim do ad, ignorando o tempo base)", flush=True)

        prox_bau = _agora()   # checa bau ja na entrada e a cada BAU_CHECK_S
        prox_banner = _agora() + timedelta(seconds=12)  # re-checa banners que aparecem depois
        # TETO DURO: fecha SEMPRE depois disso, aconteca o que acontecer (perfil travado,
        # em_ad preso, overlay grudado). Garante que nenhum perfil fica zumbi.
        limite_duro = t_sess + timedelta(seconds=SESSAO_MAX_S + 240)
        while True:
            await asyncio.sleep(1)
            if _agora() > limite_duro:
                print(f"[{rotulo}] limite duro de sessao — fechando (perfil pode ter travado)",
                      flush=True)
                break
            if _agora() >= prox_banner:
                try:
                    await asyncio.wait_for(navegacao.fechar_banners(page, rotulo), timeout=15)
                except Exception:
                    pass
                prox_banner = _agora() + timedelta(seconds=12)
            if BAU and _agora() >= prox_bau:
                try:
                    if await asyncio.wait_for(navegacao.resgatar_bau(page, rotulo), timeout=15):
                        eventos.emit("bau", n=slot_n, canal=canal)
                except Exception:
                    pass
                prox_bau = _agora() + timedelta(seconds=BAU_CHECK_S)
            if slot["em_ad"]:
                # se passou MUITO do fim previsto do ad e o AD_END nao veio, os manifests
                # pararam (ex.: proxy caiu no meio do ad) -> destrava p/ poder fechar.
                afm = slot.get("ad_fim_max")
                if afm and _agora() > afm:
                    slot["em_ad"] = False
                else:
                    continue                  # nunca fecha no meio do anuncio (manifest)
            if _agora() >= slot["fechar_apos"]:
                # confirmacao final pelo DOM: se o overlay de ad ainda esta na tela
                # (viewer atrasado pela latencia), NAO fecha — re-checa em 3s.
                try:
                    overlay = (slot["teve_ad"] and
                               await asyncio.wait_for(navegacao.ad_na_tela(page), timeout=8))
                except Exception:
                    overlay = False
                if overlay:
                    slot["fechar_apos"] = _agora() + timedelta(seconds=3)
                    continue
                break

        extra = " (teve anuncio)" if slot["teve_ad"] else ""
        print(f"[{rotulo}] sessao fim{extra} — fechando perfil", flush=True)
        resumo["teve_ad"] = slot["teve_ad"]
        resumo["ad_info"] = slot["ad_info"]
        resumo["dur_s"] = int((_agora() - t_sess).total_seconds())
        return resumo
    finally:
        if PREVIEW:
            preview.desregistrar(slot_n)
        try:
            await browser.close()
        except Exception:
            pass

# ─────────────────────────── Loop de cada slot/perfil ───────────────────────────
async def slot_loop(uid, contas_q, proxies_q, pw_holder, canais, canal_carga, stop_event,
                    inicio_delay=0.0, slot_n=0, abrir_sem=None):
    if inicio_delay:
        await asyncio.sleep(inicio_delay)   # escalona o start (suaviza burst de API/CPU)
    while not stop_event.is_set():
        conta = await contas_q.get()
        proxy = await proxies_q.get()
        canal = _escolher_canal(canais, canal_carga)   # canal DESTE ciclo (balanceado+aleatorio)
        rotulo = f"{uid}/{conta['id']}"
        t0 = datetime.now()   # horario local do inicio do ciclo (token/proxy setados)
        base = f"TOKEN SETADO: {conta['auth_token']} > PROXY SETADO: {proxy}"
        _run_stats["tokens"].add(conta["auth_token"])
        _run_stats["ips"].add(proxy.split(":")[0])

        # ── gate de ABERTURA ──
        # Modo BATCH (BATCH_SIZE>0): libera em lotes (navegam juntos), pausa entre lotes,
        #   sem semaforo (lotes ja limitam a concorrencia).
        # Modo ROLLING: semaforo (teto de concorrentes) + intervalo minimo entre aberturas.
        if BATCH_SIZE > 0:
            await _gate_batch()
            sem = None
        else:
            sem = abrir_sem
            if sem is not None:
                await sem.acquire()
            await _gate_abertura()        # respiro minimo entre aberturas consecutivas
        _liberou = {"v": False}
        def _libera():
            if sem is not None and not _liberou["v"]:
                _liberou["v"] = True
                sem.release()

        descartar_proxy = False
        try:
            port = await abrir_perfil(uid, conta, proxy)
            if not port:
                print(f"[{rotulo}] falha ao abrir — proxima conta", flush=True)
                log_ciclo(t0, f"{base} > FALHOU ABRIR PERFIL")
                eventos.emit("falha_abrir", n=slot_n, canal=canal)
                _libera()
                await asyncio.sleep(5)
                continue
            print(f"[{rotulo}] aberto (porta {port}) proxy={proxy.split(':')[0]}", flush=True)
            eventos.emit("aberto", n=slot_n, canal=canal)
            resumo = await sessao_no_canal(pw_holder["pw"], port, canal, rotulo,
                                           slot_n=slot_n, libera_cb=_libera)
            nav = "navegou" if (resumo and resumo["chegou"]) else "NAO chegou"
            ad_txt = f"AD: {resumo['ad_info']}" if (resumo and resumo["teve_ad"]) else "sem anuncio"
            dur = resumo["dur_s"] if resumo else 0
            log_ciclo(t0, f"{base} > CANAL: {canal} > {nav} > {ad_txt} > DUROU: {dur}s")
            eventos.emit("fim", n=slot_n, canal=canal,
                         teve_ad=bool(resumo and resumo["teve_ad"]), dur=dur)
            _proxy_falhas.pop(proxy, None)   # funcionou -> zera strikes desse proxy
            _run_stats["ciclos"] += 1
            if resumo and resumo["teve_ad"]:
                _run_stats["anuncios"] += 1
        except asyncio.CancelledError:
            # Parar no MEIO da sessao: registra o ciclo PARCIAL antes de encerrar (nada se perde).
            dur = int((datetime.now() - t0).total_seconds())
            _run_stats["interrompidos"] += 1
            log_ciclo(t0, f"{base} > CANAL: {canal} > INTERROMPIDO (parar) > DUROU: {dur}s")
            raise
        except navegacao.ProxySemRede:
            # proxy sem rede: conta um strike. Se atingiu PROXY_MAX_FALHAS, DESCARTA
            # (nao devolve a fila) — residencial estatico que falha N vezes esta morto.
            n_fal = _proxy_falhas.get(proxy, 0) + 1
            _proxy_falhas[proxy] = n_fal
            if PROXY_MAX_FALHAS > 0 and n_fal >= PROXY_MAX_FALHAS:
                descartar_proxy = True
                _registrar_proxy_morto(proxy)
                print(f"[{rotulo}] PROXY {proxy.split(':')[0]} sem rede {n_fal}x — "
                      f"DESCARTADO (saiu do rodizio)", flush=True)
                log_ciclo(t0, f"{base} > PROXY DESCARTADO ({n_fal} falhas)")
                eventos.emit("proxy_descartado", n=slot_n, canal=canal)
            else:
                print(f"[{rotulo}] PROXY {proxy.split(':')[0]} sem rede "
                      f"({n_fal}/{PROXY_MAX_FALHAS}) — troca", flush=True)
                log_ciclo(t0, f"{base} > PROXY SEM REDE ({n_fal}/{PROXY_MAX_FALHAS})")
                eventos.emit("proxy_morto", n=slot_n, canal=canal)
        except Exception as e:
            msg = str(e)
            print(f"[{rotulo}] erro: {msg[:140]}", flush=True)
            log_ciclo(t0, f"{base} > ERRO: {msg[:80]}")
            eventos.emit("erro", n=slot_n, canal=canal, msg=msg[:80])
            if _erro_driver(msg):
                pw_holder["morto"].set()      # driver do Playwright caiu -> aciona supervisor
        finally:
            _libera()                     # idempotente (solta a vaga se ainda nao soltou)
            try:
                await _ads(swap.stop, uid)
            except Exception:
                pass
            await contas_q.put(conta)     # devolve a conta pra pool (rotaciona)
            if not descartar_proxy:
                await proxies_q.put(proxy) # devolve o proxy; se descartado, SAI do rodizio
            canal_carga[canal] -= 1       # libera a vaga do canal (rebalanceia o proximo ciclo)

        # se o driver caiu, respira ate o supervisor reerguer (evita spin de erro)
        while pw_holder["morto"].is_set() and not stop_event.is_set():
            await asyncio.sleep(random.uniform(3, 6))
        # CADENCIA: pausa randomizada apos fechar, antes de reabrir (menos perfis juntos)
        if not stop_event.is_set() and PAUSA_REABRIR_MAX_S > 0:
            await asyncio.sleep(random.uniform(PAUSA_REABRIR_MIN_S, PAUSA_REABRIR_MAX_S))

# ─────────────────────────── Modos de abertura (presets) ─────────────────────
# Cada modo define a cadencia/velocidade da abertura+navegacao. O TURBO bate na
# porta do limite da API (gate ~0.55s = ~109 RPM, sem cap) com muitos navegando
# juntos; o Conservador abre devagar, sem risco de cap.
PRESETS_ABERTURA = {
    "turbo":       dict(max_abrindo=10, abrir_intervalo=0.8, stagger=0.5, api=0.55,
                        batch=0, batch_pausa=0),
    "moderado":    dict(max_abrindo=6,  abrir_intervalo=1.5, stagger=1.0, api=0.7,
                        batch=0, batch_pausa=0),
    "conservador": dict(max_abrindo=3,  abrir_intervalo=4.0, stagger=2.5, api=1.1,
                        batch=0, batch_pausa=0),
}

def _aplicar_modo_abertura(modo):
    """Aplica o preset de abertura (turbo/moderado/conservador) nos globais de cadencia."""
    global MAX_ABRINDO, ABRIR_INTERVALO_S, STAGGER_START_S, API_MIN_INTERVALO_S
    global BATCH_SIZE, BATCH_PAUSA_S
    p = PRESETS_ABERTURA.get(str(modo).lower(), PRESETS_ABERTURA["moderado"])
    MAX_ABRINDO = p["max_abrindo"]
    ABRIR_INTERVALO_S = p["abrir_intervalo"]
    STAGGER_START_S = p["stagger"]
    API_MIN_INTERVALO_S = p["api"]
    BATCH_SIZE = p["batch"]
    BATCH_PAUSA_S = p["batch_pausa"]

# ─────────────────────────── Config (settings.json -> globais) ───────────────
def _aplicar_config_run():
    """Le a secao 'run' do settings.json e sobrescreve os globais do modulo.
    Retorna (canais, n_perfis). Defaults = as constantes do topo."""
    global SESSAO_MIN_S, SESSAO_MAX_S, GRACE_POS_AD_MIN_S, GRACE_POS_AD_MAX_S
    global AD_FIM_MARGEM_S, API_MIN_INTERVALO_S, STAGGER_START_S, BAU, BAU_CHECK_S
    global PREVIEW, PREVIEW_INTERVALO, PAUSA_REABRIR_MIN_S, PAUSA_REABRIR_MAX_S
    global TIMEOUT_NAV, TIMEOUT_REDE, PROXY_MAX_FALHAS
    global FORCAR_QUALIDADE, QUALIDADE_ALVO, TEMA_ESCURO, MAX_ABRINDO, ABRIR_INTERVALO_S
    global BATCH_SIZE, BATCH_PAUSA_S
    swap.aplicar_config_adspower()
    g = lambda k, d: config_store.get("run", k, d)
    canais = g("canais", CANAIS) or CANAIS
    canais = [str(c).strip().lstrip("/").lower() for c in canais if str(c).strip()]
    n = int(g("n_perfis", N_PERFIS) or 0)
    SESSAO_MIN_S = int(g("sessao_min_s", SESSAO_MIN_S))
    SESSAO_MAX_S = int(g("sessao_max_s", SESSAO_MAX_S))
    GRACE_POS_AD_MIN_S = float(g("grace_min_s", GRACE_POS_AD_MIN_S))
    GRACE_POS_AD_MAX_S = float(g("grace_max_s", GRACE_POS_AD_MAX_S))
    AD_FIM_MARGEM_S = float(g("ad_margem_s", AD_FIM_MARGEM_S))
    _aplicar_modo_abertura(g("modo_abertura", "moderado"))   # presets de cadencia/velocidade
    BAU = bool(g("bau", BAU))
    BAU_CHECK_S = int(g("bau_check_s", BAU_CHECK_S))
    PREVIEW = bool(g("preview", PREVIEW))
    PREVIEW_INTERVALO = float(g("preview_intervalo", PREVIEW_INTERVALO))
    PAUSA_REABRIR_MIN_S = float(g("pausa_reabrir_min_s", PAUSA_REABRIR_MIN_S))
    PAUSA_REABRIR_MAX_S = float(g("pausa_reabrir_max_s", PAUSA_REABRIR_MAX_S))
    PROXY_MAX_FALHAS = int(g("proxy_max_falhas", PROXY_MAX_FALHAS))
    TIMEOUT_NAV = int(g("timeout_nav_ms", TIMEOUT_NAV))
    TIMEOUT_REDE = int(g("timeout_rede_ms", TIMEOUT_REDE))
    FORCAR_QUALIDADE = bool(g("forcar_qualidade", FORCAR_QUALIDADE))
    QUALIDADE_ALVO = g("qualidade_alvo", QUALIDADE_ALVO)
    TEMA_ESCURO = bool(g("tema_escuro", TEMA_ESCURO))
    return canais, n

async def _watch_parar(stop_event):
    """Converte o pedido de parada externo (eventos.parar, setado pela GUI) no
    asyncio stop_event deste loop."""
    while not stop_event.is_set():
        if eventos.parar.is_set():
            stop_event.set()
            return
        await asyncio.sleep(0.4)


def _escolher_canal(canais, carga):
    """Distribuicao DINAMICA + balanceada + aleatoria: escolhe o canal MENOS carregado
    agora (desempate por sorteio) e reserva uma vaga. Garante divisao ~igual entre canais
    (diferenca max. 1), com qual-perfil-vai-onde aleatorizado. Liberar com carga[canal]-=1."""
    menor = min(carga[c] for c in canais)
    c = random.choice([c for c in canais if carga[c] == menor])
    carga[c] += 1
    return c


def _erro_driver(msg):
    """True se o erro indica que o DRIVER do Playwright (node.exe) caiu — nesse caso o
    pw inteiro morre e TODOS os slots falham; precisa reiniciar o Playwright."""
    m = (msg or "").lower()
    return ("reading from the driver" in m
            or "driver closed" in m
            or ("connection closed" in m and "driver" in m))


async def _supervisor_driver(pw_holder, async_pw, stop_event):
    """Auto-recuperacao: se o driver do Playwright cair (um slot seta pw_holder['morto']),
    reinicia o Playwright e atualiza pw_holder['pw']. Os slots leem pw_holder['pw'] a cada
    ciclo, entao passam a usar o driver novo automaticamente."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(pw_holder["morto"].wait(), timeout=5)
        except asyncio.TimeoutError:
            continue                       # re-checa stop_event periodicamente
        if stop_event.is_set():
            break
        await asyncio.sleep(2)             # debounce: junta varios slots sinalizando juntos
        print("[driver] Playwright caiu — reiniciando...", flush=True)
        eventos.emit("erro", n="-", canal="", msg="driver CDP caiu — reiniciando")
        try:
            await pw_holder["pw"].stop()
        except Exception:
            pass
        novo = None
        for tent in range(6):
            if stop_event.is_set():
                break
            try:
                novo = await async_pw().start()
                break
            except Exception as e:
                print(f"[driver] falha ao reiniciar ({tent+1}/6): {str(e)[:80]}", flush=True)
                await asyncio.sleep(5)
        if novo is not None:
            pw_holder["pw"] = novo
            print("[driver] Playwright reiniciado com sucesso.", flush=True)
            eventos.emit("erro", n="-", canal="", msg="driver CDP reiniciado — retomando")
        pw_holder["morto"].clear()
        await asyncio.sleep(10)            # cooldown: evita reinicio em cascata

# ─────────────────────────── Main ───────────────────────────
async def amain():
    canais, n = _aplicar_config_run()
    # Locks asyncio sao por-loop. A GUI roda cada RUN num asyncio.run() (loop novo),
    # entao recriamos os locks/contadores AQUI, ligados ao loop ATUAL -> evita o erro
    # "Lock bound to a different event loop" ao iniciar a 2a RUN.
    global _api_lock, _abrir_lock, _batch_lock, _api_ultimo, _abrir_ultimo, _batch_n
    global _proxy_falhas, _run_stats
    _api_lock = asyncio.Lock()
    _abrir_lock = asyncio.Lock()
    _batch_lock = asyncio.Lock()
    _api_ultimo = 0.0
    _abrir_ultimo = 0.0
    _batch_n = 0
    _proxy_falhas = {}   # zera os strikes de proxy a cada RUN
    _run_stats = {"ciclos": 0, "anuncios": 0, "interrompidos": 0, "tokens": set(), "ips": set()}
    # argv ainda sobrescreve (modo CLI): [n_perfis] [canais,virgula]
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        n = int(sys.argv[1])
    if len(sys.argv) > 2:
        canais = [c.strip().lstrip("/").lower() for c in sys.argv[2].split(",") if c.strip()]
    if not canais:
        print("Nenhum canal configurado."); eventos.emit("run_fim", motivo="sem canal"); return

    contas, proxies, perfis = swap.carregar()
    if n and n > 0:
        perfis = perfis[:n]
    if not perfis:
        print("Nenhum perfil detectado (AdsPower API)."); eventos.emit("run_fim", motivo="sem perfil"); return
    if not contas:
        print("Nenhuma conta (tokens.txt vazio)."); eventos.emit("run_fim", motivo="sem conta"); return
    if len(proxies) < len(perfis):
        print(f"AVISO: menos proxies ({len(proxies)}) que perfis ({len(perfis)}) — "
              f"slots vao esperar proxy livre.")

    # Distribuicao DINAMICA + balanceada + aleatoria: a cada ciclo o perfil pega o canal
    # MENOS carregado (desempate por sorteio) -> trocam de canal ao longo da run, mas a
    # divisao entre canais fica sempre ~igual (nunca 80/20). Ver _escolher_canal/slot_loop.
    numeros = {u: i + 1 for i, u in enumerate(perfis)}   # nº amigavel do slot (1..N)
    canal_carga = Counter()                              # perfis ATIVOS por canal agora
    alvo = Counter(canais[i % len(canais)] for i in range(len(perfis)))   # so p/ exibir o alvo
    print(f"Canais (dinamico ~balanceado): {', '.join(f'{c}(~{alvo[c]})' for c in canais)} | "
          f"Perfis(slots): {len(perfis)} | Contas: {len(contas)} | Proxies: {len(proxies)}")
    eventos.emit("run_inicio", perfis=len(perfis), canais=dict(alvo))

    contas_q, proxies_q = asyncio.Queue(), asyncio.Queue()
    for c in contas:  contas_q.put_nowait(c)
    for p in proxies: proxies_q.put_nowait(p)

    stop_event = asyncio.Event()
    async_pw, engine = navegacao._get_async_playwright()

    abrir_sem = asyncio.Semaphore(MAX_ABRINDO)   # limita aberturas+navegacoes simultaneas

    # pw gerenciado manualmente (start/stop) p/ permitir REINICIO automatico se o driver
    # do Playwright cair (senao todos os slots ficam em erro pra sempre). Ver _supervisor_driver.
    pw_holder = {"pw": await async_pw().start(), "morto": asyncio.Event()}
    try:
        print(f"engine CDP: {engine}. Rodando. Ctrl+C para parar.", flush=True)
        watcher = asyncio.create_task(_watch_parar(stop_event))
        supervisor = asyncio.create_task(_supervisor_driver(pw_holder, async_pw, stop_event))
        prev_task = (asyncio.create_task(preview.capturador(PREVIEW_INTERVALO))
                     if PREVIEW else None)
        # no modo batch o gate de lote controla a cadencia -> sem stagger por slot
        tasks = [asyncio.create_task(
                     slot_loop(u, contas_q, proxies_q, pw_holder, canais, canal_carga, stop_event,
                               inicio_delay=(0 if BATCH_SIZE > 0 else i * STAGGER_START_S),
                               slot_n=numeros[u], abrir_sem=abrir_sem))
                 for i, u in enumerate(perfis)]
        try:
            await stop_event.wait()            # bloqueia ate parada (GUI) ou Ctrl+C (CLI)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop_event.set()
            watcher.cancel()
            supervisor.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if prev_task:
                prev_task.cancel()
            preview.limpar()
            print("\nFechando perfis abertos...", flush=True)
            for u in perfis:
                try:
                    await _ads(swap.stop, u)
                except Exception:
                    pass
            s = _run_stats
            resumo_run = ("=== RUN encerrada: "
                          f"{s['ciclos']} ciclos"
                          + (f" (+{s['interrompidos']} interrompidos no parar)" if s['interrompidos'] else "")
                          + f" · {s['anuncios']} anuncios"
                          f" · {len(s['tokens'])} tokens · {len(s['ips'])} IPs ===")
            log_ciclo(datetime.now(), resumo_run)
            print(resumo_run, flush=True)
            eventos.emit("resumo", txt=resumo_run)
            eventos.emit("run_fim", motivo="parado")
    finally:
        try:
            await pw_holder["pw"].stop()    # encerra o driver do Playwright
        except Exception:
            pass

def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nInterrompido.")

if __name__ == "__main__":
    main()
