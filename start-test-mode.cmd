@echo off
rem Try SubZero AI on this PC with sample sign-ins and a pretend Claude. Close this window to stop it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" -TestMode
pause
