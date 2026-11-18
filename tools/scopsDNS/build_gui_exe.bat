@echo off
rem Builds SCOPS.exe: double-click it and the window opens. No command prompt.
rem Run once on a Windows PC with Python 3. Keep SCOPS_UPnP_Check.ps1 in this folder.
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller || goto fail
py -3 -m PyInstaller --onefile --noconsole --name SCOPS SCOPS_launcher.py || goto fail
copy /y dist\SCOPS.exe . >nul
echo.
echo Done: SCOPS.exe
echo Double-click SCOPS.exe for the window. It starts DNS itself.
goto end
:fail
echo Build failed.
:end
pause
