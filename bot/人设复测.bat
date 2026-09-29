@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\regression.py 0929g
.venv\Scripts\python tools\persona_focus.py 0929g N08,N03,N09,G10 10
pause
