@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem emoji回放（11/22，聊天 agent，《AI味回放报告-1122_0131》第 2 节第 3 条）：对方只发“🐷🐷”时她还会不会正经问“发这个做什么”
rem   Q6：Y = 第 1 版【表情】提示（11/21 晚），A = 第 2 版（“就当他冲你做了个表情……别正经追问”）；每种 15 次，约 30 次调用
rem   要用到 server_logs\2026-11-21_2249（已经在你电脑上了）。不碰 data，不连 QQ。结果在 docs\措辞对比\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_emoji"
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
set "PHRASING_ONLY=Q6"
set "PHRASING_VARIANTS=Y,A"
set "PHRASING_SAMPLES=15"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
