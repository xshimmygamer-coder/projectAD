@echo off
REM ============================================================
REM  Gera o PACOTE .zip portatil pra mandar pros amigos.
REM  (rode build_exe.bat ANTES, p/ existir dist\twitch_swap\)
REM ============================================================
setlocal
if not exist "dist\MURIADS\MURIADS.exe" (
  echo [ERRO] dist\MURIADS\MURIADS.exe nao existe.
  echo Rode build_exe.bat primeiro.
  pause
  exit /b 1
)

set "SAIDA=MURIADS_pacote.zip"
if exist "%SAIDA%" del /q "%SAIDA%"

echo === Compactando dist\MURIADS\ para %SAIDA% ===
powershell -NoProfile -Command "Compress-Archive -Path 'dist\MURIADS\*' -DestinationPath '%SAIDA%' -Force"
if errorlevel 1 (
  echo [ERRO] falha ao compactar.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  OK: %SAIDA%
echo  Mande esse .zip. O amigo EXTRAI numa pasta normal
echo  (Desktop/Downloads) e roda twitch_swap.exe de dentro dela.
echo  (NAO extrair dentro de Program Files.)
echo ============================================================
pause
