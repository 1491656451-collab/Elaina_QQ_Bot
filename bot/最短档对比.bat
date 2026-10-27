@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 最短档对比（10/27，聊天 agent，《接话回放报告-1027_1808》）：回复长短抽到最短那档“几个字就好，比如‘嗯。’‘蛤？’”时，
rem 对方那句有点费解，她容易回“嗯？听不懂”。这里把长短固定在最短那档，现在的说法（S）和改过的说法（T）各跑一遍：
rem   第一部分：10/22 “都差求不多”（G9），S、T 各 10 次，看接上的比例
rem   第二部分：以前的几道“随口一句”题（G2 就想叫你一下、G6 一起、G8 装高冷、P7 还没睡吧），S、T 各 5 次，看改过的说法会不会让短回复变啰嗦
rem 一共约 60 次调用，几分钱。结果：docs\措辞对比\ 里最新的两个 措辞对比_时间.json
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_shortcmp"
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
set "PHRASING_VARIANTS=S,T"
pushd "%T%"
echo ==== 第一部分：都差求不多（G9），每种 10 次 ====
set "PHRASING_ONLY=G9"
set "PHRASING_SAMPLES=10"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
echo.
echo ==== 第二部分：以前的随口一句（G2、G6、G8、P7），每种 5 次 ====
set "PHRASING_ONLY=G2,G6,G8,P7"
set "PHRASING_SAMPLES=5"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了。把 docs\措辞对比\ 里最新的两个 措辞对比_时间.json 发给我就行（不带 _答案、_盲评 的那两个）
pause
