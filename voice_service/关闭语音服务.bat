@echo off
chcp 65001 >nul
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0voice_ctl.ps1" stop
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0voice_ctl.ps1" stop
)
pause
