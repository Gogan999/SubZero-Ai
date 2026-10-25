@echo off
rem Removes SubZero AI. The data folder is kept.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1" %*
