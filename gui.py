"""
MURIADS — Interface grafica (Flet).
Abas: APIs (AdsPower) · Proxy · Tokens · Configs · Preview · Logs ao vivo.
Roda o orquestrador no mesmo processo (thread daemon) e mostra logs didaticos +
contador de anuncios assistidos por canal + preview ao vivo (screenshots CDP).

  pip install flet==0.28.3 requests playwright pywin32 pillow
  python gui.py
"""
import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time

# No .exe sem console (PyInstaller --windowed) o sys.stdout/stderr ficam None; o uvicorn
# (servidor web do Flet) faz stdout.isatty() e crasha. Garante objetos validos ANTES do flet.
if sys.stdout is None or sys.stderr is None:
    _nul = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = _nul
    if sys.stderr is None:
        sys.stderr = _nul

import flet as ft

import config_store
import eventos
import paths
import preview
import proxy_checker
import swap

# orquestrador importa swap/navegacao/ad_detector — pesado, mas ok no start
import orquestrador

# Arquivos de IPC entre a GUI e a ENGINE (processo separado) — ao lado do exe:
ARQ_EVENTOS = paths.arquivo("eventos_live.jsonl")    # engine ANEXA eventos; GUI faz tail
ARQ_PARAR_FLAG = paths.arquivo("parar.flag")         # GUI cria -> engine encerra
ARQ_PREVIEW_FLAG = paths.arquivo("preview_on.flag")  # GUI liga/desliga a captura na engine
PREVIEW_DIR = paths.arquivo("preview")               # engine grava slot_N.jpg; GUI le
ARQ_ENGINE_PID = paths.arquivo("engine.pid")         # engine cria/atualiza enqto vive; remove ao sair
ENGINE_HEARTBEAT_S = 3          # de quanto em quanto a engine "bate o coracao" (atualiza o arquivo)
ENGINE_TIMEOUT_S = 12           # sem batida ha > isto = engine MORTA (reboot/crash/kill)


def _engine_vivo():
    """True se a ENGINE esta de fato viva: o arquivo de heartbeat existe e foi tocado
    ha pouco. Cobre o caso de reabrir a GUI: se o motor morreu feio (reboot/crash) sem
    escrever run_fim, o heartbeat fica velho/ausente -> a GUI NAO trava o Iniciar."""
    try:
        return (time.time() - os.stat(ARQ_ENGINE_PID).st_mtime) < ENGINE_TIMEOUT_S
    except OSError:
        return False

APP_NOME = "MURIADS"

ROXO = "#9146FF"
BG = "#0e0e10"
CARD = "#18181b"
VERDE = "#3fd16b"
CINZA = "#adadb8"


# ─────────────────────────── helpers de arquivo ───────────────────────────
def _ler(nome):
    try:
        with open(paths.arquivo(nome), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _escrever(nome, texto):
    # normaliza quebras (paste vem com \r\n; sem isso o Windows duplica em \r\r\n =
    # linha em branco entre cada item). Tira linhas vazias e espacos das pontas.
    linhas = [l.strip() for l in texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    limpo = "\n".join(l for l in linhas if l)
    with open(paths.arquivo(nome), "w", encoding="utf-8", newline="\n") as f:
        f.write(limpo + ("\n" if limpo else ""))


def _conta_linhas(texto):
    return len([l for l in texto.splitlines() if l.strip() and not l.strip().startswith("#")])


# ─────────────────────────── app ───────────────────────────
def main(page: ft.Page):
    page.title = APP_NOME
    page.bgcolor = BG
    page.padding = 0
    page.theme_mode = ft.ThemeMode.DARK   # texto claro por padrao (legivel no fundo escuro)
    page.window_width = 1100
    page.window_height = 800
    # icone da janela/taskbar (Flet 0.28: page.window.icon). Path bundle-aware.
    try:
        page.window.icon = paths.asset("icone.ico")
    except Exception:
        pass

    def aviso(msg, cor=VERDE):
        page.open(ft.SnackBar(ft.Text(msg, color="#ffffff"), bgcolor=cor))

    # ── estado da RUN ──
    estado = {"rodando": False, "contador": {}, "tail_pos": 0, "proc": None}
    # UI: APENAS o thread `atualizador` chama page.update(). As demais threads (consumidor
    # de logs, preview) e os handlers só MEXEM nos controles -> zero corrida/lock/starvation.
    IDX_PREVIEW = 4   # ordem das abas: APIs(0) Proxy(1) Tokens(2) Configs(3) Preview(4) Logs(5)

    # ╔══ BANNER ══╗ (faixa full-width, altura fixa — corta topo/baixo)
    BANNER_H = 150
    banner = ft.Container(
        content=ft.Image(src="banner.png", fit=ft.ImageFit.COVER, expand=True),
        height=BANNER_H, bgcolor="#000000",
    )

    # ╔══ ABA APIs ══╗
    f_api_key = ft.TextField(label="AdsPower API key", password=True,
                             can_reveal_password=True,
                             value=config_store.get("adspower", "api_key", ""))
    f_api_base = ft.TextField(label="Base URL",
                              value=config_store.get("adspower", "base",
                                                     "http://local.adspower.net:50325"))
    # MULTI-GRUPO: a selecao e uma lista de group_ids separada por virgula (no settings).
    _grupos_salvos = set(g.strip() for g in
                         str(config_store.get("adspower", "group_id", "") or "").split(",") if g.strip())
    f_api_filtro = ft.TextField(label="Filtro de nome (opcional)",
                                value=config_store.get("adspower", "filtro_nome", ""))
    _chk_grupos = []   # lista de (group_id, Checkbox)
    grupos_box = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO, height=150)
    lbl_grupos = ft.Text(
        ("Grupos salvos: " + ", ".join(sorted(_grupos_salvos))) if _grupos_salvos
        else "Nenhum grupo selecionado (= TODOS). Clique 'Detectar grupos'.",
        color=CINZA, size=12)

    def _grupos_marcados():
        return [gid for gid, chk in _chk_grupos if chk.value]

    def _salvar_adspower():
        # ja detectou? usa o que esta marcado. Senao, preserva o que ja estava salvo.
        gid_val = (",".join(_grupos_marcados()) if _chk_grupos
                   else ",".join(sorted(_grupos_salvos)))
        config_store.salvar_secao("adspower", {
            "api_key": f_api_key.value.strip(),
            "base": f_api_base.value.strip() or "http://local.adspower.net:50325",
            "group_id": gid_val,
            "filtro_nome": f_api_filtro.value.strip(),
        })
        _grupos_salvos.clear()
        _grupos_salvos.update(g for g in gid_val.split(",") if g)

    def detectar_grupos(e):
        _salvar_adspower()                 # usa a key/base atuais
        swap.aplicar_config_adspower()
        try:
            grupos = swap.listar_grupos()
        except Exception as ex:
            aviso(f"Erro ao listar grupos: {str(ex)[:70]}", "#ff6b6b")
            return
        _chk_grupos.clear()
        grupos_box.controls.clear()
        for g in grupos:
            gid = str(g["group_id"])
            chk = ft.Checkbox(label=(g["group_name"] or gid),
                              value=(gid in _grupos_salvos),
                              label_style=ft.TextStyle(color="#ffffff"))
            _chk_grupos.append((gid, chk))
            grupos_box.controls.append(chk)
        lbl_grupos.value = (f"{len(grupos)} grupo(s) — marque 1 ou mais "
                            f"(nenhum marcado = TODOS) e clique Salvar.")
        aviso(f"{len(grupos)} grupo(s) detectado(s).")

    def salvar_apis(e):
        _salvar_adspower()
        sel = ", ".join(sorted(_grupos_salvos)) if _grupos_salvos else "TODOS"
        aviso(f"AdsPower salvo. Grupos: {sel}.")

    aba_apis = ft.Container(padding=20, content=ft.Column([
        ft.Text("AdsPower", size=18, weight=ft.FontWeight.BOLD, color="#ffffff"),
        ft.Text("API key → 'Detectar grupos' → marque 1 ou mais grupos deste server → Salvar.",
                color=CINZA, size=12),
        f_api_key, f_api_base,
        ft.Row([ft.OutlinedButton("Detectar grupos", on_click=detectar_grupos)]),
        lbl_grupos,
        ft.Container(content=grupos_box, bgcolor=CARD, border_radius=6, padding=8),
        f_api_filtro,
        ft.FilledButton("Salvar", on_click=salvar_apis,
                        style=ft.ButtonStyle(bgcolor=ROXO)),
    ], spacing=12))

    # ╔══ ABA PROXY ══╗
    f_proxies = ft.TextField(label="Proxies (1 por linha: host:port ou host:port:user:senha)",
                             multiline=True, min_lines=10, max_lines=10,
                             value=_ler("proxies_pool.txt"))

    def salvar_proxies(e):
        _escrever("proxies_pool.txt", f_proxies.value)
        aviso(f"{_conta_linhas(f_proxies.value)} proxies salvos para esta RUN.")

    # ── CHECKER de reputação (portado do MURIPRO2) ──
    def _pcget(k, d):
        return str(config_store.get("proxy_check", k, d))

    f_pc_token = ft.TextField(label="IPInfo token (obrigatório p/ reputação)", password=True,
                              can_reveal_password=True, value=_pcget("ipinfo_token", ""))
    f_pc_echo = ft.TextField(label="Echo URL (opcional; privado evita 429 em lotes grandes)",
                             value=_pcget("echo_url", "https://ipinfo.io/ip"))
    f_pc_abuse_keys = ft.TextField(label="AbuseIPDB keys (opcional, vírgula)",
                                   value=_pcget("abuseipdb_keys_str", ""))
    f_pc_threads = ft.TextField(label="Threads", value=_pcget("threads", 20), width=130)
    f_pc_to_proxy = ft.TextField(label="Timeout proxy (s)", value=_pcget("timeout_proxy", 12), width=170)
    f_pc_to_api = ft.TextField(label="Timeout API (s)", value=_pcget("timeout_api", 8), width=160)
    f_pc_abuse_max = ft.TextField(label="AbuseIPDB score máx", value=_pcget("abuseipdb_score_max", 50), width=180)
    f_pc_paises = ft.TextField(label="Países (ISO2, vírgula — vazio=todos)", value=_pcget("paises_str", ""))
    f_pc_estados = ft.TextField(label="Estados (vírgula — vazio=todos)", value=_pcget("estados_str", ""))
    f_pc_zips = ft.TextField(label="ZIPs (vírgula — vazio=todos)", value=_pcget("zipcodes_str", ""))
    sw_pc_dnsbl = ft.Switch(label="Usar DNSBL", value=bool(config_store.get("proxy_check", "dnsbl_enabled", True)))
    sw_pc_abuse = ft.Switch(label="Usar AbuseIPDB", value=bool(config_store.get("proxy_check", "abuseipdb_enabled", False)))

    for _c in (f_pc_token, f_pc_echo, f_pc_abuse_keys, f_pc_threads, f_pc_to_proxy,
               f_pc_to_api, f_pc_abuse_max, f_pc_paises, f_pc_estados, f_pc_zips):
        _c.color = "#ffffff"; _c.bgcolor = CARD; _c.border_color = "#3a3a3d"
        _c.focused_border_color = ROXO; _c.cursor_color = ROXO
        _c.label_style = ft.TextStyle(color=CINZA)
    for _s in (sw_pc_dnsbl, sw_pc_abuse):
        _s.label_style = ft.TextStyle(color="#ffffff"); _s.active_color = ROXO

    txt_pc_prog = ft.Text("", color=CINZA, size=12)
    txt_pc_res = ft.Text("Nenhuma checagem ainda.", color=VERDE, size=13, weight=ft.FontWeight.BOLD)
    # quadro pequeno de LOGS ao vivo do checker (1 linha por proxy)
    lista_pc_logs = ft.ListView(auto_scroll=True, spacing=1, padding=8)

    def _pc_int(tf, d):
        try:
            return int(float(tf.value))
        except (TypeError, ValueError):
            return d

    def _coletar_cfg_pc():
        """Lê os campos, persiste em settings.json["proxy_check"] e devolve o cfg do checker."""
        keys = [k.strip() for k in f_pc_abuse_keys.value.split(",") if k.strip()]
        paises = [x.strip() for x in f_pc_paises.value.split(",") if x.strip()]
        estados = [x.strip() for x in f_pc_estados.value.split(",") if x.strip()]
        zips = [x.strip() for x in f_pc_zips.value.split(",") if x.strip()]
        cfg = {
            "ipinfo_token": f_pc_token.value.strip(),
            "echo_url": f_pc_echo.value.strip() or "https://ipinfo.io/ip",
            "abuseipdb_keys": keys,
            "abuseipdb_enabled": bool(sw_pc_abuse.value),
            "abuseipdb_score_max": _pc_int(f_pc_abuse_max, 50),
            "dnsbl_enabled": bool(sw_pc_dnsbl.value),
            "threads": _pc_int(f_pc_threads, 20),
            "timeout_proxy": _pc_int(f_pc_to_proxy, 12),
            "timeout_api": _pc_int(f_pc_to_api, 8),
            "paises": paises, "estados": estados, "zipcodes": zips,
        }
        # persiste (guarda também as versões _str p/ repovoar os campos ao reabrir)
        config_store.salvar_secao("proxy_check", {**cfg,
            "abuseipdb_keys_str": f_pc_abuse_keys.value.strip(),
            "paises_str": f_pc_paises.value.strip(),
            "estados_str": f_pc_estados.value.strip(),
            "zipcodes_str": f_pc_zips.value.strip()})
        return cfg

    def _checar_proxies(e):
        if estado.get("pc_rodando"):
            return
        linhas = [l.strip() for l in f_proxies.value.replace("\r\n", "\n").split("\n")
                  if l.strip() and not l.strip().startswith("#")]
        if not linhas:
            aviso("Cole proxies primeiro.", "#ff6b6b"); return
        cfg = _coletar_cfg_pc()
        if not cfg["ipinfo_token"]:
            aviso("Preencha o IPInfo token (a reputação exige).", "#ff6b6b"); return
        estado["pc_rodando"] = True
        estado["proxies_aprovados"] = []
        estado["pc_parar"] = threading.Event()
        txt_pc_prog.value = f"Checando 0/{len(linhas)}…"
        txt_pc_res.value = ""
        lista_pc_logs.controls.clear()
        btn_checar.disabled = True
        btn_pc_parar.disabled = False

        def _prog(feito, total, status, proxy, info):
            txt_pc_prog.value = f"Checando {feito}/{total}…"
            tag, cor = {"BOM": ("✓", VERDE), "RUIM": ("✗", "#ff6b6b")}.get(status, ("⚠", "#ffd24a"))
            lista_pc_logs.controls.append(
                ft.Text(f"[{feito}/{total}] {tag} {proxy} — {info}", color=cor, size=12,
                        selectable=True, no_wrap=False))
            if len(lista_pc_logs.controls) > 200:
                del lista_pc_logs.controls[:len(lista_pc_logs.controls) - 150]

        def _run():
            try:
                aprov, repro = proxy_checker.checar_lista(
                    linhas, cfg, progress_cb=_prog, parar=estado["pc_parar"])
            except Exception as ex:
                txt_pc_prog.value = ""
                txt_pc_res.value = f"Erro no checker: {str(ex)[:80]}"
                estado["pc_rodando"] = False
                btn_checar.disabled = False
                btn_pc_parar.disabled = True
                return
            estado["proxies_aprovados"] = [r["proxy"] for r in aprov]
            cancelado = estado["pc_parar"].is_set()
            txt_pc_prog.value = "Parado." if cancelado else "Concluído."
            txt_pc_res.value = f"✓ {len(aprov)} aprovados / {len(repro)} reprovados"
            try:
                _escrever("proxies_reprovados.txt",
                          "\n".join(f"{r['proxy']} | {r['status']} | {r.get('info','')}" for r in repro))
            except OSError:
                pass
            estado["pc_rodando"] = False
            btn_checar.disabled = False
            btn_pc_parar.disabled = True

        threading.Thread(target=_run, daemon=True).start()

    def _setar_aprovados(e):
        aprov = estado.get("proxies_aprovados") or []
        if not aprov:
            aviso("0 aprovados — pool PRESERVADO. Cheque os proxies primeiro.", "#ff6b6b")
            return
        _escrever("proxies_pool.txt", "\n".join(aprov))
        f_proxies.value = "\n".join(aprov)
        aviso(f"{len(aprov)} proxies aprovados setados no pool.")

    def _parar_pc(e):
        ev = estado.get("pc_parar")
        if ev:
            ev.set()
        txt_pc_prog.value = "Parando…"
        btn_pc_parar.disabled = True

    btn_checar = ft.FilledButton("🔎 Checar reputação", on_click=_checar_proxies,
                                 style=ft.ButtonStyle(bgcolor=ROXO))
    btn_setar = ft.FilledButton("✅ Setar aprovados no pool", on_click=_setar_aprovados,
                                style=ft.ButtonStyle(bgcolor=VERDE))
    btn_pc_parar = ft.OutlinedButton("■ Parar", on_click=_parar_pc, disabled=True)

    aba_proxy = ft.Container(padding=20, content=ft.Column([
        ft.Text("Proxies (SOCKS5)", size=18, weight=ft.FontWeight.BOLD, color="#ffffff"),
        ft.Text("Cole os proxies da RUN e clique OK. Sem editar arquivo na mão.",
                color=CINZA, size=12),
        f_proxies,
        ft.FilledButton("OK — salvar proxies", on_click=salvar_proxies,
                        style=ft.ButtonStyle(bgcolor=ROXO)),
        ft.Divider(color="#3a3a3d"),
        ft.Text("Checker de reputação (IPInfo + DNSBL + AbuseIPDB)", size=16,
                weight=ft.FontWeight.BOLD, color="#ffffff"),
        ft.Text("Checa os proxies acima e mantém SÓ os aprovados. ⚠️ Proxies de datacenter "
                "tendem a reprovar (flags hosting/proxy).", color=CINZA, size=12),
        f_pc_token, f_pc_echo, f_pc_abuse_keys,
        ft.Row([f_pc_threads, f_pc_to_proxy, f_pc_to_api, f_pc_abuse_max], spacing=12, wrap=True),
        ft.Row([sw_pc_dnsbl, sw_pc_abuse], spacing=20, wrap=True),
        ft.Row([f_pc_paises, f_pc_estados, f_pc_zips], spacing=12, wrap=True),
        ft.Row([btn_checar, btn_setar, btn_pc_parar], spacing=12, wrap=True),
        txt_pc_prog,
        ft.Container(content=txt_pc_res, bgcolor=CARD, padding=10, border_radius=6),
        ft.Text("Logs ao vivo do checker:", color=CINZA, size=12),
        ft.Container(content=lista_pc_logs, bgcolor="#000000", border_radius=6, height=170),
    ], spacing=12, scroll=ft.ScrollMode.AUTO))

    # ╔══ ABA TOKENS ══╗
    f_tokens = ft.TextField(label="Tokens / cookies (1 auth-token por linha; 'apelido,token' opcional)",
                            multiline=True, min_lines=14, max_lines=14,
                            value=_ler("tokens.txt"))

    def salvar_tokens(e):
        _escrever("tokens.txt", f_tokens.value)
        aviso(f"{_conta_linhas(f_tokens.value)} tokens salvos para esta RUN.")

    aba_tokens = ft.Container(padding=20, content=ft.Column([
        ft.Text("Tokens (contas)", size=18, weight=ft.FontWeight.BOLD, color="#ffffff"),
        ft.Text("Cole os auth-tokens da RUN e clique OK.", color=CINZA, size=12),
        f_tokens,
        ft.FilledButton("OK — salvar tokens", on_click=salvar_tokens,
                        style=ft.ButtonStyle(bgcolor=ROXO)),
    ], spacing=12))

    # ╔══ ABA CONFIGS ══╗
    def gr(k, d):
        return str(config_store.get("run", k, d))

    f_canais = ft.TextField(label="Canais alvo (separados por vírgula)",
                            value=", ".join(config_store.get("run", "canais", ["vitinho"])))
    f_nperfis = ft.TextField(label="Nº de perfis (0 = todos)", value=gr("n_perfis", 0), width=220)
    f_sess_min = ft.TextField(label="Tempo na live MIN (s)", value=gr("sessao_min_s", 60), width=220)
    f_sess_max = ft.TextField(label="Tempo na live MAX (s)", value=gr("sessao_max_s", 240), width=220)
    f_grace_min = ft.TextField(label="Espera pós-AD MIN (s)", value=gr("grace_min_s", 15), width=220)
    f_grace_max = ft.TextField(label="Espera pós-AD MAX (s)", value=gr("grace_max_s", 30), width=220)
    f_ad_marg = ft.TextField(label="Margem fim do AD (s)", value=gr("ad_margem_s", 6), width=220)
    _modo_salvo = str(config_store.get("run", "modo_abertura", "moderado")).lower()
    dd_modo = ft.Dropdown(
        label="Modo de abertura dos perfis",
        value=_modo_salvo if _modo_salvo in ("turbo", "moderado", "conservador") else "moderado",
        options=[
            ft.dropdown.Option(key="turbo", text="TURBO — mais rápido possível (no limite da API)"),
            ft.dropdown.Option(key="moderado", text="Moderado — meio-termo"),
            ft.dropdown.Option(key="conservador", text="Conservador — bem cadenciado (evita cap)"),
        ],
        color="#ffffff", bgcolor=CARD, border_color="#3a3a3d",
        label_style=ft.TextStyle(color=CINZA), expand=True)
    f_dark = ft.Switch(label="Tema escuro na Twitch",
                       value=bool(config_store.get("run", "tema_escuro", True)))
    f_q = ft.Switch(label="Forçar qualidade baixa",
                    value=bool(config_store.get("run", "forcar_qualidade", True)))
    f_q_alvo = ft.TextField(label="Qualidade alvo", value=gr("qualidade_alvo", "160p30"), width=220)
    f_bau = ft.Switch(label="Coletar baú", value=bool(config_store.get("run", "bau", True)))
    f_bau_check = ft.TextField(label="Checar baú a cada (s)", value=gr("bau_check_s", 30), width=220)
    f_prev = ft.Switch(label="Preview dos perfis (aba Preview)",
                       value=bool(config_store.get("run", "preview", True)))
    f_prev_int = ft.TextField(label="Preview: atualizar a cada (s)", value=gr("preview_intervalo", 2.0), width=220)
    f_pausa_min = ft.TextField(label="Pausa p/ reabrir MIN (s)", value=gr("pausa_reabrir_min_s", 8), width=220)
    f_pausa_max = ft.TextField(label="Pausa p/ reabrir MAX (s)", value=gr("pausa_reabrir_max_s", 20), width=220)
    f_to_nav = ft.TextField(label="Timeout navegação (ms)", value=gr("timeout_nav_ms", 30000), width=220)
    f_to_rede = ft.TextField(label="Timeout rede proxy (ms)", value=gr("timeout_rede_ms", 20000), width=220)
    f_proxy_falhas = ft.TextField(label="Descartar proxy após N falhas (0=nunca)", value=gr("proxy_max_falhas", 3), width=260)
    f_slate = ft.Switch(label="Descartar perfil no slate roxo (Commercial break)",
                        value=bool(config_store.get("run", "descartar_slate", True)))

    # ── LEGIBILIDADE: texto branco + fundo do campo + borda (some no fundo escuro) ──
    for _c in (f_api_key, f_api_base, f_api_filtro, f_proxies, f_tokens,
               f_canais, f_nperfis, f_sess_min, f_sess_max, f_grace_min, f_grace_max,
               f_ad_marg, f_bau_check, f_prev_int, f_pausa_min, f_pausa_max,
               f_q_alvo, f_to_nav, f_to_rede, f_proxy_falhas):
        _c.color = "#ffffff"
        _c.bgcolor = CARD
        _c.border_color = "#3a3a3d"
        _c.focused_border_color = ROXO
        _c.cursor_color = ROXO
        _c.label_style = ft.TextStyle(color=CINZA)
    for _s in (f_bau, f_prev, f_dark, f_q, f_slate):
        _s.label_style = ft.TextStyle(color="#ffffff")
        _s.active_color = ROXO

    def _i(tf, d):
        try:
            return int(float(tf.value))
        except (TypeError, ValueError):
            return d

    def _f(tf, d):
        try:
            return float(tf.value)
        except (TypeError, ValueError):
            return d

    def salvar_configs(e):
        canais = [c.strip().lstrip("/").lower() for c in f_canais.value.split(",") if c.strip()]
        config_store.salvar_secao("run", {
            "canais": canais or ["vitinho"],
            "n_perfis": _i(f_nperfis, 0),
            "sessao_min_s": _i(f_sess_min, 60),
            "sessao_max_s": _i(f_sess_max, 240),
            "grace_min_s": _f(f_grace_min, 15),
            "grace_max_s": _f(f_grace_max, 30),
            "ad_margem_s": _f(f_ad_marg, 6),
            "modo_abertura": dd_modo.value or "moderado",
            "bau": bool(f_bau.value),
            "bau_check_s": _i(f_bau_check, 30),
            "tema_escuro": bool(f_dark.value),
            "forcar_qualidade": bool(f_q.value),
            "qualidade_alvo": f_q_alvo.value.strip() or "160p30",
            "preview": bool(f_prev.value),
            "preview_intervalo": _f(f_prev_int, 2.0),
            "pausa_reabrir_min_s": _f(f_pausa_min, 8),
            "pausa_reabrir_max_s": _f(f_pausa_max, 20),
            "timeout_nav_ms": _i(f_to_nav, 30000),
            "timeout_rede_ms": _i(f_to_rede, 20000),
            "proxy_max_falhas": _i(f_proxy_falhas, 3),
            "descartar_slate": bool(f_slate.value),
        })
        aviso("Configurações da RUN salvas.")

    def _linha(*campos):
        return ft.Row(list(campos), spacing=12, wrap=True)

    aba_configs = ft.Container(padding=20, content=ft.Column([
        ft.Text("Configurações da RUN", size=18, weight=ft.FontWeight.BOLD, color="#ffffff"),
        f_canais,
        _linha(f_nperfis, f_sess_min, f_sess_max),
        _linha(f_grace_min, f_grace_max, f_ad_marg),
        dd_modo,
        ft.Text("TURBO = enche o mais rápido que a API permite · Moderado = meio-termo · "
                "Conservador = abre devagar, sem risco de cap.", color=CINZA, size=11),
        _linha(f_dark, f_q, f_q_alvo),
        _linha(f_bau, f_bau_check),
        _linha(f_prev, f_prev_int),
        _linha(f_pausa_min, f_pausa_max),
        _linha(f_to_nav, f_to_rede, f_proxy_falhas),
        f_slate,
        ft.FilledButton("Salvar", on_click=salvar_configs, style=ft.ButtonStyle(bgcolor=ROXO)),
    ], spacing=12, scroll=ft.ScrollMode.AUTO))

    # ╔══ ABA LOGS ══╗
    txt_contador = ft.Text("Nenhum anúncio assistido ainda.", color=VERDE, size=14,
                           weight=ft.FontWeight.BOLD)
    lista_logs = ft.ListView(expand=True, auto_scroll=True, spacing=1, padding=8)

    btn_iniciar = ft.FilledButton("▶ Iniciar RUN", style=ft.ButtonStyle(bgcolor=VERDE))
    btn_parar = ft.OutlinedButton("■ Parar", disabled=True)
    chk_preview = ft.Switch(label="Preview ao vivo (aba Preview)",
                            value=bool(config_store.get("run", "preview", True)),
                            active_color=ROXO, label_style=ft.TextStyle(color="#ffffff"))

    aba_logs = ft.Container(padding=12, expand=True, content=ft.Column([
        ft.Container(content=txt_contador, bgcolor=CARD, padding=10, border_radius=6),
        ft.Container(content=lista_logs, bgcolor="#000000", border_radius=6, expand=True),
        ft.Row([btn_iniciar, btn_parar, chk_preview], spacing=16,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
    ], spacing=10, expand=True))

    # ╔══ ABA PREVIEW (screenshots CDP ao vivo) ══╗
    grid_preview = ft.GridView(expand=True, max_extent=300, child_aspect_ratio=1.55,
                               spacing=6, run_spacing=6, padding=8)
    aba_preview = ft.Container(padding=6, expand=True, content=ft.Column([
        ft.Text("Preview ao vivo dos perfis (screenshots via CDP, ~2s) — funciona até minimizado.",
                color=CINZA, size=12),
        ft.Container(content=grid_preview, bgcolor="#000000", border_radius=6, expand=True),
    ], spacing=6, expand=True))

    _cards = {}   # n -> (Column, Image, Text)

    def _refresh_preview():
        while True:
            try:
                intervalo = float(config_store.get("run", "preview_intervalo", 2.0))
            except (TypeError, ValueError):
                intervalo = 2.0
            time.sleep(max(0.5, intervalo))
            try:
                # so trabalha quando a aba Preview esta aberta (alivia CDP + cliente Flet).
                na_aba = False
                try:
                    na_aba = (abas.selected_index == IDX_PREVIEW)
                except Exception:
                    na_aba = False
                # sinaliza a ENGINE (outro processo) p/ capturar SO quando a aba esta aberta
                try:
                    if na_aba:
                        open(ARQ_PREVIEW_FLAG, "w").close()
                    else:
                        os.remove(ARQ_PREVIEW_FLAG)
                except OSError:
                    pass
                if not na_aba:
                    continue
                # le os JPEGs que a engine gravou em PREVIEW_DIR
                try:
                    arqs = [f for f in os.listdir(PREVIEW_DIR)
                            if f.startswith("slot_") and f.endswith(".jpg")]
                except OSError:
                    arqs = []
                presentes = set()
                for fn in arqs:
                    try:
                        n = int(fn[len("slot_"):-len(".jpg")])
                    except ValueError:
                        continue
                    try:
                        with open(os.path.join(PREVIEW_DIR, fn), "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                    except OSError:
                        continue
                    presentes.add(n)
                    if n not in _cards:
                        img = ft.Image(src_base64=b64, fit=ft.ImageFit.COVER,
                                       border_radius=4, expand=True)
                        txt = ft.Text(f"Perfil {n}", color=CINZA, size=11)
                        _cards[n] = (ft.Column([img, txt], spacing=2, expand=True), img, txt)
                    else:
                        _cards[n][1].src_base64 = b64
                for n in list(_cards):
                    if n not in presentes:
                        _cards.pop(n, None)
                grid_preview.controls = [_cards[n][0] for n in sorted(_cards)]
            except Exception:
                pass
    threading.Thread(target=_refresh_preview, daemon=True).start()

    # ── log helpers ──
    def add_log(msg, cor=CINZA):
        lista_logs.controls.append(ft.Text(msg, color=cor, size=13, selectable=True))
        if len(lista_logs.controls) > 150:
            del lista_logs.controls[:len(lista_logs.controls) - 100]
        # update e feito pelo thread `atualizador`

    def atualiza_contador():
        c = estado["contador"]
        if not c:
            txt_contador.value = "Nenhum anúncio assistido ainda."
        else:
            txt_contador.value = "Anúncios assistidos — " + " · ".join(
                f"{canal}: {n}" for canal, n in sorted(c.items(), key=lambda x: -x[1]))
        # page.update() e feito pelo chamador/consumidor (evita corrida entre threads)

    # ── traducao de eventos -> mensagem didatica ──
    # Retorna (msg, cor, live). live=False => NAO mostra no painel ao vivo (evita o spam
    # rotineiro que travava a ListView), mas o ciclo segue gravado no ciclos_log.txt.
    def traduzir(ev):
        t = ev.get("tipo"); n = ev.get("n"); canal = ev.get("canal", "")
        if t == "run_inicio":
            return f"RUN iniciada — {ev.get('perfis')} perfis ({ev.get('canais')})", "#ffffff", True
        if t == "aberto":
            return f"Perfil {n} aberto · proxy ok", CINZA, False           # rotineiro: oculta ao vivo
        if t == "navegou":
            if ev.get("ok"):
                return f"Perfil {n} entrou em {canal}", CINZA, False        # rotineiro: oculta ao vivo
            return f"Perfil {n} não conseguiu entrar em {canal}", "#ff6b6b", True
        if t == "ad_on":   # INICIO: anuncio identificado
            return f"Perfil {n} — anúncio de {ev.get('dur')}s identificado em {canal}", "#ffd24a", True
        if t == "ad_off":  # MEIO: anuncio assistido com sucesso
            return f"Perfil {n} — anúncio assistido com sucesso ✅ ({ev.get('dur')}s)", VERDE, True
        if t == "bau":
            return f"Perfil {n} coletou o baú em {canal}", VERDE, False   # oculto ao vivo
        if t == "fim":     # FIM: perfil encerrando / reabrindo
            if ev.get("teve_ad"):
                estado["contador"][canal] = estado["contador"].get(canal, 0) + 1
                atualiza_contador()
            return f"Perfil {n} se encerrando (reabre em ~{ev.get('reabre', 0)}s)", CINZA, True
        if t == "proxy_morto":
            return f"Perfil {n}: proxy sem rede — trocando de proxy", "#ff6b6b", True
        if t == "proxy_descartado":
            return f"Perfil {n}: proxy descartado (sem rede demais) — fora do rodízio", "#ff6b6b", True
        if t == "slate":
            return f"Perfil {n}: anúncio 'slate' roxo (Commercial break) — perfil descartado, reciclando", "#ff6b6b", True
        if t == "falha_abrir":
            return f"Perfil {n}: falha ao abrir — tentando outra conta", "#ff6b6b", True
        if t == "erro":
            return f"Perfil {n}: erro ({ev.get('msg')})", "#ff6b6b", True
        if t == "resumo":
            return ev.get("txt", ""), "#7CFC00", True
        if t == "run_fim":
            return f"RUN encerrada ({ev.get('motivo','')}).", "#ffffff", True
        return None, CINZA, False

    # ── tailer: lê os eventos que a ENGINE (processo separado) anexa em ARQ_EVENTOS ──
    # Funciona como 'tail -f'. Ao (re)abrir a GUI, lê desde o início e reconstrói
    # contador+logs do estado atual da RUN. A GUI nunca roda a engine -> não congela.
    def tailer():
        while True:
            try:
                existe = os.path.exists(ARQ_EVENTOS)
                tam = os.path.getsize(ARQ_EVENTOS) if existe else 0
                pos = estado.get("tail_pos", 0)
                if (not existe) or tam < pos:        # recriado/truncado (nova RUN / 1º open)
                    pos = 0
                if tam > pos:
                    with open(ARQ_EVENTOS, "r", encoding="utf-8") as f:
                        f.seek(pos)
                        novas = f.readlines()
                        pos = f.tell()
                    estado["tail_pos"] = pos
                    for ln in novas:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            ev = json.loads(ln)
                        except Exception:
                            continue
                        msg, cor, live = traduzir(ev)   # traduzir ja atualiza o contador
                        if msg and live:
                            lista_logs.controls.append(ft.Text(msg, color=cor, size=13, selectable=True))
                        t = ev.get("tipo")
                        if t == "run_inicio":
                            estado["rodando"] = True
                            btn_iniciar.disabled = True
                            btn_parar.disabled = False
                        elif t == "run_fim":
                            estado["rodando"] = False
                            btn_iniciar.disabled = False
                            btn_parar.disabled = True
                    if len(lista_logs.controls) > 150:
                        del lista_logs.controls[:len(lista_logs.controls) - 100]
                else:
                    estado["tail_pos"] = pos
                # motor morreu sem run_fim? reseta os botoes
                proc = estado.get("proc")
                if proc is not None and proc.poll() is not None and estado["rodando"]:
                    estado["rodando"] = False
                    btn_iniciar.disabled = False
                    btn_parar.disabled = True
                    lista_logs.controls.append(ft.Text("Motor encerrou.", color="#ffffff", size=13))
                # REATTACH: a GUI reabriu e o replay do log marcou rodando=True por causa de
                # um run_inicio "pendurado" (motor morreu feio: reboot/crash/kill, sem run_fim).
                # Nao fomos nos que lancamos a engine (proc is None) e o heartbeat esta velho/
                # ausente -> a engine NAO esta viva. Destrava o Iniciar em vez de ficar preso.
                elif proc is None and estado["rodando"] and not _engine_vivo():
                    estado["rodando"] = False
                    btn_iniciar.disabled = False
                    btn_parar.disabled = True
            except Exception:
                pass                                  # NUNCA deixa a thread morrer
            time.sleep(0.4)

    threading.Thread(target=tailer, daemon=True).start()

    def atualizador():
        # UNICO chamador de page.update() — as outras threads so mexem nos controles.
        # Se um update for lento/pesado (preview), so atrasa o proximo tick; nada congela
        # nem morre. Logs/contador aparecem no proximo ciclo.
        while True:
            time.sleep(0.4)
            try:
                page.update()
            except Exception:
                pass
    threading.Thread(target=atualizador, daemon=True).start()

    # ── start / stop ── (a engine roda em PROCESSO separado; a GUI só lê o arquivo)
    def iniciar(e):
        if estado["rodando"]:
            return
        estado["rodando"] = True
        estado["contador"] = {}
        atualiza_contador()
        lista_logs.controls.clear()
        add_log("Preparando RUN… (motor em processo separado)", "#ffffff")
        btn_iniciar.disabled = True
        btn_parar.disabled = False
        # aplica a escolha do Preview DESTA run (a engine le do settings.json)
        run = config_store.carregar().get("run", {}) or {}
        run["preview"] = bool(chk_preview.value)
        config_store.salvar_secao("run", run)
        f_prev.value = chk_preview.value   # mantem a aba Configs em sincronia
        # limpa eventos/flags/heartbeat da run anterior e reinicia a posicao do tailer
        for arq in (ARQ_EVENTOS, ARQ_PARAR_FLAG, ARQ_PREVIEW_FLAG, ARQ_ENGINE_PID):
            try:
                os.remove(arq)
            except OSError:
                pass
        estado["tail_pos"] = 0
        # lanca a ENGINE: o MESMO exe com --engine (frozen) ou 'python gui.py --engine'
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--engine"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--engine"]
        try:
            estado["proc"] = subprocess.Popen(cmd, cwd=paths.base_dir())
        except Exception as ex:
            add_log(f"Falha ao iniciar o motor: {str(ex)[:100]}", "#ff6b6b")
            estado["rodando"] = False
            btn_iniciar.disabled = False
            btn_parar.disabled = True

    def parar(e):
        add_log("Parando… o motor vai fechar os perfis.", "#ffffff")
        try:
            open(ARQ_PARAR_FLAG, "w").close()   # flag -> a engine encerra (fecha perfis + resumo)
        except OSError:
            pass
        btn_parar.disabled = True

    btn_iniciar.on_click = iniciar
    btn_parar.on_click = parar

    # ── botao trocar modo da UI (app <-> web): fecha ESTA janela e reabre no outro modo.
    #    A engine (se rodando) e processo separado -> a RUN NAO e afetada.
    _modo_atual = (os.environ.get("MURIADS_GUI") or "app").strip().lower()
    _is_app = _modo_atual in ("app", "desktop", "flet_app")

    def _trocar_modo(e):
        alvo = "web" if _is_app else "app"
        env = {**os.environ, "MURIADS_GUI": alvo}
        cmd = ([sys.executable] if getattr(sys, "frozen", False)
               else [sys.executable, os.path.abspath(__file__)])
        try:
            subprocess.Popen(cmd, env=env, cwd=paths.base_dir())
        except Exception:
            return
        # fecha esta instancia SOZINHA (a engine, se rodando, continua em processo separado):
        # 1) fecha a janela desktop graciosamente; 2) encerra o processo (fallback garantido).
        try:
            page.window.destroy()
        except Exception:
            pass
        threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()

    btn_modo = ft.OutlinedButton(
        ("🌐 Abrir em modo Web" if _is_app else "🖥 Abrir em modo App"),
        on_click=_trocar_modo,
        tooltip="Fecha esta janela e reabre no outro modo. A RUN em andamento continua.")
    header = ft.Container(content=ft.Row([btn_modo], alignment=ft.MainAxisAlignment.END),
                          padding=ft.padding.only(left=12, right=12, top=4, bottom=2))

    # ╔══ MONTAGEM ══╗
    abas = ft.Tabs(selected_index=0, expand=True, indicator_color=ROXO,
                   label_color="#ffffff", unselected_label_color=CINZA, tabs=[
        ft.Tab(text="APIs", content=aba_apis),
        ft.Tab(text="Proxy", content=aba_proxy),
        ft.Tab(text="Tokens", content=aba_tokens),
        ft.Tab(text="Configs", content=aba_configs),
        ft.Tab(text="Preview", content=aba_preview),
        ft.Tab(text="Logs ao vivo", content=aba_logs),
    ])

    page.add(ft.Column([banner, header, abas], spacing=0, expand=True))


def _rodar_engine():
    """Modo ENGINE (processo separado, lançado pela GUI com --engine): roda o orquestrador,
    grava eventos em ARQ_EVENTOS e observa ARQ_PARAR_FLAG (parada) e ARQ_PREVIEW_FLAG (preview).
    Roda em processo PRÓPRIO -> não disputa CPU/GIL com a UI (a GUI não congela mais)."""
    eventos.set_sink(eventos.sink_arquivo(ARQ_EVENTOS))

    # HEARTBEAT: enquanto o motor vive, (re)escreve ARQ_ENGINE_PID a cada ENGINE_HEARTBEAT_S.
    # A GUI usa o mtime desse arquivo p/ saber se a engine esta viva. Se o motor morrer feio
    # (reboot/crash/kill), o arquivo para de ser tocado -> a GUI destrava o Iniciar sozinha.
    def _heartbeat():
        while not eventos.parar.is_set():
            try:
                with open(ARQ_ENGINE_PID, "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass
            time.sleep(ENGINE_HEARTBEAT_S)
    threading.Thread(target=_heartbeat, daemon=True).start()

    def _watch_flags():
        while not eventos.parar.is_set():
            if os.path.exists(ARQ_PARAR_FLAG):
                eventos.parar.set()
                return
            preview.set_ativo(os.path.exists(ARQ_PREVIEW_FLAG))  # captura só quando a GUI pede
            time.sleep(0.4)
    threading.Thread(target=_watch_flags, daemon=True).start()
    try:
        asyncio.run(orquestrador.amain())
    except Exception as ex:
        try:
            with open(ARQ_EVENTOS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tipo": "erro", "n": "-", "msg": str(ex)[:120]}) + "\n")
                f.write(json.dumps({"tipo": "run_fim", "motivo": "erro"}) + "\n")
        except Exception:
            pass
    finally:
        # saida limpa: remove o heartbeat na hora (a GUI ja ve a engine como morta).
        try:
            os.remove(ARQ_ENGINE_PID)
        except OSError:
            pass


if __name__ == "__main__":
    import sys
    # Modo ENGINE: a GUI relança ESTE exe com --engine (motor em processo separado).
    if "--engine" in sys.argv:
        _rodar_engine()
        sys.exit(0)
    # Empacotado em .exe: o orquestrador relanca ESTE exe com --taskview p/ abrir a
    # grade DWM (nao existe 'python taskview.py' dentro do bundle).
    if "--taskview" in sys.argv:
        import taskview
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--taskview"]
        taskview.main()
        sys.exit(0)
    # MODO da UI (sem precisar rebuildar — troca pela variavel de ambiente MURIADS_GUI):
    #   web (PADRAO): abre no navegador. Mais estavel em runs longas; F5 recupera sem
    #                 matar a RUN; fechar a aba nao para o backend.
    #   app/desktop : janela nativa do Flet (cuidado: congela em sessoes longas).
    _modo = (os.environ.get("MURIADS_GUI") or "app").strip().lower()
    if _modo in ("app", "desktop", "flet_app"):
        ft.app(target=main, assets_dir=paths.assets_dir())            # janela desktop nativa
    else:
        ft.app(target=main, assets_dir=paths.assets_dir(),
               view=ft.AppView.WEB_BROWSER, port=8553)                # navegador (padrao)
