@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 看图对比（11/06，聊天 agent，识图 agent《交接-聊天模型直接看图-聊天-1106》方案 A）：
rem   D = 只给描述（现在线上），I = 描述 + 原图（VISION_CHAT_SEES_IMAGE=true），每种每题 3 次
rem   题目在 tools\image_chat_cases.json：梗图接上下文、聊天截图、别的灰发角色（不能认成自己）、她自己的图、实物照片、普通照片
rem   普通照片放在 tools\识图普通照片\1.jpg、2.jpg（没有就跳过这两题）
rem   同一张图只看一次，D、I 用同一份描述；走真实的私聊流程，长期记忆关掉，不碰 data，不连 QQ
rem 约 80 次聊天调用 + 十几次看图，一两毛钱。结果在 docs\看图对比\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_kantu"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\image_chat_compare.py "%T%\test_image.py" >nul
set "REPLAY_BOT=%~dp0."
set "IMGCMP_SAMPLES=3"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_image.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
