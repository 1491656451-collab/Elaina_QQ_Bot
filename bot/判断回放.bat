@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 判断回放（10/28，聊天 agent，《问题排查记录-群里没反应过来-1028》）：
rem   第一部分：群里没 @ 她的消息，程序先问判断模型“是不是在跟她说话”。拿日志里 35 条当时判过的群消息（tools\judge_cases.json，
rem             已标好该回 / 不该回 / 都行），现在的判断提示（A）和改过的（B）各问 3 次，数认出来几条、误判几条。约 210 次很短的调用，一两分钱
rem   第二部分：“我替我替”（G11）原样回放 10 次，看她接不接得上自己刚说的“替我付旅馆钱”。约 10 次调用
rem 要用到 server_logs\2026-10-28_1710 里的日志（已经在你电脑上了）
rem 结果：docs\判断回放\ 最新的 判断回放_时间.md；docs\措辞对比\ 最新的 措辞对比_时间.json
echo ==== 第一部分：判断回放 ====
".venv\Scripts\python" tools\judge_compare.py
echo.
echo ==== 第二部分：我替我替（G11），10 次 ====
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_judgecmp"
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
set "PHRASING_VARIANTS=A"
set "PHRASING_SAMPLES=10"
set "PHRASING_ONLY=G11"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了。把 docs\判断回放\ 最新的 判断回放_时间.json 和 docs\措辞对比\ 最新的 措辞对比_时间.json 发给我就行
pause
