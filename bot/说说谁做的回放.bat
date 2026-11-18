@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 说说“谁做的”回放（11/18，空间 agent）：猫那条把“舍不得打飞”的人写反了。
rem   W1～W3 三题，改前（见闻只有一句）和改后（见闻带当时的几句）各写 5 次，约 30～40 次调用，几分钱。
rem   用 server_logs 里最新的 posts.json（当时的配图描述和最近几条）
rem 不碰 data，不连 QQ，不发说说。结果在 docs\说说回放\谁做的回放_时间.md
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_shuoshuo_who"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\diary_replay.py "%T%\test_diaryreplay.py" >nul
set "REPLAY_BOT=%~dp0."
set "DIARY_SAMPLES=5"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_diaryreplay.py -k test_who_replay
popd
echo.
echo 跑完了，告诉我一声就行
pause
