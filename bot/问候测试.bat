@echo off
chcp 65001 >nul
cd /d %~dp0
rem 问候测试：叫她一声、问在不在、问个好时，比较改前 / 改后会不会回“要问什么？”，结果写到 docs\人设回归测试（不启动机器人，约 100 次调用）
.venv\Scripts\python tools\greeting_test.py
pause
