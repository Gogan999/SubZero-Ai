@echo off
rem Start SubZero AI with the settings in C:\ProgramData\SubZeroAI\config.env. Close this window to stop it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
pause
