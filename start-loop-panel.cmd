@echo off
setlocal
set CAS_LOOP_PANEL=1
call "%~dp0start-ui.cmd" --port 8766 %*
endlocal