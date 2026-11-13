@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 刚看到回放（11/13，聊天 agent，《日志复核-1113》）：11/12 23:19 私聊聊冷了等了 5 分钟才看，她回复开头写了“（刚看到）”
rem   Y1：A = 现在的提示（不带“【刚看到】”标题），V = 11/13 以前的提示；每种 20 次，约 40 次调用
rem   报告里看模型原话（发前清理之前）有没有括号、有没有编理由解释晚回。要用到 server_logs\2026-11-13_1810（已经在你电脑上了）
rem 不碰 data，不连 QQ。结果在 docs\措辞对比\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_gangkandao"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\phrasing_compare.py "%T%\test_phrasing.py" >nul
set "REPLAY_BOT=%~dp0."
set "PHRASING_ONLY=Y1"
set "PHRASING_VARIANTS=A,V"
set "PHRASING_SAMPLES=20"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
