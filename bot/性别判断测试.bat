@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 性别判断测试（10/23，好感 agent）：对方是不是在说自己的性别，现在问模型判断；看它准不准、偏不偏向男或女
rem 50 句男女成对的话，每句问 3 次，约 150 次很短的调用，不到 1 分钱。不连 QQ、不碰 data。结果在 docs\性别判断测试\
".venv\Scripts\python" tools\nowmi.py tools\gender_judge_test.py
pause
