@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run_wind_daily.py" %*
set "WIND_EXIT=%ERRORLEVEL%"
pause
exit /b %WIND_EXIT%
