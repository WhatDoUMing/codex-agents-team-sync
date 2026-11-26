@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo Local Team Sync - loopback only
echo If a browser tab does not open automatically, use the URL and fallback key below.
echo Press Ctrl+C here to stop the server. Existing data is preserved.
python -B "%~dp0sync.py" serve %*
if errorlevel 1 (
  echo.
  echo Startup failed. Check Python and port availability above.
  pause
)
endlocal
