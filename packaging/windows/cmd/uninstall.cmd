@echo off
rem Removes SubZero AI. The data folder is kept.
if exist "%~dp0scripts\uninstall.ps1" goto run
echo.
echo This has to run from the unzipped folder, not from inside the zip.
echo Right-click SubZeroAI-windows.zip, choose Extract All, then double-click
echo uninstall.cmd in the folder that Windows makes.
echo.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1" %*
