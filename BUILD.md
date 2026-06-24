# Empacotar o twitch_swap em .exe

## Build (na sua máquina dev)
```
build_exe.bat
```
Isso instala as deps + PyInstaller e gera **`dist\twitch_swap\twitch_swap.exe`** (modo
*onedir* — uma pasta com o exe + DLLs/assets; mais robusto que onefile pra Flet+Playwright).

> Distribua a **pasta inteira** `dist\twitch_swap\` (zip). O amigo roda o `.exe` dela.

## O que vai dentro / o que NÃO vai
- **Vai:** o código (compilado em bytecode), Flet (cliente desktop), Playwright (driver),
  pywin32, e `assets/banner.png`.
- **NÃO vai (segredo/runtime):** `settings.json`, `tokens.txt`, `proxies_pool.txt`,
  `contas_pool.json`, `ciclos_log.txt`, `ads_log.jsonl`. Esses são criados **ao lado do .exe**
  na 1ª execução, preenchidos pela GUI. Assim nada de chave/conta vaza no pacote.

## Gerar o PACOTE pra mandar (2 formas)
O `build_exe.bat` só gera a **pasta** `dist\twitch_swap\`. Pra enviar, empacote:

**A) ZIP portátil (mais simples, recomendado)**
```
gerar_zip.bat        ->  twitch_swap_pacote.zip
```
O amigo **extrai numa pasta normal** (Desktop/Downloads) e roda `twitch_swap.exe`. Sem
instalação, sem admin. ⚠️ NÃO extrair dentro de `Program Files` (o app precisa gravar
`settings.json`/tokens/proxies ao lado do .exe).

**B) Instalador Setup.exe (Inno Setup)**
```
gerar_installer.bat  ->  Output\twitch_swap_setup.exe
```
Precisa do **Inno Setup** instalado (https://jrsoftware.org/isdl.php). Instala **por usuário**
em `%LOCALAPPDATA%\twitch_swap` (sem admin) — local gravável, então settings/tokens/proxies
funcionam. Cria atalho no menu/área de trabalho.

> Fluxo completo: `build_exe.bat` → (`gerar_zip.bat` **ou** `gerar_installer.bat`) → enviar.

## Requisitos na máquina do amigo
- **AdsPower instalado e aberto** (Local API ligada em `local.adspower.net:50325`).
- Windows (TaskView usa DWM). NÃO precisa instalar navegadores do Playwright — o app
  **conecta** ao navegador do AdsPower (`connect_over_cdp`), não baixa Chromium.

## Como usar (amigo)
1. Abrir `twitch_swap.exe`.
2. Aba **APIs**: colar a API key do AdsPower → Salvar.
3. Abas **Proxy** e **Tokens**: colar e OK.
4. Aba **Configs**: canais + tempos + nº de perfis → Salvar.
5. Aba **Logs ao vivo** → **Iniciar RUN**.

## Notas / ajustes possíveis
- **Ícone:** ponha um `assets/icone.ico` e troque `icon=None` no `twitch_swap.spec`.
- **TaskView no .exe:** o exe se relança com `--taskview` (o `gui.py` roteia isso); por
  isso o TaskView funciona empacotado, sem precisar de `python`.
- **Patchright:** se quiser usá-lo (anti-detecção), `pip install patchright` antes do build
  (o spec já tenta incluí-lo; o app prefere patchright e cai pra playwright).
- **Proteção de fonte (opcional):** o bytecode do PyInstaller é descompilável. Pra dificultar,
  use **pyarmor** antes do PyInstaller (`pyarmor gen ...`) — fica como passo extra futuro.
- Se o exe reclamar de módulo Playwright/Flet faltando, adicione-o em `hiddenimports` no
  `.spec` e rebuilde.
