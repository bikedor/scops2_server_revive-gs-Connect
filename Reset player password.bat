@echo off
rem Reset a player's password by hand, for when they've forgotten it and can't log in to change it
rem themselves. Passwords are hashed, so there is no way to look up or show the old one - this only
rem sets a new one. Asks for the username, shows what's on file for it, asks for the new password
rem twice and confirms before writing. Does not create a new account.
setlocal
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"
%PY% tools\reset_player_password.py
echo.
pause
