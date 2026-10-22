@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\nowmi.py tools\affection_prompt_compare.py %1
pause
