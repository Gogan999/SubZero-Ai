@echo off
rem Updates SubZero AI to the newest release, keeping all data.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1" %*
