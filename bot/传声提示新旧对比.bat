@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 传声提示新旧对比（10/23）：改前 = 1023o（10/22 22:15 那版【传声】提示，其余和现在一样），改后 = 现在（23:11 润色 + 01:35 加回“就别用”）
rem 只差【传声】那两段提示的写法。跑 4 道题，每题每版 20 次，约 160 次调用、两三毛钱：
rem   V02 陌生人要语音、V04 熟人刚听过又要（答应的比例），V14 只是夸刚才那条好听（不该发，看误发），V15 陌生人打招呼（不该发）
rem 结果：docs\人设回归测试\回归_时间.md，看“命中次数”那张表
set "REG_ONLY=V02,V04,V14,V15"
set "REG_SAMPLES=20"
".venv\Scripts\python" tools\regression.py 1023o
set "REG_ONLY="
set "REG_SAMPLES="
pause
