@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0finboard.ps1" %*
exit /b %errorlevel%
