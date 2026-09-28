@echo off
chcp 65001 >nul
if not exist "%~dp0server.txt" (
  echo 找不到 %~dp0server.txt ，请新建这个文件，里面写一行：admin@你的服务器IP
  pause
  exit /b 1
)
set /p SERVER=<"%~dp0server.txt"
title NapCat（服务器）
echo 正在连接服务器，显示 NapCat 的实时日志……
echo 关掉这个窗口、或者按 Ctrl+C，都只是不看了，NapCat 照常运行。
echo.
ssh -t %SERVER% "tail -n 60 -F ~/napcat.log"
pause
