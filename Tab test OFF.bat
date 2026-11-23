@echo off
rem Tab test OFF: removes state\ladder_diag.flag so every answer uses the real totals again.
cd /d "%~dp0"
if exist "state\ladder_diag.flag" del "state\ladder_diag.flag"
echo Tab test is OFF. Real totals again (players see them after their next login).
pause
