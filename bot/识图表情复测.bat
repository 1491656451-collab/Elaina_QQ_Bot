@echo off
rem 识图表情复测（11/17，识图 agent）：几张重点图只跑新版、每张看 3 次，看描述稳不稳
rem 要换图就改下面 --only 后面的编号（编号见 docs\识图表情测试\表情_*.md）；结果在 docs\识图表情测试\复测_*.md
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python" tools\nowmi.py tools\vision_face_test.py --only K3,K4,G5,G9,G14,G20,E6,E8,E13,E15,E33,E46 --repeat 3
pause
