@echo off
REM ============================================================
REM  Build do twitch_swap.exe (PyInstaller + Flet)
REM  Rode na pasta do projeto:  build_exe.bat
REM ============================================================
setlocal
echo === [1/3] Instalando dependencias ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :erro

echo.
echo === [2/3] Limpando build anterior ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo === [3/3] Empacotando (onedir) ===
pyinstaller --noconfirm twitch_swap.spec
if errorlevel 1 goto :erro

echo.
echo ============================================================
echo  OK: dist\MURIADS\MURIADS.exe
echo  Distribua a PASTA inteira dist\MURIADS\ (onedir).
echo  O usuario preenche tudo pela GUI (settings/proxies/tokens
echo  sao criados ao lado do .exe na 1a execucao).
echo ============================================================
pause
exit /b 0

:erro
echo.
echo *** BUILD FALHOU — veja o erro acima. ***
pause
exit /b 1
