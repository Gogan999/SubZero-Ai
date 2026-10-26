@echo off
rem Updates SubZero AI to the newest release, keeping all data.
if exist "%~dp0scripts\update.ps1" goto run
echo.
echo This has to run from the unzipped folder, not from inside the zip.
echo Right-click SubZeroAI-windows.zip, choose Extract All, then double-click
echo update.cmd in the folder that Windows makes.
echo.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1" %*
