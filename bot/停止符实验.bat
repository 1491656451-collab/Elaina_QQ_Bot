@echo off
chcp 65001 >nul
cd /d %~dp0
rem 停止符实验：同一道题一半开着“写到【就停”、一半不开，看她原本想写什么、会不会被截成半截字
rem 默认 G11、G5、S01、G7 四题各 10 次 × 2 组，约 80 次调用，几分钱；也可以指定：停止符实验.bat G11 20
.venv\Scripts\python tools\stop_test.py %1 %2
pause
