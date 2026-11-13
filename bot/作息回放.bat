@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 作息回放（11/13，聊天 agent，《方案-她自己定作息-1113》）：编几种当天的情况，看她自己定的几点睡、几点起、理由
rem   S1 野外露宿 / S2 刚住进旅馆 / S3 赶了一整天路 / S4 什么都没说过 / S5 前两晚都 1 点睡；每题 10 次，约 50 次调用，几分钱
rem 不碰 data，不连 QQ。结果在 docs\作息回放\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_zuoxi"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\sleep_plan_compare.py "%T%\test_sleepplan.py" >nul
set "REPLAY_BOT=%~dp0."
set "SLEEP_SAMPLES=10"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_sleepplan.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
