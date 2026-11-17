@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 识图“动图看 4 帧”新旧对比测试（11/17，识图 agent）：表情库和对照图里的动图，只看第一帧 / 看 4 帧各描述一次
rem 不连 QQ、不碰 data，约 80 次看图调用，空闲价约 6 分钱。结果在 docs\识图动图测试\
".venv\Scripts\python" tools\nowmi.py tools\vision_gif_test.py
pause
