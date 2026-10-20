@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\persona_focus.py 1021 L1,L2,L3,L4,A01,C01,T5,N09 10
pause
