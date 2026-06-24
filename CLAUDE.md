# MURIADS (pasta twitch_swap) — contexto do projeto (handoff p/ Claude Code)

Este arquivo carrega automaticamente quando o Claude Code roda nesta pasta. É o
"estado do projeto" pra continuar de onde paramos.

> **Nome do produto = MURIADS** (título da GUI, .exe e instalador). A pasta de dev
> ainda se chama `twitch_swap` (o código é agnóstico ao nome da pasta — `paths.base_dir`).

## O que é
Projeto **novo e separado** dos outros (MURI_PRO / MURI_PRO_2). Objetivo: **economizar
no plano de perfis** do AdsPower — reusar **poucos perfis** (ex: 50) rodando em **rodízio**
uma pool **maior de contas** (ex: 200). Cada conta roda numa sessão curta (10–15 min), e ao
reiniciar entra com **conta + proxy + fingerprint diferentes**.

Arquitetura: vai usar **CDP** (`connect_over_cdp`) pra dirigir o navegador (assistir/interagir),
**não** o clicker OpenCV do MURI_PRO_2. Isso alinha mais com o MURI_PRO original (CDP + GUI Flet).
A camada de **swap** (quem está logado em cada perfil) é **100% Local API do AdsPower** — agnóstica
a CDP. O CDP é a camada de cima (driver do navegador), a implementar.

## A receita do swap (validada ao vivo, AdsPower)
Por abertura, com o perfil **FECHADO**, nesta ordem:
1. `GET  /api/v1/browser/stop`  (garante fechado)
2. `POST /api/v2/browser-profile/delete-cache`
   body `{"profile_id":[uid], "type":["cookie","local_storage","indexeddb"]}`
3. `POST /api/v1/user/update` com:
   - `user_proxy_config` (socks5: host/port/user/pw)
   - `cookie` = **string JSON** do auth-token (`json.dumps`, com `id` incremental)
   - `fingerprint_config` (aleatório coerente)
   - `open_urls: [home da Twitch]`
   - `ignore_cookie_error: "1"`
4. `GET  /api/v1/browser/start`  (`open_tabs=0`, `ip_tab=0`)

### Por que o delete-cache é a peça-chave
`update cookie` só grava no **metadata** do perfil. O auth-token/sessão antigos persistem no
**cookie store + localStorage em disco**, e o Twitch lê os ANTIGOS → o login **não troca**
(confirmado: 2 testes sem limpar não trocaram a conta; com delete-cache, trocou). Limpar
`cookie`+`local_storage`+`indexeddb` antes do `update` é o que destrava o swap.

### Sobre o cookie
Só o **auth-token** basta pra logar no Twitch (mesmo papel do antigo
`document.cookie = "auth-token=..."`, mas injetado pela API **antes** de navegar — por isso
funciona sem precisar estar na página). Com CDP de volta, o caminho `document.cookie` também
volta a existir como alternativa (injetar depois de carregar a página).

## Decisões tomadas
- **Modelo SIMPLES**: fingerprint **aleatório a cada abertura** (não fixo-por-conta). Para
  fixar fp por conta no futuro: salvar o `fingerprint_config` junto da conta no pool e reaplicar.
- **Baixíssima frequência** de uso → reuso frequente / "device novo" a cada login não preocupa.
- **Concorrência é 1:1**: N perfis = N contas ONLINE ao mesmo tempo. O swap economiza via
  **rodízio no tempo**, NÃO multiplica viewers simultâneos.
- **Fingerprint aleatório deve ser COERENTE** (UA/OS/GPU/resolução/timezone plausíveis juntos).
  Aleatoriedade incoerente clusteriza mais, não menos. timezone/geo seguem o IP do proxy.

## Estrutura / arquivos
```
swap.py            orquestrador: 1 thread/perfil; loop stop→delcache→update→start→sessão→stop
tokens.txt         FONTE dos cookies: 1 auth-token por linha (cole o grupo aqui). Opcional
                   "apelido,token"; '#'=comentário; vazias e duplicados ignorados.
contas_pool.json   GERADO automaticamente de tokens.txt ao iniciar (não editar à mão).
                   [{"id":"c001","auth_token":"..."}]  (id = apelido da conta, só log;
                   NÃO é o user_id do perfil).
proxies_pool.txt   socks5: host:port  ou  host:port:user:senha  (1/linha)
```
Perfis são DETECTADOS via AdsPower API (`swap.listar_perfis` -> `/api/v1/user/list`,
paginado, ordenado pelo nº do nome) — **sem `perfis.txt`, sem fallback**. Se a API não
responder, roda com 0 perfis (o orquestrador avisa e não sobe nada). Limite a X perfis
com `N_PERFIS`/argv (ex.: 100 existem, quero 25). Filtros opcionais:
`swap.PERFIS_GROUP_ID` / `swap.PERFIS_FILTRO_NOME`.

**Fluxo do tokens.txt:** você mantém só o `tokens.txt`. Ao rodar `python swap.py`, a função
`construir_contas_pool()` lê o `tokens.txt`, remove duplicados, atribui apelidos (c001, c002…)
e (re)grava o `contas_pool.json` que é usado pra injeção. Assim dá pra trocar o grupo de cookies
(colar outro lote no tokens.txt) sem mexer no JSON. Se `tokens.txt` não existir, cai no
`contas_pool.json` já presente. O **tempo de sessão** (quanto cada conta fica na stream antes de
fechar e logar outra) se configura em `swap.py`: `SESSAO_MIN_S`/`SESSAO_MAX_S`.
Três identificadores distintos: `id` da conta (contas_pool.json) ≠ `auth_token` (o cookie) ≠
`user_id` do perfil (perfis.txt). O swap.py pareia conta+proxy+perfil dinamicamente em runtime
(filas que se revezam); nada é fixo.

## Como rodar
- **GUI (recomendado):** `pip install -r requirements.txt` → `python gui.py`. Preencher
  abas APIs/Proxy/Tokens/Configs (salvam em `settings.json` + `proxies_pool.txt`/`tokens.txt`)
  → aba **Logs ao vivo** → **Iniciar RUN**.
- **CLI:** `python orquestrador.py [n_perfis] [canais,virgula]` (lê `settings.json` se houver).
- AdsPower aberto (Local API em `local.adspower.net:50325`).

## GUI — `gui.py` (Flet 0.28.3) [FEITO, base]
Interface própria (separada do MURI). Banner no topo + abas:
- **APIs**: AdsPower (api_key/base/filtro) → `settings.json["adspower"]`. Botão **Detectar
  grupos** (`swap.listar_grupos` → `/api/v1/group/list`) popula um **dropdown** p/ escolher o
  grupo de perfis daquele server (salvo em `group_id`; `swap.listar_perfis` filtra por ele).
- **Proxy** / **Tokens**: textarea "cole e OK" → escreve `proxies_pool.txt` / `tokens.txt`
  (sem editar arquivo na mão).
- **Configs**: canais (vírgula) + TODOS os ajustes do orquestrador → `settings.json["run"]`.
- **Logs ao vivo**: mensagens didáticas ("Perfil 1 assistindo a um anúncio de 90s em
  gaules…") + **contador de anúncios por canal** no topo (incrementa em `fim` com `teve_ad`).
  Botões Iniciar/Parar RUN.

**Arquitetura GUI:** a GUI roda `orquestrador.amain()` numa **thread daemon** (não subprocess
— exe-ready); o orquestrador emite eventos via `eventos.emit(nome, **dados)` (sink trocável:
print `@EVT@` no CLI, `queue.put` na GUI); um consumidor traduz p/ mensagens. **Stop** =
`eventos.parar` (threading.Event) → `_watch_parar` vira o `asyncio.Event` → cancela slots +
fecha perfis/TaskView.
- `paths.py` — `base_dir()`/`arquivo()` (funciona empacotado em .exe; arquivos de runtime ao
  lado do exe). `config_store.py` — lê/grava `settings.json` (gitignored, tem api_key).
- `_aplicar_config_run()` no orquestrador sobrescreve os globais a partir de `settings.json`.
- **Distribuição .exe (follow-up):** `flet pack`/PyInstaller; segredos (settings/tokens/proxies)
  são runtime, fora do bundle. `assets/banner.png` é o único asset.

## Identificador de ADS — `ad_detector.py` (FEITO, detecção via CDP)
Twitch usa **SSAI** (anúncio costurado no vídeo) → fonte da verdade = manifest HLS (.m3u8 de
`*.playlist.ttvnw.net`). Cada anúncio é uma tag `#EXT-X-DATERANGE CLASS="twitch-stitched-ad"`
com `START-DATE`, `DURATION`, `X-TV-TWITCH-AD-ROLL-TYPE` (MIDROLL/PREROLL),
`X-TV-TWITCH-AD-POD-FILLED-DURATION` (total do pod) e `COMMERCIAL-ID` (agrupa o pod).
- `ad_detector.py` conecta no perfil via `connect_over_cdp`, escuta respostas, filtra
  `.m3u8`+`ttvnw.net`, parseia e emite **AD_START** (início, tipo, duração total, fim previsto,
  nº de anúncios) e **AD_END**. Log em `ads_log.jsonl`.
- **Precisão do FIM (não fechar no meio do ad):** AD_END dispara SÓ quando
  `agora >= inicio + duração + AD_FIM_MARGEM_S` (margem cobre a latência do HLS — o playhead
  do viewer fica atrás do live-edge). O gatilho antigo "daterange sumiu do manifest" foi
  REMOVIDO (encerrava ~3s cedo, fechava no meio). Margem via `AdState(margem_fim_s=...)`.
- **Confirmação por DOM:** `navegacao.ad_na_tela(page)` checa o overlay do player
  (`video-ad-label`/`video-ad-countdown`); o orquestrador, na hora de fechar, NÃO fecha se o
  overlay ainda estiver visível (re-checa em 3s) — garante que o viewer não está mais no ad.
- `parse_ads(texto_manifest)` é puro/testável; validado contra os HARs em `identificador de ad/`.
- Teste isolado: `python ad_detector.py <debug_port> [rotulo]` (perfil já aberto). Requer
  `pip install playwright` (sem baixar browser — conecta a um existente).
- Pod com vários anúncios: trata como UM bloco comercial (mesmo COMMERCIAL-ID); ex. memfps =
  60+30+15s, pod total 105s.

## Navegação ao canal — `navegacao.py` (FEITO, via CDP, replica o MURI_PRO)
3 camadas de fallback (igual `ir_para_canal_alvo` do MURI_PRO):
0. **SEMPRE parte da home** (`comecar_da_home=True`): o Chromium faz session-restore e
   reabre a aba do canal anterior; sem forçar a home, o perfil abriria já no canal e o
   fluxo orgânico seria pulado. Então volta pra `twitch.tv/` antes de procurar.
1. **HOMEPAGE/SIDEBAR**: procura `a[href="/<canal>" i]` por toda a home + sidebar (cards,
   Followed, Recommended); expande sidebar colapsada (`side-nav-arrow`), clica "Show More"
   (`side-nav-show-more-button`), rola a sidebar, e CLICA no link (navegação orgânica).
2. **BUSCA**: clica no `search-link`, digita o canal (humanizado) no `tw-input`, clica no
   resultado (`a[href="/<canal>" i]`); tenta Enter se preciso.
3. **URL DIRETO**: `page.goto` e, se falhar, CDP `Page.navigate`.
- Seletores idênticos aos do MURI_PRO. **Humanização via `mouse_humano.py`** (portado do
  MURI_PRO, copiado verbatim — self-contained): cliques/hover/scroll usam **mouse REAL via
  CDP** (Input.dispatchMouseEvent, curvas Bézier + tremor AR(1) + Fitts). `digitar_humano`
  com typos realistas. `_clicar()` = `mouse_humano.mover_para_elemento(clicar=True)`.
- Import prefere **Patchright** (anti-detecção), cai p/ playwright puro.
- **DESMUTAR (mouse real)**: ao chegar no canal, `ir_para_canal` chama `desmutar(page)` —
  fecha gate de conteúdo (mature/"Start Watching"), faz hover no player e, se o `aria-label`
  do `player-mute-unmute-button` indicar mudo, **clica com mouse_humano** p/ desmutar
  (fallback tecla `m`). Controlável por `ir_para_canal(..., desmutar_apos=True)`.
- **PREFERÊNCIAS (tema dark + 160p) ANTES de navegar**: `aplicar_preferencias(page)` faz
  `context.add_init_script` semeando `localStorage` da Twitch (`twilight.theme="1"` +
  `video-quality={"default":"160p30"}`) — carrega já dark + 160p do 1º frame e **sobrevive ao
  delcache**. Chamado no orquestrador logo após conectar, antes de navegar. Config: `tema_escuro`,
  `forcar_qualidade`, `qualidade_alvo` (GUI aba Configs).
- **AUTO-ACCEPT banners**: `fechar_banners(page)` clica (mouse real) nos banners de
  consentimento que sujam a tela — cookies ("Proceed") e Termos/"Heads Up" ("Accept").
  Chamado após navegar e a cada ~12s no loop (alguns aparecem depois).
- **RESGATAR BAÚ**: `resgatar_bau(page)` clica (mouse real) no baú de community points se
  visível (`button[aria-label*="laim"|"esgatar"|"onus" i]`). O orquestrador chama a cada
  `BAU_CHECK_S` (30s) durante a sessão. Flag `BAU`.
- Anti "navega direto no URL": `ir_para_canal` espera a home renderizar (`wait_for_selector`
  sidebar/busca) e a busca espera o resultado ficar visível antes de clicar — reduz cair no
  Tier 3. Conta nova não segue o canal → normal cair no Tier 2 (busca).
- API: `await ir_para_canal(page, canal, rotulo)` (recebe page já conectada). Standalone:
  `python navegacao.py <debug_port> <canal> [rotulo]`.

## Orquestrador CDP — `orquestrador.py` (FEITO, fluxo completo)
Amarra tudo. **Multi-canal**: `CANAIS` = lista; distribuição **fixa por perfil
(balanceada)** — perfil `i` → `CANAIS[i % M]` (cada perfil fica sempre no mesmo canal;
`atribuicao` montada no start, split impresso ex. `vitinho(3), gaules(2)`). argv[2]
sobrescreve por vírgula: `python orquestrador.py 6 vitinho,gaules,loud`.
Por slot (1 task async por perfil), em loop:
1. pega conta (cookie) + proxy livres da pool;
2. `swap`: stop -> delcache -> update(cookie+proxy+fingerprint) -> start (pega `debug_port`);
3. `connect_over_cdp` na porta; **checa rede do proxy** (`navegacao.tem_rede` carrega a
   home; residencial cai muito) — sem rede levanta `ProxySemRede` -> fecha o perfil e
   reinicia o ciclo com OUTRO proxy (o proxy ruim vai pro fim da fila). Com rede,
   `navegacao.ir_para_canal` (sidebar->busca->URL, já na home: `comecar_da_home=False`);
4. fica `SESSAO_MIN_S..MAX_S` no canal; o `ad_detector` (anexado à page) vigia anúncio:
   **nunca fecha no meio do anúncio**; ao detectar ad, **ignora o tempo base** e fecha o
   ciclo após **espera RANDOMIZADA `GRACE_POS_AD_MIN_S..MAX_S`** do fim do ad
   (`fechar_apos = fim_do_ad + rand(min,max)`, override — pode fechar antes do mínimo;
   ad que começa perto do fim adia o fechamento); detector é **1 START/1 END por bloco**
   (commercial_id em `encerrados` evita re-disparo enquanto o daterange persiste);
5. fecha o perfil (`swap.stop`), devolve conta+proxy (rotaciona IP e cookie), repete.
- Config no topo: `CANAL`, `N_PERFIS`, `SESSAO_MIN_S/MAX_S`, `GRACE_POS_AD_MIN_S/MAX_S`,
  `AD_FIM_MARGEM_S`, `API_MIN_INTERVALO_S`, `STAGGER_START_S`, `BAU/BAU_CHECK_S`,
  `LOG_CICLOS/ARQ_LOG_CICLOS`.
- **Log de ciclos** (`log_ciclo`): 1 linha por ciclo em `ciclos_log.txt` (gitignored, tem
  tokens) — `dd/mm HH:MM:SS > TOKEN SETADO: ... > PROXY SETADO: ... > CANAL: ... > navegou >
  AD: MIDROLL 30s > DUROU: Ns`. Sem ID de perfil. Cobre falha de abrir e proxy sem rede.
- Uso: `python orquestrador.py [n_perfis] [canal]` · Ctrl+C fecha os perfis.
- AdsPower (requests, bloqueante) roda via `asyncio.to_thread`; tudo o resto é async.
- **Anti-engasgo (abertura)**: `MAX_ABRINDO` (semáforo) limita quantos perfis estão
  *abrindo+navegando* ao mesmo tempo; a vaga é liberada (`libera_cb`) assim que o perfil já
  está assistindo — suaviza picos de CPU/RAM em aberturas E reaberturas (não só na largada,
  que é o `STAGGER_START_S`). Default 4. Config na aba Configs.
- **Rate limit AdsPower**: TODAS as chamadas passam por `_ads()` (gate global com
  `API_MIN_INTERVALO_S`, default 0.7s -> ~85/min, sob os 100 RPM). **Start escalonado**
  (`STAGGER_START_S` entre perfis) suaviza o burst inicial e a carga de CPU. Ciclo agora
  = **4 chamadas** (delcache, update, start, stop) — sem o stop fixo no início (delcache
  com guarda: se falhar por perfil aberto, faz stop+retry 1x).

## Preview — `preview.py` (FEITO, screenshots CDP dentro da GUI)
**Substituiu o TaskView DWM.** Captura `page.screenshot` (jpeg) de cada perfil ativo a cada
`PREVIEW_INTERVALO` (~2s), faz downscale (Pillow) e guarda em base64 num store thread-safe.
Funciona mesmo com a janela minimizada/coberta (CDP renderiza). Exibido na **aba Preview da
própria GUI** (grade `GridView` de imagens base64, atualizada por thread).
- Orquestrador: `preview.registrar(slot_n, page, canal)` ao navegar / `desregistrar` ao fechar;
  `preview.capturador()` roda como task; `preview.limpar()` no fim.
- Flags: `PREVIEW` (on/off, toggle no painel Logs + Configs), `PREVIEW_INTERVALO`.
- (O `taskview.py` DWM ficou no projeto mas NÃO é mais usado pelo fluxo.)

## Cadência / velocidade de abertura — 3 MODOS (preset único na GUI)
A GUI tem **1 seletor "Modo de abertura"** (não mais campos soltos). Cada modo é um preset
em `PRESETS_ABERTURA` (orquestrador) aplicado por `_aplicar_modo_abertura(modo)`, que seta
internamente `MAX_ABRINDO` (semáforo), `ABRIR_INTERVALO_S`, `STAGGER_START_S`,
`API_MIN_INTERVALO_S` (e `BATCH_*`, hoje 0):
- **turbo**: max_abrindo=50, intervalo=0, stagger=0, api=0.55 → enche no limite da API (~109 RPM).
- **moderado**: max_abrindo=6, intervalo=1.5, stagger=1.0, api=0.7 → meio-termo.
- **conservador**: max_abrindo=3, intervalo=4.0, stagger=2.5, api=1.1 → bem cadenciado, sem cap.
Config salva como `modo_abertura` em settings.json[run]. O gargalo real é sempre a API
(~40 perfis/min); os modos só evitam ficar abaixo (turbo) ou suavizam (conservador).
- `PAUSA_REABRIR_MIN_S`/`MAX_S`: pausa randomizada **após fechar**, antes de reabrir (campo
  próprio, independente do modo). O motor batch (`_gate_batch`) e os globais continuam no
  código (BATCH_SIZE=0 nos presets), mas não há mais campos individuais na GUI.

## Próximos passos (a implementar)
1. **Ação no anúncio além de segurar a sessão** (a definir): mutar? logar métricas? Hoje só
   estende a sessão e loga (`ads_log.jsonl`).
2. **Anti-detecção plena**: portar `mouse_humano.py` do MURI_PRO + usar Patchright de fato.
3. **Proteção de IP / health** se necessário (rotação, re-check) — reaproveitar do MURI_PRO_2.
4. (Opcional) fingerprint fixo-por-conta; pool de proxies maior (1:conta em vez de 1:perfil).
5. **Teste ao vivo** end-to-end (abrir 1 perfil, navegar, esperar cair um midroll).

## Cuidados
- `API_KEY` e auth-tokens = **segredo**. Não commitar valores reais (manter placeholders no git).
- `delete-cache` exige o perfil **fechado** (o fluxo já faz `stop` antes). O delete-cache V1
  (`/api/v1/user/delete-cache`) é **global** (todos os perfis) — usar **só o V2 por perfil**.
