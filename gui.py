"""
MURIADS — Interface grafica (Flet).
Abas: APIs (AdsPower) · Proxy · Tokens · Configs · Preview · Logs ao vivo.
Roda o orquestrador no mesmo processo (thread daemon) e mostra logs didaticos +
contador de anuncios assistidos por canal + preview ao vivo (screenshots CDP).

  pip install flet==0.28.3 requests playwright pywin32 pillow
  python gui.py
"""
import asyncio
import os
import queue
import threading
import time

import flet as ft

import config_store
import eventos
import paths
import preview
import swap

# orquestrador importa swap/navegacao/ad_detector — pesado, mas ok no start
import orquestrador

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
    estado = {"rodando": False, "contador": {}}
    # UI: APENAS o thread `atualizador` chama page.update(). As demais threads (consumidor
    # de logs, preview) e os handlers só MEXEM nos controles -> zero corrida/lock/starvation.

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
    _g_salvo = str(config_store.get("adspower", "group_id", "") or "")
    dd_group = ft.Dropdown(
        label="Grupo de perfis (clique em Detectar grupos)",
        value=_g_salvo,
        options=([ft.dropdown.Option(key="", text="Todos os grupos")]
                 + ([ft.dropdown.Option(key=_g_salvo, text=f"(salvo) {_g_salvo}")]
                    if _g_salvo else [])),
        color="#ffffff", bgcolor=CARD, border_color="#3a3a3d",
        label_style=ft.TextStyle(color=CINZA), expand=True)
    f_api_filtro = ft.TextField(label="Filtro de nome (opcional)",
                                value=config_store.get("adspower", "filtro_nome", ""))

    def _salvar_adspower():
        config_store.salvar_secao("adspower", {
            "api_key": f_api_key.value.strip(),
            "base": f_api_base.value.strip() or "http://local.adspower.net:50325",
            "group_id": dd_group.value or "",
            "filtro_nome": f_api_filtro.value.strip(),
        })

    def detectar_grupos(e):
        _salvar_adspower()                 # usa a key/base atuais
        swap.aplicar_config_adspower()
        try:
            grupos = swap.listar_grupos()
        except Exception as ex:
            aviso(f"Erro ao listar grupos: {str(ex)[:70]}", "#ff6b6b")
            return
        atual = dd_group.value
        dd_group.options = ([ft.dropdown.Option(key="", text="Todos os grupos")]
                            + [ft.dropdown.Option(key=str(g["group_id"]),
                                                  text=g["group_name"] or g["group_id"])
                               for g in grupos])
        chaves = {o.key for o in dd_group.options}
        dd_group.value = atual if atual in chaves else ""
        aviso(f"{len(grupos)} grupo(s) detectado(s).")

    def salvar_apis(e):
        _salvar_adspower()
        aviso("Configurações do AdsPower salvas.")

    aba_apis = ft.Container(padding=20, content=ft.Column([
        ft.Text("AdsPower", size=18, weight=ft.FontWeight.BOLD, color="#ffffff"),
        ft.Text("Coloque a API key, clique Detectar grupos e escolha o grupo deste server.",
                color=CINZA, size=12),
        f_api_key, f_api_base,
        ft.Row([dd_group, ft.OutlinedButton("Detectar grupos", on_click=detectar_grupos)],
               vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
        f_api_filtro,
        ft.FilledButton("Salvar", on_click=salvar_apis,
                        style=ft.ButtonStyle(bgcolor=ROXO)),
    ], spacing=12))

    # ╔══ ABA PROXY ══╗
    f_proxies = ft.TextField(label="Proxies (1 por linha: host:port ou host:port:user:senha)",
                             multiline=True, min_lines=14, max_lines=14,
                             value=_ler("proxies_pool.txt"))

    def salvar_proxies(e):
        _escrever("proxies_pool.txt", f_proxies.value)
        aviso(f"{_conta_linhas(f_proxies.value)} proxies salvos para esta RUN.")

    aba_proxy = ft.Container(padding=20, content=ft.Column([
        ft.Text("Proxies (SOCKS5)", size=18, weight=ft.FontWeight.BOLD, color="#ffffff"),
        ft.Text("Cole os proxies da RUN e clique OK. Sem editar arquivo na mão.",
                color=CINZA, size=12),
        f_proxies,
        ft.FilledButton("OK — salvar proxies", on_click=salvar_proxies,
                        style=ft.ButtonStyle(bgcolor=ROXO)),
    ], spacing=12))

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
    for _s in (f_bau, f_prev, f_dark, f_q):
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
                shots = preview.get_shots()
                for n in list(_cards):
                    if n not in shots:
                        _cards.pop(n, None)
                for n, (b64, canal) in shots.items():
                    if n not in _cards:
                        img = ft.Image(src_base64=b64, fit=ft.ImageFit.COVER,
                                       border_radius=4, expand=True)
                        txt = ft.Text(f"Perfil {n} · {canal}", color=CINZA, size=11)
                        _cards[n] = (ft.Column([img, txt], spacing=2, expand=True), img, txt)
                    else:
                        _cards[n][1].src_base64 = b64
                        _cards[n][2].value = f"Perfil {n} · {canal}"
                grid_preview.controls = [_cards[n][0] for n in sorted(_cards)]
            except Exception:
                pass
    threading.Thread(target=_refresh_preview, daemon=True).start()

    # ── log helpers ──
    def add_log(msg, cor=CINZA):
        lista_logs.controls.append(ft.Text(msg, color=cor, size=13, selectable=True))
        if len(lista_logs.controls) > 600:
            del lista_logs.controls[:200]
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
    def traduzir(ev):
        t = ev.get("tipo"); n = ev.get("n"); canal = ev.get("canal", "")
        if t == "run_inicio":
            return f"RUN iniciada — {ev.get('perfis')} perfis ({ev.get('canais')})", "#ffffff"
        if t == "aberto":
            return f"Perfil {n} aberto · proxy ok", CINZA
        if t == "navegou":
            return (f"Perfil {n} entrou em {canal}" if ev.get("ok")
                    else f"Perfil {n} não conseguiu entrar em {canal}"), CINZA
        if t == "ad_on":
            return f"Perfil {n} assistindo a um anúncio de {ev.get('dur')}s em {canal}…", "#ffd24a"
        if t == "ad_off":
            return f"Perfil {n} — anúncio terminou em {canal} (fecha em ~{ev.get('grace')}s)", "#ffd24a"
        if t == "bau":
            return f"Perfil {n} coletou o baú em {canal}", VERDE
        if t == "fim":
            if ev.get("teve_ad"):
                estado["contador"][canal] = estado["contador"].get(canal, 0) + 1
                atualiza_contador()
                return f"Perfil {n} saiu de {canal} — assistiu anúncio ✅ (durou {ev.get('dur')}s)", VERDE
            return f"Perfil {n} saiu de {canal} (durou {ev.get('dur')}s)", CINZA
        if t == "proxy_morto":
            return f"Perfil {n}: proxy sem rede — trocando de proxy", "#ff6b6b"
        if t == "proxy_descartado":
            return f"Perfil {n}: proxy descartado (sem rede demais) — fora do rodízio", "#ff6b6b"
        if t == "falha_abrir":
            return f"Perfil {n}: falha ao abrir — tentando outra conta", "#ff6b6b"
        if t == "erro":
            return f"Perfil {n}: erro ({ev.get('msg')})", "#ff6b6b"
        if t == "run_fim":
            return f"RUN encerrada ({ev.get('motivo','')}).", "#ffffff"
        return None, CINZA

    # ── consumidor da fila de eventos (thread) ──
    fila = queue.Queue()

    def consumidor():
        while True:
            try:
                ev = fila.get()                      # bloqueia ate ter 1 evento
                lote = [ev]
                try:                                  # drena o que ja chegou (batch)
                    while len(lote) < 300:
                        lote.append(fila.get_nowait())
                except queue.Empty:
                    pass
                for e in lote:
                    if not e:
                        continue
                    msg, cor = traduzir(e)            # traduzir ja atualiza o contador (sem update)
                    if msg:
                        lista_logs.controls.append(ft.Text(msg, color=cor, size=13, selectable=True))
                    if e.get("tipo") == "run_fim":
                        estado["rodando"] = False
                        btn_iniciar.disabled = False
                        btn_parar.disabled = True
                if len(lista_logs.controls) > 600:
                    del lista_logs.controls[:len(lista_logs.controls) - 400]
            except Exception:
                pass                                  # NUNCA deixa a thread morrer

    threading.Thread(target=consumidor, daemon=True).start()

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

    # ── start / stop ──
    def iniciar(e):
        if estado["rodando"]:
            return
        estado["rodando"] = True
        estado["contador"] = {}
        atualiza_contador()
        lista_logs.controls.clear()
        add_log("Preparando RUN…", "#ffffff")
        btn_iniciar.disabled = True
        btn_parar.disabled = False
        # aplica a escolha do Preview DESTA run (orquestrador le do settings.json)
        run = config_store.carregar().get("run", {}) or {}
        run["preview"] = bool(chk_preview.value)
        config_store.salvar_secao("run", run)
        f_prev.value = chk_preview.value   # mantem a aba Configs em sincronia
        eventos.reset()
        eventos.set_sink(fila.put)

        def _run():
            try:
                asyncio.run(orquestrador.amain())
            except Exception as ex:
                fila.put({"tipo": "erro", "n": "-", "msg": str(ex)[:120]})
                fila.put({"tipo": "run_fim", "motivo": "erro"})
        threading.Thread(target=_run, daemon=True).start()

    def parar(e):
        add_log("Parando… fechando perfis.", "#ffffff")
        eventos.parar.set()
        btn_parar.disabled = True

    btn_iniciar.on_click = iniciar
    btn_parar.on_click = parar

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

    page.add(ft.Column([banner, abas], spacing=0, expand=True))


if __name__ == "__main__":
    import sys
    # Empacotado em .exe: o orquestrador relanca ESTE exe com --taskview p/ abrir a
    # grade DWM (nao existe 'python taskview.py' dentro do bundle).
    if "--taskview" in sys.argv:
        import taskview
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--taskview"]
        taskview.main()
        sys.exit(0)
    ft.app(target=main, assets_dir=paths.assets_dir())
