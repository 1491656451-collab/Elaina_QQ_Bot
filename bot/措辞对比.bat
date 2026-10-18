@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem phrasing compare (10/18): replay real chats, 5 variants x 13 cases x 3 samples, about 200 calls
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_phrasing"
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
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 结果在 docs\措辞对比\
pause
