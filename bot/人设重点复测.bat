@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\persona_focus.py 1105b U1,U2,U3,U4,P3,P4,M01 10
pause
