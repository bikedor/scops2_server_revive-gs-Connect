@echo off
rem Tab test ON: the Ranking "my stats" answers get one recognisable number per tab (display only, points still recorded for real).
rem Log in AFTER running this (the game fetches the tabs at login), play one ranked match, read Old Total on Game Statistics.
cd /d "%~dp0"
if not exist "state" mkdir "state"
> "state\ladder_diag.flag" echo tabs
echo Tab test is ON. Now log in on the console, play ONE ranked match and read "Old Total" on the Game Statistics screen.
echo.
echo   Spy 300 / Merc 400   = Overall tab
echo   Spy 1100 / Merc 1200 = Extraction tab
echo   Spy 2100 / Merc 2200 = Sabotage tab
echo   Spy 3100 / Merc 3200 = Neutralisation tab
echo.
echo Run "Tab test OFF.bat" when done.
pause
