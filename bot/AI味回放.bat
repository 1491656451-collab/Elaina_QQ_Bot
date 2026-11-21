@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem AI味回放（11/21，聊天 agent，《分析-AI味和DS五条建议-1121》）：11/18、11/21 和群鲨鱼私聊里抠字眼、打辩论、对 emoji 正经发问的那几句
rem   Q1～Q6：11/18、11/21 的真实上下文；L2、K3：11/05 群里正常接话的对照题（看改了以后会不会变得太敷衍）
rem   W = 改前（没有“斗嘴别打辩论”“说人话”、emoji 没提示），A = 现在，Z = 现在 + 回放里临时去掉人设“有主见”里两句（不动人设文件）
rem   每种每题 5 次，约 120 次调用。要用到 server_logs\2026-11-21_2249 和 2026-11-05_1610（已经在你电脑上了）
rem 不碰 data，不连 QQ。结果在 docs\措辞对比\
".venv\Scripts\python" -c "import nonebug, pytest_asyncio" 2>nul || ".venv\Scripts\python" -m pip install nonebug==0.4.4 pytest==9.1.1 pytest-asyncio==1.4.0
set "T=%TEMP%\qqbot_aiwei"
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
set "PHRASING_ONLY=Q1,Q2,Q3,Q4,Q5,Q6,L2,K3"
set "PHRASING_VARIANTS=W,A,Z"
set "PHRASING_SAMPLES=5"
pushd "%T%"
"%~dp0.venv\Scripts\python" -m pytest -q -s -p no:cacheprovider test_phrasing.py
popd
echo.
echo 跑完了，告诉我一声就行
pause
