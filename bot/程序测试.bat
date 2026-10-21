@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 程序测试：把插件、人设、小说资料和 tests 里所有测试拷到临时目录再跑，不碰真的 data（10/22 起跑 tests 里全部 test_*.py，以前只跑 test_chat.py）
set "T=%TEMP%\qqbot_pytest"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\*.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -p no:cacheprovider
popd
pause
