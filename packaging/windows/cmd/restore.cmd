@echo off
rem Puts a backup back.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restore.ps1" %*
