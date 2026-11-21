@echo off
cd /d "%~dp0"
rem Removes the firewall rules scopsDNS and the SCOPS window added (DNS 53, PCSX2 ports, router test 3659).
rem Windows asks for permission once.
rem scopsDNS.exe first: on a PC without Python, "where python" finds the Windows Store placeholder and stops there.
if exist scopsDNS.exe (scopsDNS.exe --close-firewall && goto end)
where py >nul 2>nul && (py -3 scopsDNS.py --close-firewall && goto end)
where python >nul 2>nul && (python scopsDNS.py --close-firewall && goto end)
rem No working Python or exe: remove the rules directly (one administrator prompt).
powershell.exe -NoProfile -Command "Start-Process cmd.exe -Verb RunAs -Wait -ArgumentList '/c netsh advfirewall firewall delete rule name=scopsDNS-DNS-UDP & netsh advfirewall firewall delete rule name=scopsDNS-DNS-TCP & netsh advfirewall firewall delete rule name=scopsDNS-PCSX2-game-UDP & netsh advfirewall firewall delete rule name=scopsDNS-router-test-UDP'"
echo Firewall rules removed (if Windows asked and you said yes).
:end
pause
