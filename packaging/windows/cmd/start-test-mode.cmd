@echo off
rem Tries SubZero AI without installing it: sample sign-ins and a pretend Claude. Close the window to stop.
if exist "%~dp0scripts\test-mode.ps1" goto run
echo.
echo This has to run from the unzipped folder, not from inside the zip.
echo Right-click SubZeroAI-windows.zip, choose Extract All, then double-click
echo start-test-mode.cmd in the folder that Windows makes.
echo.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\test-mode.ps1"
pause
