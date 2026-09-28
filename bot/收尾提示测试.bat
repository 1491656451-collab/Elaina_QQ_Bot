@echo off
chcp 65001 >nul
cd /d %~dp0
rem 收尾提示测试：同一段对话分别用改前、改后的收尾提示问 DeepSeek，结果写到 docs\人设回归测试\（不启动机器人，约 48 次调用）
.venv\Scripts\python tools\wrapup_test.py
pause
