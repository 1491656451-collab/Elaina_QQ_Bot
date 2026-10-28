@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 判断回放第三轮（10/28，聊天 agent，《判断回放报告-1028_1755》）：
rem   F = C（加两条规则、去掉“拿不准就答否”）再加一条“只是冲着她的话起哄、感叹一句（笑死、哈哈哈……）不用她接”
rem   第二轮 C 多认出了 5 条该回的，但把“绷不住了”这种起哄也判成了“是”，这轮看加了这条能不能挡住
rem 同样 35 道题，问 3 次，约 105 次短调用，不到一分钱
set "JUDGE_VARIANTS=F"
".venv\Scripts\python" tools\judge_compare.py
echo.
echo 跑完了，告诉我一声就行
pause
