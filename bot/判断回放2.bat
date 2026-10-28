@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 判断回放第二轮（10/28，聊天 agent，《判断回放报告-1028_1736》）：第一轮只加两条规则（B）几乎没用，这轮再试三种：
rem   C = B 再去掉“拿不准的时候也回答否”
rem   D = 现在的写法，但先让它用一句话说清新消息在接谁的话，再答是 / 否
rem   E = B + 先说一句理由
rem 同样 35 道题，每种 3 次，约 315 次短调用，一两分钱
rem 第二部分：“我替我替”换成 pro 模型再问 10 次（第一轮 flash 10 次全没接上），约 10 次调用
echo ==== 第一部分：判断回放（C、D、E）====
set "JUDGE_VARIANTS=C,D,E"
".venv\Scripts\python" tools\judge_compare.py
set "JUDGE_VARIANTS="
echo.
echo ==== 第二部分：我替我替（G11）用 pro，10 次 ====
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_judgecmp2"
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
set "PHRASING_VARIANTS=P"
set "PHRASING_SAMPLES=10"
set "PHRASING_ONLY=G11"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了，告诉我一声就行（我从 docs\判断回放\ 和 docs\措辞对比\ 里拿最新的结果）
pause
