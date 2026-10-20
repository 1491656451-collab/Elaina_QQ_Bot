@echo off
chcp 65001 >nul
cd /d %~dp0
rem 括号规则测试（10/20）：聊天规则加了“要跟对方说的话别包在括号里，括号只写心里话”，比改前（1020p）和改后
rem 跑回归测试里除了传声题（V）以外的 68 道题，每题每版 3 次，约 400 次调用、五毛钱左右
rem 结果写到 docs\人设回归测试\回归_时间.md 和 .json，跑完把 .md 发给聊天 agent
set REG_ONLY=S,G,N,W,P,A,C,L,M
.venv\Scripts\python tools\regression.py 1020p
pause
