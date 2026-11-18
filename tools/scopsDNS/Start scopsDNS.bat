@echo off
cd /d "%~dp0"
rem The .vbs starts PowerShell hidden. A .bat cannot: Windows Terminal shows an empty window.
wscript.exe //nologo "%~dp0Start scopsDNS.vbs"
