@echo off
chcp 65001 >nul
cd /d %~dp0
rem 温度对比：改前改后都用现在的代码和人设，只有温度不同（改前 1.1 = 现在线上，改后 1.0），看个性会不会变淡、跑偏会不会变少
rem 想比别的温度：温度对比.bat 0.9
set "REG_TEMP_BEFORE=1.1"
set "REG_TEMP_AFTER=1.0"
if not "%~1"=="" set "REG_TEMP_AFTER=%~1"
.venv\Scripts\python tools\regression.py 当前
pause
