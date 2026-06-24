@echo off
REM ============================================================
REM  ONE-SHOT: gera o .exe E o instalador (pra mandar pros users).
REM  Roda tudo: deps -> PyInstaller -> Inno Setup.
REM ============================================================
setlocal

echo === [1/4] Dependencias ===
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :erro

echo.
echo === [2/4] Limpando build anterior ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo === [3/4] PyInstaller (gera o .exe) ===
pyinstaller --noconfirm twitch_swap.spec
if errorlevel 1 goto :erro
if not exist "dist\MURIADS\MURIADS.exe" goto :erro

echo.
echo === [4/4] Inno Setup (gera o instalador) ===
set "ISCC="
for %%P in (
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist %%P set "ISCC=%%~P"
if not defined ISCC (
  echo [AVISO] Inno Setup nao encontrado — gerando so o ZIP.
  powershell -NoProfile -Command "Compress-Archive -Path 'dist\MURIADS\*' -DestinationPath 'MURIADS_pacote.zip' -Force"
  echo OK: MURIADS_pacote.zip
  goto :fim
)
"%ISCC%" installer.iss
if errorlevel 1 goto :erro

echo.
echo ============================================================
echo  OK! Pacotes prontos pra enviar:
echo    - Output\MURIADS_setup.exe   (instalador)
echo    - (opcional) rode gerar_zip.bat p/ o ZIP portatil
echo ============================================================
:fim
pause
exit /b 0

:erro
echo.
echo *** FALHOU — veja o erro acima. ***
pause
exit /b 1
