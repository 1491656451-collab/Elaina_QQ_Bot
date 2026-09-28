@echo off
chcp 65001 >nul
cd /d %~dp0
rem 启动诊断：只加载插件、不连 QQ，卡住的位置写到 data\logs\启动诊断.txt（最多约 1 分半自己结束）
.venv\Scripts\python -X faulthandler tools\startup_diag.py
echo.
echo 诊断结束，结果在 data\logs\启动诊断.txt
pause
