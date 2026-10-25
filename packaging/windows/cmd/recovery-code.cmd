@echo off
rem Makes a new recovery code for signing in on this PC when Slack sign-in is broken.
if exist "%~dp0scripts\tool.ps1" goto run
echo.
echo This has to run from the unzipped folder, not from inside the zip.
echo Right-click SubZeroAI-windows.zip, choose Extract All, then double-click
echo recovery-code.cmd in the folder that Windows makes.
echo.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tool.ps1" -Command recovery-code
