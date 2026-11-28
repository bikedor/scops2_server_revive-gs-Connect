@echo off
rem One-time correction: give back points a loss should have cost but did not (SCCT_RANKED_MODE_FLOOR bug,
rem 2026-11-25 - an established player losing in a mode and side they had not lost in before, or had few
rem points in, kept some or all of the points everywhere, not just on that one Ranking tab).
rem Shows what it would change first and asks before writing.
rem Writes state\ladder_stats.json (a copy is kept as ladder_stats.json.bak-<date-time>). Run it between sessions.
setlocal
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"
%PY% tools\ranked_fix_mode_floor.py
echo.
set "OK="
set /p "OK=Write these changes? (Y/N) "
if /i "%OK%"=="Y" %PY% tools\ranked_fix_mode_floor.py --apply
echo.
pause
