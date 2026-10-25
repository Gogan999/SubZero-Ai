@echo off
rem Makes a new recovery code for signing in on this PC when Slack sign-in is broken.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tool.ps1" -Command recovery-code
