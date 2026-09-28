@echo off
chcp 65001 >nul
cd /d %~dp0
rem 人设回归测试：同一批问题分别问旧版和新版人设，结果写到 docs\人设回归测试\（不启动机器人，只调 DeepSeek，约 180 次调用）
.venv\Scripts\python tools\persona_regression.py
pause
