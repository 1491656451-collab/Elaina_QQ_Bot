@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 识图“实物照片”新旧对比测试（10/25，识图 agent）：tools\识图实物照片 里的照片各问 2 次，再拿 29 张插画检查不会被误写成实物
rem 不连 QQ、不碰 data，约 100 次看图调用，空闲价约 7 分钱。结果在 docs\识图实物照片测试\
".venv\Scripts\python" tools\nowmi.py tools\vision_photo_test.py
pause
