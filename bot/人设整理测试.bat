@echo off
chcp 65001 >nul
cd /d %~dp0
rem 人设整理测试：改前 = 改人设之前（0929d：旧人设 + 新规则），改后 = 现在的人设 + 新规则，只看人设带来的变化
rem 先跑整套回归（约 350 次调用），再对几道重点题各多问 10 次（约 180 次调用），一共大约一块钱
.venv\Scripts\python tools\regression.py 0929d
.venv\Scripts\python tools\persona_focus.py 0929d
pause
