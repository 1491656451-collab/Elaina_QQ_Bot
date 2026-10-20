@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\persona_focus.py 1020c A03,A04,C01,R1,R2,R3 10
pause
