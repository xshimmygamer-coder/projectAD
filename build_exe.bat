@echo off
REM ============================================================
REM  Build do twitch_swap.exe (PyInstaller + Flet)
REM  Rode na pasta do projeto:  build_exe.bat
REM ============================================================
setlocal
echo === [1/4] Visual C++ Redistributable (x64) ===
REM O _greenlet.pyd do Playwright DEPENDE do msvcp140.dll (runtime C++ do MSVC). O Python
REM NAO traz esse DLL; sem ele a RUN falha com "DLL load failed while importing _greenlet"
REM e o proprio PyInstaller nao tem de onde copia-lo p/ o bundle. Instala o redist se faltar.
if exist "%SystemRoot%\System32\msvcp140.dll" (
    echo msvcp140.dll ja presente — pulando.
) else (
    echo Baixando e instalando o VC++ Redistributable x64...
    curl -L -o "%TEMP%\vc_redist.x64.exe" https://aka.ms/vs/17/release/vc_redist.x64.exe
    if errorlevel 1 goto :erro
    "%TEMP%\vc_redist.x64.exe" /install /quiet /norestart
    REM exit codes do instalador: 0=ok, 3010=ok mas pede reboot. Qualquer outro = erro.
    if errorlevel 3011 goto :erro
    if not exist "%SystemRoot%\System32\msvcp140.dll" (
        echo *** Falhou a instalacao do VC++ Redistributable. ***
        goto :erro
    )
)

echo.
echo === [2/4] Instalando dependencias Python ===
python -m pip install --upgrade pip
REM Flet 0.28.3: 'flet' sozinho e SO o core. O cliente de janela desktop (flet-desktop) e
REM o modo web (flet-web) sao EXTRAS opcionais. Sem eles o .exe sobe mas a janela NAO abre.
REM Instala explicitamente com extras (idempotente; requirements.txt tambem ja pede flet[all]).
python -m pip install "flet[all]==0.28.3"
if errorlevel 1 goto :erro
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :erro

echo.
echo === [3/4] Limpando build anterior ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo === [4/4] Empacotando (onedir) ===
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
