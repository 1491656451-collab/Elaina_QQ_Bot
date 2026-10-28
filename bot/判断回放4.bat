@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 判断回放第四轮（10/28，聊天 agent，《判断回放报告-1028_1805》）：判断模型换成 pro，跑 A（现在的写法）和 B（加两条规则）
rem pro 用 .env 的 MEMORY_PRO_API_KEY、MEMORY_PRO_MODEL（没填就用 DEEPSEEK_API_KEY、deepseek-v4-pro）
rem 同样 35 道题，每种 3 次，约 210 次短调用，几分钱
set "JUDGE_PRO=1"
set "JUDGE_VARIANTS=A,B"
".venv\Scripts\python" tools\judge_compare.py
echo.
echo 跑完了，告诉我一声就行
pause
