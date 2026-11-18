@echo off
title Update SCOPS beta
cd /d "%~dp0"
rem Pulls the latest beta from your GitHub repo. Your data (state\, logs\, beta_config.bat, .venv) is not touched.
where git >nul 2>&1
if errorlevel 1 (
  echo Git is not installed. Get "Git for Windows" from git-scm.com, then run this again.
  pause
  exit /b 1
)
if not exist ".git\" (
  echo This folder is not a git clone. See BETA.md, "Put it on the VPS".
  pause
  exit /b 1
)
powershell -NoProfile -Command "if(Get-NetTCPConnection -State Listen -LocalPort 40005 -ErrorAction SilentlyContinue){exit 0}else{exit 1}"
if not errorlevel 1 echo Note: a server is running. The update applies the next time it starts.
git pull --ff-only
if errorlevel 1 (
  echo.
  echo The update did not apply. If you edited files here on the VPS, "git status" shows which ones.
  pause
  exit /b 1
)
echo.
git log -1 --format="Now at: %%h %%s (%%cd)" --date=short
pause
