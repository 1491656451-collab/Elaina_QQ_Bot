@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 表情包回放（11/17，聊天 agent，《问题排查记录-表情包当图片-1117》）：对方拿图当表情回她，她接不接得住情绪
rem   X = 改前（图一律叫“图片”、没提示），N = 改后，每种每题 3 次
rem   题目在 tools\sticker_cases.json，图放在 tools\表情包对比\：K1.gif（11/17 那张灵梦）、K2.jpg（哭 / 裂开）、K3.jpg（无语 / 白眼）、
rem   K4.jpg（QQ 当普通图片发的反应图）、K5.jpg（普通饭菜照片）。缺哪张就跳过哪题
rem   同一张图只看一次，X、N 用同一份描述；走真实的私聊流程，长期记忆关掉，不碰 data，不连 QQ
rem 约 30 次聊天调用 + 5 次看图，几分钱。结果在 docs\表情包回放\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_biaoqing"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\sticker_compare.py "%T%\test_sticker.py" >nul
set "REPLAY_BOT=%~dp0."
set "STK_SAMPLES=3"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_sticker.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
