@echo off
chcp 65001 >nul
cd /d %~dp0
.venv\Scripts\python tools\persona_focus.py 1105g S02,M01,S19,A01,Q1,Q2 15
pause
