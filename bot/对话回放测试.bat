@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 对话回放测试：把服务器日志里真实的一段群聊按原来的顺序、时间喂给聊天代码，看她在关键那句上怎么回
rem 题目在 tools\replay_cases.json，日志在 ..\server_logs\。改后 = 现在的代码，改前 = docs\人设打磨备份 里的备份（默认 0930a，可以跟参数：对话回放测试.bat 0929h）
rem 每题问 3～5 次，改前改后各一遍，一共大约 40 次调用，一两毛钱；结果在 docs\对话回放\
set "B=0930a"
if not "%~1"=="" set "B=%~1"
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
call :run 改后 ""
call :run 改前_%B% "%B%"
echo.
echo 结果在 docs\对话回放\
pause
goto :eof

:run
set "T=%TEMP%\qqbot_replay"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
set "BK=%~dp0..\docs\人设打磨备份"
if not "%~2"=="" copy /y "%BK%\__init__.%~2.py.bak" "%T%\plugins\roleplay_chat\__init__.py" >nul
if not "%~2"=="" if exist "%BK%\echocheck.%~2.py.bak" copy /y "%BK%\echocheck.%~2.py.bak" "%T%\plugins\roleplay_chat\echocheck.py" >nul
if not "%~2"=="" if exist "%BK%\elaina.%~2.md.bak" copy /y "%BK%\elaina.%~2.md.bak" "%T%\personas\elaina.md" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\test_replay.py" >nul
set "REPLAY_BOT=%~dp0."
set "REPLAY_TAG=%~1"
echo.
echo ===== %~1 =====
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_replay.py
popd
goto :eof
