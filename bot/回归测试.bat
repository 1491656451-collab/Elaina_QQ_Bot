@echo off
chcp 65001 >nul
cd /d %~dp0
rem 回归测试：人设、问候、收尾、日志翻车题一起跑，比较改前 / 改后，结果写到 docs\人设回归测试\回归_时间.md（不启动机器人，约 350 次调用）
.venv\Scripts\python tools\regression.py %1
pause
