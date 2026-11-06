@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 识图“截图里认出她自己”新旧对比测试（11/06，识图 agent）：tools\识图截图 里的截图各问 2 次，再拿表情、插画、复杂图做回归
rem 不连 QQ、不碰 data，约 110 次看图调用，空闲价约 1 毛钱。结果在 docs\识图截图测试\
".venv\Scripts\python" tools\nowmi.py tools\vision_shot_test.py
pause
