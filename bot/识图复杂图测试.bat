@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 识图描述新旧对比测试（11/05，识图 agent）：复杂的图放宽到 100 字、不替发图的人猜用意
rem tools\识图复杂图 里的图各问 2 次，再拿 29 张简单的图检查没有跟着变长。不连 QQ、不碰 data，约 100 次看图调用，空闲价约 1 毛钱。结果在 docs\识图复杂图测试\
".venv\Scripts\python" tools\nowmi.py tools\vision_long_test.py
pause
