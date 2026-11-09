@echo off
chcp 65001 >nul
rem 清理 QQBot 文件夹（11/07，清理 agent）。清单和理由见 docs\清理清单-1107.md。
rem 不真删：挪进 _待删除_1107\，确认没问题后你再手动删掉那个文件夹。
rem 撤回：清理-1107.bat 还原
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PY=bot\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo 找不到 %PY%
  pause
  exit /b 1
)
if /i "%~1"=="还原" (
  "%PY%" "清理-1107.py" --还原
  echo.
  pause
  exit /b 0
)
"%PY%" "清理-1107.py" --档 3
echo.
echo 档是累加的：1 = 只第 1 档；2 = 第 1、2 档；3 = 全部。
set "LV="
set /p LV=处理到第几档？输入 1 / 2 / 3，直接回车 = 取消：
if "%LV%"=="1" goto run
if "%LV%"=="2" goto run
if "%LV%"=="3" goto run
echo 已取消，什么都没动。
pause
exit /b 0
:run
"%PY%" "清理-1107.py" --档 %LV% --执行
echo.
pause
