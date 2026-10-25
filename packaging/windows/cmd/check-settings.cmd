@echo off
rem Checks config.env, Slack, and the Claude API key.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tool.ps1" -Command check
