@echo off
rem Check config.env, Slack, and the Claude API key.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" -Check
pause
