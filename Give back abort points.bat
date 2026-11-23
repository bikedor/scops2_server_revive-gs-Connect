@echo off
rem One-time correction: give back the points taken by aborted Ranking matches (the host returned everyone to the
rem lobby, so every player was sent as lost). Shows what it would change first and asks before writing.
rem Writes state\ladder_stats.json (a copy is kept as ladder_stats.json.bak-<date-time>). Run it between sessions.
setlocal
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"
%PY% tools\ranked_undo_aborts.py
echo.
set "OK="
set /p "OK=Write these changes? (Y/N) "
if /i "%OK%"=="Y" %PY% tools\ranked_undo_aborts.py --apply
echo.
pause
