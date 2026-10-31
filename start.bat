@echo off
title Ledger
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py
  goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python app.py
  goto done
)

echo.
echo  Ledger needs Python 3.10 or newer, which isn't installed yet.
echo  Opening the download page now. During install, tick "Add python.exe to PATH".
echo  Then double-click start.bat again.
echo.
start https://www.python.org/downloads/windows/
pause
exit /b

:done
if errorlevel 1 (
  echo.
  echo  Ledger stopped with an error. The message above explains what went wrong.
  pause
)
