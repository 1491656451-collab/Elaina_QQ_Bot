@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 识图“再确认”测试（10/23，识图 agent）：让 DeepSeek 带着“可能是伊蕾娜”的提示看 20 张她的图和 tools\识图对照图 里的图，各问 2 次
rem 不连 QQ、不碰 data；约 58 次看图调用，空闲价约 1 毛钱。结果在 docs\识图再确认测试\
".venv\Scripts\python" tools\nowmi.py tools\vision_confirm_test.py
pause
