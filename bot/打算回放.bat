@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 打算回放（11/05，聊天 agent，《交接-近况少提-记忆-1105》记忆 agent 回复里的 plans）：只跑现在的代码，每题 6 次
rem   L4：对方问“还没准备上路吗”，近况里没有打算（对照）
rem   L5：同一句，近况里有“你 2 天前说过要动身去别的海岸，说好的就是今天”：看她会不会动身，或者明说改了主意
rem   L6：同一句，近况里是“说好的是昨天，日子过了你还没走”
rem   L7：聊网上下的画、没问她近况，近况里有“今天说好动身”：没问到她时打算不带，她不该自己扯到要走
rem 冒泡（“这几天在别处讲过的”、到日子了冒泡）这个工具回放不了，靠程序测试和上线后看日志
rem 约 24 次调用，几分钱。要用到 server_logs\2026-11-05_1610 里的日志（已经在你电脑上了）
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_dasuan"
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
set "PHRASING_SAMPLES=6"
set "PHRASING_ONLY=L4,L5,L6,L7"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
