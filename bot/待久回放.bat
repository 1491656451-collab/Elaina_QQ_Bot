@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 待久回放（11/09，聊天 agent，《交接-聊天真实感三项-聊天-1109》C）：她在海边待了 11 天，冒泡、写信会不会自己掂量走不走
rem   N = 现在的代码（待了 7 天以上加一句“接着待还是动身，你自己定”；“同一类”写明白了），O = 改前（对照）
rem   B11 冒泡 / B11+ 冒泡且另一个群刚讲过海边捡到东西 / L11 写信（都是待了 11 天）；B3、L3 待了 3 天（不该提走）
rem   近况用 server_logs\2026-11-09_0406\data\memory\self.json（已经在你电脑上了）；不碰 data，不连 QQ
rem 11/09 第 3 轮：只比待久那句的两种候选说法 M1、M2（提示里的“现在”按下午 3 点算），题目 B11、B11+、L11；约 70 次调用，几分钱
rem   要跑全部（N、O 和待了 3 天的题）就把下面 LIFESTAY_ 开头的两行 set 删掉。结果在 docs\待久回放\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_daijiu"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\life_stay_compare.py "%T%\test_lifestay.py" >nul
set "REPLAY_BOT=%~dp0."
set "LIFESTAY_VARIANTS=M1,M2"
set "LIFESTAY_ONLY=B11,B11+,L11"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_lifestay.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
