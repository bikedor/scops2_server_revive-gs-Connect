@echo off
rem SCOPS UPnP check - opens a window that checks whether your router supports UPnP and, if it does,
rem opens the Splinter Cell Chaos Theory host ports (UDP 3658, 10070-10080) to your PS2 / PCSX2 while it stays open.
rem Keep SCOPS_UPnP_Check.ps1 in the same folder as this file.
start "" powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0SCOPS_UPnP_Check.ps1"
