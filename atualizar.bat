@echo off
REM ============================================================
REM  ATUALIZAR — puxa a ULTIMA versao do projeto do GitHub.
REM  Use nos OUTROS servers (que importaram o projeto).
REM  ATENCAO: sobrescreve os arquivos locais com os do repo
REM  (o GitHub e a fonte da verdade). Mudancas locais sao perdidas.
REM ============================================================
setlocal
cd /d "%~dp0"

set "REPO=https://github.com/xshimmygamer-coder/projectAD.git"

REM se ainda nao for um repo, faz o 1o clone na propria pasta
git rev-parse --is-inside-work-tree >nul 2>nul || (
  git init -b main
  git remote add origin "%REPO%"
)
git remote get-url origin >nul 2>nul || git remote add origin "%REPO%"

echo Isso vai SOBRESCREVER os arquivos locais com a versao do GitHub.
choice /m "Continuar"
if errorlevel 2 (echo Cancelado. & pause & exit /b 0)

echo === Baixando a ultima versao... ===
git fetch origin main
if errorlevel 1 ( echo [ERRO] falha no fetch. & pause & exit /b 1 )
git reset --hard origin/main

echo.
echo ============================================================
echo  Atualizado para a ultima versao do repo.
echo  (Rebuilde o .exe com build_exe.bat se precisar.)
echo ============================================================
pause
