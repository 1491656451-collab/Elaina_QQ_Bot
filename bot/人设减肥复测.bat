@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 人设减肥复测（10/22）：只跑“说岔了被追问”(N08) 和“讲往事被反问”(N11)，改前 1022 对改后，每版 10 次，约 40 次调用，几分钱
rem 结果：docs\人设回归测试\回归_时间.md
set "REG_ONLY=N08,N11"
set "REG_SAMPLES=10"
".venv\Scripts\python" tools\regression.py 1022
set "REG_ONLY="
set "REG_SAMPLES="
pause
