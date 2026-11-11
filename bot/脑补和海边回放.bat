@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 脑补和海边回放（11/11，聊天 agent，《问题排查记录-脑补和一直在海边-1111》）：一次跑两个
rem 第 1 部分 措辞对比：11/11 群里那段对话的三处（X1 被问“动什么身”、X2“干嘛不去小镇或者国家”、X3“准备离开海边了吗”），
rem   A = 现在的代码，U = 11/11 以前；每题每种 10 次，约 60 次调用。带上对方当时的长期记忆档案（server_logs\2026-11-11_1806）
rem 第 2 部分 待久回放：N = 新的待久那句（带“爱去国家、城镇”），M2 = 11/11 以前那句；冒泡、写信、待了 3 天的，约 65 次调用
rem 11/11 第 3 轮：待久那句又加了半句“下一站多半是去有人住的国家、城镇”，两部分都只跑现在的代码（X1～X3 各 20 次、冒泡 10 次、写信 20 次，约 90 次调用），和第 2 轮比。不碰 data，不连 QQ。结果在 docs\措辞对比\ 和 docs\待久回放\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_naobu"
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%"
xcopy /e /i /q plugins "%T%\plugins" >nul
xcopy /e /i /q personas "%T%\personas" >nul
xcopy /e /i /q knowledge "%T%\knowledge" >nul
copy /y tests\conftest.py "%T%\" >nul
copy /y tests\pytest.ini "%T%\" >nul
copy /y tools\replay_chat.py "%T%\replay_chat.py" >nul
copy /y tools\phrasing_compare.py "%T%\test_phrasing.py" >nul
copy /y tools\life_stay_compare.py "%T%\test_lifestay.py" >nul
set "REPLAY_BOT=%~dp0."
set "PHRASING_ONLY=X1,X2,X3"
set "PHRASING_VARIANTS=A"
set "PHRASING_SAMPLES=20"
set "LIFESTAY_VARIANTS=N"
set "LIFESTAY_ONLY=B11,L11"
pushd "%T%"
echo ===== 第 1 部分：措辞对比 =====
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
echo ===== 第 2 部分：待久回放 =====
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_lifestay.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
