@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\nowmi.py tools\memory_model_compare.py %1
pause
