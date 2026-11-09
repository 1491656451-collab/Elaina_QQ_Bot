@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 短回对比（11/09，聊天 agent，《交接-聊天真实感三项-聊天-1109》A）：对方随口一句时，她能不能只回一两个字 / 只甩一个表情
rem   现 = 现在线上；甲 = 【可以不回】改成三选一；乙 = 甲 + 长短固定“几个字就好”；丙在乙的结果上算（只发第一条）
rem   题目在 tools\short_reply_cases.json：私聊 6 题 × 很熟 / 陌生人，群里 1 题；每题 现 5 次、甲乙各 10 次
rem   要用到 server_logs\2026-11-09_0406 里的日志（已经在你电脑上了）；不碰 data，不连 QQ
rem 11/09 第 2 轮：只跑丁（长短固定一句不带例子的“几个字就好”），每题 10 次，约 130 次调用。要全跑就删掉下面 SHORT_VARIANTS 那行。结果在 docs\短回对比\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_duanhui"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\phrasing_compare.py "%T%\phrasing_compare.py" >nul
copy /y tools\short_reply_compare.py "%T%\test_short.py" >nul
set "REPLAY_BOT=%~dp0."
set "SHORT_VARIANTS=D"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_short.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
