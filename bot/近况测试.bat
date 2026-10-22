@echo off
chcp 65001 >nul
cd /d %~dp0
rem 近况测试（10/23，聊天 agent）：只跑回归测试里 J 开头的 8 道近况题，每题 10 次，改前（1023L：还没有【你的近况】的代码）和改后各一遍，约 160 次调用、一两毛钱
rem 看什么：J01 近况里有猫时接不接得上；J02 没有时会不会另编；J03 说在森林时被问在哪；J04-J06 不相关的闲聊里提近况的次数（越少越好）；J07 冒泡别重复这个群听过的事；J08 对方没提面包时提面包的次数
rem 结果写到 docs\人设回归测试\回归_时间.md
set REG_ONLY=J
.venv\Scripts\python tools\regression.py 1023L
pause
