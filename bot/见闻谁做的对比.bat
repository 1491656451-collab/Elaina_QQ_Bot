@echo off
chcp 65001 >nul
cd /d %~dp0
rem 今日见闻“谁做的”改前改后对比（11/18，记忆 agent）：4 段 × 改前改后 × 3 次，24 次整理，几分钱。结果在 docs\记忆测试\见闻谁做的_时间.md
.venv\Scripts\python tools\nowmi.py tools\moment_who_compare.py
echo.
echo 跑完了，告诉我一声就行
pause
