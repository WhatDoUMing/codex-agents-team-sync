@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo Local Team Sync - real Codex support, loopback only
echo Open the localhost URL and paste the session key shown below.
echo Press Ctrl+C here to stop. Existing data is preserved.
python -B "%~dp0start_owned_codex.py" %*
if errorlevel 1 (
  echo.
  echo Startup failed. Check the message above, Python, and port availability.
  pause
  exit /b 1
)
endlocal
