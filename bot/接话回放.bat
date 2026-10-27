@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 接话回放（10/27，聊天 agent，《问题排查记录-没接上话-1027》第二节）：
rem 10/22 她冒泡说“小孩问我的扫帚是不是大号鸡毛掸子”，对方 @ 她“都差求不多”，她回“你在说什么，我没跟上”。
rem 把这段原样回放（走真实流程），现在的模型（flash）和 pro 各问 10 次，看她接不接得上。两道题：G9 第一句、G10 第二句“都差不多啦”
rem 一共约 40 次调用，几分钱。pro 用 .env 的 MEMORY_PRO_API_KEY（没填就用 DEEPSEEK_API_KEY）
rem 要用到 server_logs\2026-10-27_1547 里的日志（已经在你电脑上了）
rem 结果：docs\措辞对比\ 最新的 措辞对比_时间.json（全部原话）和 _盲评.md、_答案.json
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_jiehua"
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
set "PHRASING_VARIANTS=A,P"
set "PHRASING_SAMPLES=10"
set "PHRASING_ONLY=G9,G10"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了。把 docs\措辞对比\ 里最新的 措辞对比_时间.json 发给我就行
pause
