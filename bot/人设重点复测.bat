@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\persona_focus.py 0929l S02,S03,M01,S15,A01 10
pause
