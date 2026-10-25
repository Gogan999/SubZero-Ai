@echo off
rem Backs up the chats, files, and prompts now.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tool.ps1" -Command backup
