@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 空间评论回放（11/21，空间 agent，《问题排查记录-空间评论人机感和没礼貌-1121》）：她在别人说说下说过的几句，改前和改后各写 5 次
rem   F1～F7 七条说说 + R1 一条回复，每题每版 5 次评论 + 5 次“赞 / 评”的决定，约 160 次调用，一两毛钱
rem 不碰 data，不连 QQ，不发评论。结果在 docs\空间评论回放\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_qzone_comment"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\comment_replay.py "%T%\test_commentreplay.py" >nul
set "REPLAY_BOT=%~dp0."
set "COMMENT_SAMPLES=5"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_commentreplay.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
