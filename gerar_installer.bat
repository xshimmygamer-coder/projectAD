@echo off
REM ============================================================
REM  Gera o INSTALADOR (MURIADS_setup.exe) via Inno Setup.
REM  Pre-requisitos:
REM   1) build_exe.bat ja rodado (existe dist\twitch_swap\)
REM   2) Inno Setup instalado  (https://jrsoftware.org/isdl.php)
REM ============================================================
setlocal
if not exist "dist\MURIADS\MURIADS.exe" (
  echo [ERRO] rode build_exe.bat primeiro.
  pause & exit /b 1
)

set "ISCC="
for %%P in (
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
) do if not defined ISCC if exist %%P set "ISCC=%%~P"
where iscc >nul 2>nul && if not defined ISCC set "ISCC=iscc"

if not defined ISCC (
  echo [ERRO] Inno Setup nao encontrado. Instale: https://jrsoftware.org/isdl.php
  echo (ou abra installer.iss no Inno Setup e clique Compile)
  pause & exit /b 1
)

echo === Compilando instalador com "%ISCC%" ===
"%ISCC%" installer.iss
if errorlevel 1 ( echo [ERRO] falha no Inno Setup. & pause & exit /b 1 )

echo.
echo ============================================================
echo  OK: Output\MURIADS_setup.exe
echo  Mande esse setup. Ele instala por usuario (sem admin).
echo ============================================================
pause
