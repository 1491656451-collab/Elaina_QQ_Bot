@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 空间二创回放（11/22，聊天 agent，《问题排查记录-空间二创说说-1122》）：鸡块嚎吃的 4 条二创说说，看识图准不准、她评不评、评什么
rem   图在 tools\空间二创\1～4.jpg；识图每张 3 次，决定 / 评论每题两版各 5 次（V = 识图的描述，H = 人写的准确描述）
rem   约 90 次调用，一两毛钱。只读空间、识图的代码；不碰 data，不连 QQ，不发评论。结果在 docs\空间二创回放\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_erchuang"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\acg_post_replay.py "%T%\test_acg.py" >nul
set "REPLAY_BOT=%~dp0."
set "ACG_SAMPLES=5"
set "ACG_VISION_TIMES=3"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_acg.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
