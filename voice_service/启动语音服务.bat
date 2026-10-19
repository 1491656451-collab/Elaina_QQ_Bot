@echo off
chcp 65001 >nul
title 伊蕾娜语音服务
rem 用 GPT-SoVITS 整合包自带的 Python 运行；整合包不在这个位置的话，改下面这一行，再改 本机配置.json 里的 gsv_dir
set GSV_DIR=D:\GPT-SoVITS\GPT-SoVITS-v2pro-20250604
if not exist "%GSV_DIR%\runtime\python.exe" (
  echo 没找到 %GSV_DIR%\runtime\python.exe ，用记事本改这个 bat 第 5 行
  pause
  exit /b 1
)
"%GSV_DIR%\runtime\python.exe" "%~dp0elaina_tts.py"
pause
