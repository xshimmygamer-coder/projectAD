@echo off
REM ============================================================
REM  PUBLICAR — sobe o projeto pro GitHub (commit + push).
REM  Use na maquina DEV (onde voce edita o projeto).
REM  Uso:  publicar.bat  "mensagem opcional do commit"
REM ============================================================
setlocal
cd /d "%~dp0"

set "REPO=https://github.com/xshimmygamer-coder/projectAD.git"

REM identidade local (email noreply evita o erro GH007 de privacidade)
git rev-parse --is-inside-work-tree >nul 2>nul || git init -b main
git config user.email "xshimmygamer-coder@users.noreply.github.com"
git config user.name "xshimmygamer-coder"

REM garante o remote 'origin'
git remote get-url origin >nul 2>nul || git remote add origin "%REPO%"

REM mensagem do commit: argumento, senao data/hora
set "MSG=%~1"
if "%MSG%"=="" set "MSG=update %DATE% %TIME%"

echo === Adicionando arquivos... ===
git add -A
echo === Commitando: %MSG% ===
git commit -m "%MSG%"
echo === Enviando pro GitHub... ===
git push -u origin main

echo.
echo ============================================================
echo  Pronto. (Se disse "nothing to commit", nao havia mudancas.)
echo  Repo: %REPO%
echo ============================================================
pause
