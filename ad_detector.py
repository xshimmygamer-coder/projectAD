"""
Identificador de ADS (Twitch) via CDP.

Twitch usa SSAI (Server-Side Ad Insertion): o anuncio e "costurado" dentro do
proprio video. NAO ha requisicao de anuncio separada — a fonte da verdade e o
manifest HLS (.m3u8) baixado de *.playlist.ttvnw.net. Dentro dele, cada anuncio
e uma tag:

  #EXT-X-DATERANGE:ID="...",CLASS="twitch-stitched-ad",START-DATE="...Z",
    DURATION=30.265,X-TV-TWITCH-AD-ROLL-TYPE="MIDROLL",
    X-TV-TWITCH-AD-POD-FILLED-DURATION="30",X-TV-TWITCH-AD-COMMERCIAL-ID="..."

Este modulo conecta no perfil via CDP (connect_over_cdp), escuta as respostas de
rede, filtra os manifests (.m3u8 de ttvnw.net), parseia as tags acima e emite:
  - AD_START : inicio, tipo (MIDROLL/PREROLL), duracao total, fim previsto, pod
  - AD_END   : fim (quando o anuncio sai da tela) + duracao real

Teste isolado (perfil JA aberto, com a porta CDP do AdsPower):
  python ad_detector.py <debug_port> [rotulo]
  ex.: python ad_detector.py 20323 "k1bdjmcm/vitinho"

Requer:  pip install playwright   (NAO precisa baixar browser; conectamos a um existente)
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone

# ─────────────────────────── Parser do manifest ───────────────────────────
_AD_CLASS = 'CLASS="twitch-stitched-ad"'

def _attr_str(line, key):
    m = re.search(rf'{re.escape(key)}="([^"]*)"', line)
    return m.group(1) if m else None

def _attr_num(line, key):
    # negative lookbehind p/ nao casar PLANNED-DURATION quando key=DURATION etc.
    m = re.search(rf'(?<![-A-Z]){re.escape(key)}=([0-9.]+)', line)
    return float(m.group(1)) if m else None

def parse_ads(manifest_text):
    """Extrai os anuncios costurados de um manifest .m3u8. Retorna lista de dicts
    (1 por tag DATERANGE de anuncio). Vazia se nao houver anuncio."""
    ads = []
    for line in manifest_text.splitlines():
        if not line.startswith("#EXT-X-DATERANGE"):
            continue
        if _AD_CLASS not in line:
            continue
        ads.append({
            "id":            _attr_str(line, "ID"),
            "start":         _attr_str(line, "START-DATE"),
            "duration":      _attr_num(line, "DURATION"),
            "roll_type":     _attr_str(line, "X-TV-TWITCH-AD-ROLL-TYPE"),
            "pod_duration":  _attr_str(line, "X-TV-TWITCH-AD-POD-FILLED-DURATION"),
            "commercial_id": _attr_str(line, "X-TV-TWITCH-AD-COMMERCIAL-ID"),
        })
    return ads

def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None

# ─────────────────────────── Maquina de estado ───────────────────────────
class AdState:
    """Acompanha o estado de anuncio de UM perfil. `feed(texto_manifest)` a cada
    manifest recebido; chama on_event(dict) em AD_START / AD_END."""
    def __init__(self, rotulo, on_event, margem_fim_s=0):
        self.rotulo = rotulo
        self.on_event = on_event
        self.margem_fim_s = margem_fim_s   # margem extra no fim (cobre a latencia do HLS)
        self.atual = None        # bloco comercial ativo, ou None
        self.encerrados = set()  # commercial_ids ja encerrados (ignora enquanto persistirem)

    def feed(self, manifest_text):
        agora = datetime.now(timezone.utc)
        ads = parse_ads(manifest_text)
        present = {a["commercial_id"] for a in ads if a["commercial_id"]}

        # libera da lista de 'encerrados' os cids que ja sumiram do manifest
        self.encerrados &= present

        # ── inicio de um novo bloco comercial (cid novo, ainda nao encerrado) ──
        if self.atual is None:
            novos = [a for a in ads if a["commercial_id"] not in self.encerrados]
            if novos:
                cid = novos[0]["commercial_id"]
                grupo = [a for a in ads if a["commercial_id"] == cid]
                starts = [d for d in (_parse_dt(a["start"]) for a in grupo) if d]
                inicio = min(starts) if starts else agora
                pod = grupo[0]["pod_duration"]
                dur_total = float(pod) if pod else sum((a["duration"] or 0) for a in grupo)
                fim_prev = inicio + timedelta(seconds=dur_total + self.margem_fim_s)
                self.atual = {
                    "commercial_id": cid,
                    "roll_type": grupo[0]["roll_type"],
                    "inicio": inicio,
                    "duracao_total_s": round(dur_total, 1),
                    "fim_previsto": fim_prev,
                    "n_anuncios": len(grupo),
                }
                self.on_event({
                    "tipo": "AD_START",
                    "perfil": self.rotulo,
                    "roll_type": self.atual["roll_type"],
                    "n_anuncios": len(grupo),
                    "inicio": inicio.isoformat(),
                    "duracao_total_s": self.atual["duracao_total_s"],
                    "fim_previsto": fim_prev.isoformat(),
                    "commercial_id": cid,
                    "anuncios": [{"inicio": a["start"], "duracao_s": a["duration"],
                                  "tipo": a["roll_type"]} for a in grupo],
                })

        # ── fim do bloco ──
        # SO encerra quando o tempo previsto (com margem de latencia) passou.
        # IMPORTANTE: 'sumiu do manifest' NAO encerra sozinho — o daterange sai do
        # manifest no live-edge ANTES do playhead do viewer (atrasado pela latencia)
        # chegar ao fim do anuncio; usar isso encerrava cedo (fechava no meio do ad).
        # O cid encerrado vai pra 'encerrados' p/ NAO re-disparar enquanto persistir.
        if self.atual:
            cid = self.atual["commercial_id"]
            presente = cid in present
            if agora >= self.atual["fim_previsto"]:
                real = (agora - self.atual["inicio"]).total_seconds()
                self.on_event({
                    "tipo": "AD_END",
                    "perfil": self.rotulo,
                    "commercial_id": cid,
                    "fim": agora.isoformat(),
                    "duracao_real_s": round(real, 1),
                    "motivo": "fim_previsto",
                })
                if presente and cid:
                    self.encerrados.add(cid)
                self.atual = None

# ─────────────────────────── Saida padrao (log) ───────────────────────────
LOG_JSONL = "ads_log.jsonl"

def evento_padrao(ev):
    # linha humana
    if ev["tipo"] == "AD_START":
        print(f"[{ev['perfil']}] AD ON  · {ev['roll_type']} · {ev['duracao_total_s']:.0f}s "
              f"· {ev['n_anuncios']} anuncio(s) · fim ~{ev['fim_previsto']}", flush=True)
    else:
        print(f"[{ev['perfil']}] AD OFF · durou {ev['duracao_real_s']:.0f}s "
              f"({ev['motivo']})", flush=True)
    # jsonl p/ auditoria
    try:
        with open(LOG_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass

# ─────────────────────────── CDP / Playwright ───────────────────────────
def _eh_manifest(url):
    return ".m3u8" in url and "ttvnw.net" in url

def anexar_detector(contexto, state):
    """Liga o detector a um BrowserContext (ou Page): escuta respostas, filtra os
    manifests (.m3u8 ttvnw) e alimenta o AdState. Reusavel pelo orquestrador."""
    async def handle(resp):
        try:
            if not _eh_manifest(resp.url):
                return
            txt = await resp.text()
        except Exception:
            return
        if "EXT-X-DATERANGE" in txt or "twitch-stitched-ad" in txt:
            state.feed(txt)
    contexto.on("response", lambda resp: asyncio.create_task(handle(resp)))

async def monitorar(cdp_endpoint, rotulo, on_event=evento_padrao):
    """Conecta no perfil via CDP e monitora anuncios ate ser interrompido."""
    from playwright.async_api import async_playwright
    state = AdState(rotulo, on_event)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_endpoint)
        print(f"[{rotulo}] conectado via CDP em {cdp_endpoint}", flush=True)

        for ctx in browser.contexts:
            anexar_detector(ctx, state)

        print(f"[{rotulo}] monitorando manifests (.m3u8 ttvnw). Ctrl+C para sair.", flush=True)
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

def _cdp_url(arg):
    # aceita "20323" (porta), "127.0.0.1:20323" ou url http completa
    if arg.startswith("http"):
        return arg
    if ":" in arg:
        return f"http://{arg}"
    return f"http://127.0.0.1:{arg}"

def main():
    if len(sys.argv) < 2:
        print("uso: python ad_detector.py <debug_port|host:port|http-url> [rotulo]")
        sys.exit(2)
    endpoint = _cdp_url(sys.argv[1])
    rotulo = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]
    asyncio.run(monitorar(endpoint, rotulo))

if __name__ == "__main__":
    main()
