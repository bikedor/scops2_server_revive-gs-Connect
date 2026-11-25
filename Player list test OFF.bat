@echo off
rem Player list test OFF: removes state\ladder_diag.flag so every answer uses the real stats again.
cd /d "%~dp0"
if exist "state\ladder_diag.flag" del "state\ladder_diag.flag"
echo Player list test is OFF. Real stats again (players see them after their next login).
pause
