@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 主模型对比（10/22，第三批第 1 项）：聊天主模型 flash 和 pro 各生成几次，打乱顺序给两个 agent 盲评
rem 第一部分：回归测试 82 题，flash、pro 各问 5 次，约 820 次调用；自动数翻车，另出一份盲评稿（每版每题挑 2 条）
rem 第二部分：措辞对比 15 道真实场景（怪话清单），flash、pro 各 5 次，约 150 次；走机器人真实流程，出盲评稿
rem 合计大约四五块钱（pro 那一半占大头）。pro 用 .env 的 MEMORY_PRO_API_KEY（没填就用 DEEPSEEK_API_KEY）
rem 结果：docs\人设回归测试\回归_时间_盲评.md、docs\措辞对比\措辞对比_时间_盲评.md，各带一份打分表和答案（答案打完分再看）
echo ==== 第一部分：回归测试（flash 对 pro）====
set "REG_MODEL_BEFORE=deepseek-flash"
set "REG_MODEL_AFTER=deepseek-v4-pro"
set "REG_SAMPLES=5"
set "REG_BLIND=1"
set "REG_BLIND_PICK=2"
".venv\Scripts\python" tools\regression.py 当前
set "REG_MODEL_BEFORE="
set "REG_MODEL_AFTER="
set "REG_SAMPLES="
set "REG_BLIND="
set "REG_BLIND_PICK="
echo.
echo ==== 第二部分：真实场景回放（flash 对 pro）====
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_modelcmp"
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
set "PHRASING_SAMPLES=5"
set "PHRASING_ONLY=G1,G2,G3,G4,G5,G6,G7,G8,P1,P2,P3,P4,P5,P6,P7"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了。盲评稿在 docs\人设回归测试\ 和 docs\措辞对比\ 最新的 *_盲评.md，打分表是 *_打分表.txt
pause
