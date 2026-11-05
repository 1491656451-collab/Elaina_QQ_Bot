@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 人机感回放（11/05，聊天 agent，《人机感检查-周到型-1105》第一批）：改前（Q）和改后（A）各问 6 次
rem   K1～K5：对方开玩笑、胡扯、抬杠（“我用 1000 天的工资替你付了”“你不看跟我说干嘛”“咄咄逼人啊这个魔女”……），看她还会不会逐条讲理、追着算账
rem   L1～L3：没人问她近况的闲聊，数她扯到海边、旅馆的账的次数；L4：对方问“还没准备上路吗”，近况要照常接得上
rem 改前 = 近况每轮整段带、聊天规则里没有“玩笑别当真”；改后 = 现在电脑上的代码
rem “一直找她就不理”和“早上不回几小时前的群消息”是程序规则，已经用测试和日志算过，不在这里回放
rem 约 110 次调用，一两毛钱。要用到 server_logs\2026-11-05_1610 里的日志（已经在你电脑上了）
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_renji"
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
set "PHRASING_VARIANTS=Q,A"
set "PHRASING_SAMPLES=6"
set "PHRASING_ONLY=K1,K2,K3,K4,K5,L1,L2,L3,L4"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
