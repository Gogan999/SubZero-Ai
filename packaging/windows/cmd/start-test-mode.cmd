@echo off
rem Tries SubZero AI without installing it: sample sign-ins and a pretend Claude. Close the window to stop.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\test-mode.ps1"
pause
