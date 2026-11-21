@echo off
rem Runs the built-in DNS check (scopsDNS.ps1, no Python needed) and shows the result here.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scopsDNS.ps1" -CheckOnly -NoFirewall -NoUpnp
echo.
pause
