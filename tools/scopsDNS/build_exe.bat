@echo off
rem Optional: build scopsDNS.exe so players do not need Python.
rem Run once on a Windows PC that has Python 3. Keep scopsDNS.py in this folder.
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller || goto fail
py -3 -m PyInstaller --onefile --console --name scopsDNS scopsDNS.py || goto fail
copy /y dist\scopsDNS.exe . >nul
echo.
echo Done: scopsDNS.exe
echo Start scopsDNS.bat will use the exe and players will not need Python.
goto end
:fail
echo Build failed.
:end
pause
