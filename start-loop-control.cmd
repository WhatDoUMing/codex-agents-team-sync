@echo off
setlocal
set CAS_LOOP_PANEL=1
set CAS_LOOP_CONTROL=1
call "%~dp0start-ui.cmd" --port 8766 --open-browser %*
endlocal
