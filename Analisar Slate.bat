@echo off
REM Roda a analise do ciclos_log.txt (SLATE = culpa do proxy ou da conta?).
REM Execute DEPOIS de finalizar a RUN.
cd /d "%~dp0"
python analisar_log.py %*
echo.
pause
