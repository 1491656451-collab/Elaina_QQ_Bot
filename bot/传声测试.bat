@echo off
chcp 65001 >nul
cd /d %~dp0
rem 传声测试（10/20）：只跑回归测试里 V 开头的 14 道语音题（V01-V08 该发，V11-V16 不该发），每题 10 次，只跑改后（现在的人设和代码），约 140 次调用、一两毛钱
rem 结果写到 docs\人设回归测试\回归_时间.md，看“命中次数”那张表：该发的题是答应的比例，不该发的题是误发的次数
set REG_ONLY=V
set REG_AFTER_ONLY=1
.venv\Scripts\python tools\regression.py 当前
pause
