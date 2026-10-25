@echo off
rem Installs SubZero AI on this PC. Windows asks for administrator permission.
if exist "%~dp0scripts\install.ps1" goto run
echo.
echo This has to run from the unzipped folder, not from inside the zip.
echo Right-click SubZeroAI-windows.zip, choose Extract All, then double-click
echo install.cmd in the folder that Windows makes.
echo.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
