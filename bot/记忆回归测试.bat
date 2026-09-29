@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\nowmi.py tools\memory_regression.py
pause
