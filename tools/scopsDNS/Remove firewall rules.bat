@echo off
cd /d "%~dp0"
rem Removes the firewall rules scopsDNS and the SCOPS window added (DNS 53, PCSX2 ports, router test 3659).
rem Windows asks for permission once.
where py >nul 2>nul && (py -3 scopsDNS.py --close-firewall & goto end)
where python >nul 2>nul && (python scopsDNS.py --close-firewall & goto end)
if exist scopsDNS.exe (scopsDNS.exe --close-firewall & goto end)
echo Python or scopsDNS.exe was not found in this folder.
:end
pause
