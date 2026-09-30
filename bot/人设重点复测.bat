@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\persona_focus.py 0929m T1,T2,T3,T4,T5,N03 10
pause
