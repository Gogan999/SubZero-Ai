@echo off
rem Puts the Code tools into the sandbox base disk (run once, after the plain base disk is built).
if exist "%~dp0scripts\prepare-sandbox-image.ps1" goto run
echo.
echo This has to run from the unzipped folder, not from inside the zip.
echo Right-click SubZeroAI-windows.zip, choose Extract All, then double-click
echo prepare-sandbox-image.cmd in the folder that Windows makes.
echo.
pause
exit /b 1
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare-sandbox-image.ps1" %*
