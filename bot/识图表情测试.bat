@echo off
rem 识图“表情看准”新旧对比测试（11/17，识图 agent）
rem 约 240 次看图调用，约 0.2 元，10 分钟左右；结果在 docs\识图表情测试\
rem 注释放在 chcp 前面：放后面 cmd 会把中文注释读串行
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python" tools\nowmi.py tools\vision_face_test.py
pause
