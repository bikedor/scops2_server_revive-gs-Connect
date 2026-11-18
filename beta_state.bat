@echo off
setlocal
cd /d "%~dp0"
rem Copies the server data (state\: accounts, friends, ladder, MOTD, ranked mode) between the main server and the beta.
rem   beta_state.bat import   main -> beta
rem   beta_state.bat export   beta -> main
rem The folder being overwritten is backed up first to state_backups\<date-time>-<beta|main>\.
rem Runtime files (lobby_rooms.json, relay_map.json) are not copied. Run it only while no server is running.

set "MODE=%~1"
set "NOPAUSE=%~2"
if exist "beta_config.bat" call "beta_config.bat"
if not defined MAIN_DIR (
  echo No main server folder set. Run "BETA Server.bat" once, or create beta_config.bat with:
  echo   set "MAIN_DIR=C:\path\to\main\server"
  goto fail
)
if not exist "%MAIN_DIR%\SCOPS Server.bat" (
  echo Main server folder not found: %MAIN_DIR%
  goto fail
)

if /i "%MODE%"=="import" (
  set "SRC=%MAIN_DIR%\state"
  set "DST=%~dp0state"
  set "DSTNAME=beta"
  goto check
)
if /i "%MODE%"=="export" (
  set "SRC=%~dp0state"
  set "DST=%MAIN_DIR%\state"
  set "DSTNAME=main"
  goto check
)
echo Usage:
echo   beta_state.bat import    copy accounts / friends / ladder from the main server into the beta
echo   beta_state.bat export    copy them from the beta back to the main server
goto fail

:check
if not exist "%SRC%\" (
  echo Nothing to copy: %SRC% does not exist.
  goto fail
)
powershell -NoProfile -Command "if(Get-NetTCPConnection -State Listen -LocalPort 40005 -ErrorAction SilentlyContinue){exit 0}else{exit 1}"
if errorlevel 1 goto copy
echo A SCOPS server is running. Stop it first: a running server can overwrite the copied files.
goto fail

:copy
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "TS=%%t"
if exist "%DST%\" (
  robocopy "%DST%" "%~dp0state_backups\%TS%-%DSTNAME%" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 (
    echo Backup failed, nothing copied.
    goto fail
  )
  echo Backup of the %DSTNAME% data: state_backups\%TS%-%DSTNAME%
)
robocopy "%SRC%" "%DST%" /E /R:1 /W:1 /XF lobby_rooms.json relay_map.json /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo Copy failed. Restore from state_backups if needed.
  goto fail
)
echo Copied %SRC%
echo     to %DST%
if not exist "%DST%\users.json" goto done
for /f %%n in ('powershell -NoProfile -Command "try{ $j=Get-Content -Raw -LiteralPath '%DST%\users.json' | ConvertFrom-Json; @($j.users.PSObject.Properties).Count }catch{ '?' }"') do echo Accounts in users.json: %%n
:done
if /i not "%NOPAUSE%"=="nopause" pause
exit /b 0

:fail
if /i not "%NOPAUSE%"=="nopause" pause
exit /b 1
