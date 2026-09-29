@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "T=%TEMP%\qqbot_pytest"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\test_chat.py "%T%\" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -p no:cacheprovider test_chat.py
popd
pause
