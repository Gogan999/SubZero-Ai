@echo off
rem Installs SubZero AI on this PC. Windows asks for administrator permission.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
