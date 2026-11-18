@echo off
title SCOPS BETA launcher
cd /d "%~dp0"

rem Turn-key start for the SCOPS beta on the same VPS as the main server.
rem   1. stops the main server if it is running (they share ports)
rem   2. sets up Python (bundled pylib, plus the main server's .venv if it has one)
rem   3. optionally copies accounts / friends / ladder from the main server
rem   4. runs SCOPS Server.bat from this folder
rem   5. after you stop it: optionally copies the data back and restarts the main server

net session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ==================================================
echo   SCOPS BETA  -  test server from this folder
echo   %~dp0
echo ==================================================
echo.

rem ---------- 1. where is the main server ----------
if exist "beta_config.bat" call "beta_config.bat"
if defined MAIN_DIR if exist "%MAIN_DIR%\SCOPS Server.bat" goto have_main
echo Where is the MAIN server folder on this VPS?
echo Example: C:\scops2_server_revive
echo Leave empty if there is no main server on this machine.
set "MAIN_DIR="
set /p "MAIN_DIR=Main server folder: "
if not defined MAIN_DIR goto no_main
if "%MAIN_DIR:~-1%"=="\" set "MAIN_DIR=%MAIN_DIR:~0,-1%"
if not exist "%MAIN_DIR%\SCOPS Server.bat" (
  echo That folder has no "SCOPS Server.bat". Run this again and check the path.
  pause
  exit /b 1
)
if /i "%MAIN_DIR%\"=="%~dp0" (
  echo That is this beta folder. Give the folder of the main server.
  pause
  exit /b 1
)
> "beta_config.bat" echo set "MAIN_DIR=%MAIN_DIR%"
echo Saved in beta_config.bat. Delete that file to choose again.
goto have_main

:no_main
echo No main server: skipping the stop, copy and restart steps.
goto python

:have_main
echo Main server folder: %MAIN_DIR%

rem ---------- 2. stop whatever SCOPS server is running ----------
call :is_running
if errorlevel 1 goto python
echo.
echo A SCOPS server is running now (port 40005 is in use), probably the main one.
echo Both use the same ports, so it has to stop while the beta runs.
choice /c YN /n /m "Stop it now? [Y/N]: "
if errorlevel 2 (
  echo Left it running. The beta cannot start while it runs.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0beta_stop_running.ps1"
if errorlevel 1 (
  echo Port 40005 is still in use. Close the main server window and run this again.
  pause
  exit /b 1
)
echo Main server stopped.

rem ---------- 3. Python ----------
:python
echo.
if exist ".venv\Scripts\python.exe" goto py_venv
if not defined MAIN_DIR goto py_system
if not exist "%MAIN_DIR%\.venv\Scripts\python.exe" goto py_system
if exist ".venv" rd ".venv" 2>nul
mklink /J ".venv" "%MAIN_DIR%\.venv" >nul
if exist ".venv\Scripts\python.exe" (
  echo Using the main server's Python environment, linked, nothing copied.
  goto py_venv
)
:py_system
set "PY=python"
where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is not installed or not on PATH.
  echo Install Python 3 from python.org with "Add python.exe to PATH" ticked, then run this again.
  pause
  exit /b 1
)
goto py_check
:py_venv
set "PY=%~dp0.venv\Scripts\python.exe"
:py_check
rem pylib holds rsa, pyasn1 and dnslib, so no pip install is needed.
set "PYTHONPATH=%~dp0pylib"
"%PY%" -c "import rsa, pyasn1, dnslib" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python could not load rsa / pyasn1 / dnslib from pylib.
  "%PY%" -c "import rsa, pyasn1, dnslib"
  pause
  exit /b 1
)
"%PY%" --version
echo Python modules OK.

rem ---------- 4. data from the main server ----------
if not defined MAIN_DIR goto run
echo.
set "IMPDEF=N"
if not exist "state\users.json" set "IMPDEF=Y"
echo Copy accounts, friends and ladder FROM the main server into the beta?
echo The beta's current data is backed up first. Default %IMPDEF% in 15 s.
choice /c YN /n /t 15 /d %IMPDEF% /m "Copy main -> beta? [Y/N]: "
if errorlevel 2 goto run
call "%~dp0beta_state.bat" import nopause

rem ---------- 5. run ----------
:run
if not exist "state\" mkdir state
if not exist "logs\" mkdir logs
echo.
echo Starting the BETA server...
echo.
rem Run it in a child cmd: its cleanup watcher then watches that child, not this window,
rem so it cannot stop the main server after we restart it below.
cmd /c ""%~dp0SCOPS Server.bat""
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0beta_stop_running.ps1" -WaitWatchers

if not defined MAIN_DIR goto end
echo.
echo ==================================================
echo   BETA stopped.
echo ==================================================
echo Copy accounts, friends and ladder from the beta BACK to the main server?
echo The main server's current data is backed up first. Default N in 20 s.
choice /c YN /n /t 20 /d N /m "Copy beta -> main? [Y/N]: "
if errorlevel 2 goto restart
call "%~dp0beta_state.bat" export nopause

:restart
echo.
choice /c YN /n /t 20 /d Y /m "Start the MAIN server again? [Y/N] (Y in 20 s): "
if errorlevel 2 goto end
start "" "%MAIN_DIR%\SCOPS Server.bat"
echo Main server is starting in its own window.

:end
echo.
echo Done.
timeout /t 5 >nul
exit /b 0

rem ---------- helpers ----------
:is_running
powershell -NoProfile -Command "if(Get-NetTCPConnection -State Listen -LocalPort 40005 -ErrorAction SilentlyContinue){exit 0}else{exit 1}"
exit /b %errorlevel%
