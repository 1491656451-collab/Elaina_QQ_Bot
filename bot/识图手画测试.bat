@echo off
rem 识图“手画小动物”新旧对比测试（11/22，识图 agent）：空间二创 4 张带配文对比、1.jpg 各看 3 次，再加实物照片、复杂图、截图、表情包回归
rem 约 260 次看图调用，约 0.2 元，十几分钟；结果在 docs\识图手画测试\
rem 注释放在 chcp 前面：放后面 cmd 会把中文注释读串行
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python" tools\nowmi.py tools\vision_sketch_test.py
pause
