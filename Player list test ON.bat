@echo off
rem Player list test ON: every ladder answer gets one different number per stat (display only, nothing is recorded),
rem so the "(n)" after each name in the in-game Player List shows which stat the game prints there.
cd /d "%~dp0"
if not exist "state" mkdir "state"
> "state\ladder_diag.flag" echo plist
echo Player list test is ON. Log in on the console AFTER this, join or host a room and open the Player List.
echo.
echo   (7), (8) or (9)  = level (overall / Spy / Mercenary)
echo   (75)             = finished games percentage
echo   (42) or (43)     = ranking position
echo   (20) (11) (14)   = games played
echo   (5)              = disconnections
echo   still (0)        = not from the ranking stats
echo.
echo Other screens (Ranking, Game Statistics) will also show these fake numbers while the test is on.
echo Run "Player list test OFF.bat" when done.
pause
