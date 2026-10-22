@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 人设减肥对比（10/22，第三批第 3 项）：改前（docs\人设打磨备份 里的 1022 备份）对改后（现在的人设和 prompts.py），都用 flash
rem 第一部分：回归测试 82 题，改前、改后各问 5 次，约 820 次调用；自动数翻车，另出一份盲评稿（每版每题挑 2 条）
rem 第二部分：措辞对比 15 道真实场景，O（改前）、A（改后）各 5 次，约 150 次；走机器人真实流程，出盲评稿
rem 第三部分：传声题。① 语音能用：V 开头 14 题只跑改后，每题 10 次（同 传声测试.bat）；② 语音用不了：V01～V08 换成“这一轮传不了”的提示，每题 10 次
rem 合计大约一块多钱（全是 flash）
rem 结果：docs\人设回归测试\回归_时间*.md、docs\措辞对比\措辞对比_时间_盲评.md，盲评稿各带一份打分表和答案（答案打完分再看）
echo ==== 第一部分：回归测试（改前 1022 对 改后）====
set "REG_SAMPLES=5"
set "REG_BLIND=1"
set "REG_BLIND_PICK=2"
".venv\Scripts\python" tools\regression.py 1022
set "REG_SAMPLES="
set "REG_BLIND="
set "REG_BLIND_PICK="
echo.
echo ==== 第二部分：真实场景回放（O 改前 对 A 改后）====
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_slim"
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
set "PHRASING_OLD=1022"
set "PHRASING_VARIANTS=O,A"
set "PHRASING_SAMPLES=5"
set "PHRASING_ONLY=G1,G2,G3,G4,G5,G6,G7,G8,P1,P2,P3,P4,P5,P6,P7"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
set "PHRASING_OLD="
set "PHRASING_VARIANTS="
set "PHRASING_SAMPLES="
set "PHRASING_ONLY="
echo.
echo ==== 第三部分①：传声题，语音能用（只跑改后）====
set "REG_ONLY=V"
set "REG_AFTER_ONLY=1"
".venv\Scripts\python" tools\regression.py 当前
echo.
echo ==== 第三部分②：传声题，语音用不了（V01～V08，只跑改后）====
set "REG_ONLY=V0"
set "REG_VOICE_OFF=1"
".venv\Scripts\python" tools\regression.py 当前
set "REG_ONLY="
set "REG_AFTER_ONLY="
set "REG_VOICE_OFF="
echo.
echo 跑完了。盲评稿在 docs\人设回归测试\ 和 docs\措辞对比\ 最新的 *_盲评.md，打分表是 *_打分表.txt；传声两份是 docs\人设回归测试\ 最后两份 回归_时间.md
pause
