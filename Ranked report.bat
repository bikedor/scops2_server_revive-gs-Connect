@echo off
rem Ranked match report: reads logs\router_wm_40005.log (never changes the server or state\), writes logs\ranked_report.txt
rem and opens it in Notepad. Safe to run while the server is up.
setlocal
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"
echo Ranked match report
echo.
echo   Return            = only since the last server start
echo   a date            = from that date on, e.g. 2026-11-22  or  "2026-11-22 18:00"
echo   all               = the whole log
echo.
set "WHEN="
set /p "WHEN=Which matches? "
if /i "%WHEN%"=="all" (
  %PY% tools\ranked_report.py --scored
) else if "%WHEN%"=="" (
  %PY% tools\ranked_report.py --last --scored
) else (
  %PY% tools\ranked_report.py --scored --since "%WHEN:"=%"
)
if exist "logs\ranked_report.txt" start "" notepad "logs\ranked_report.txt"
echo.
pause
