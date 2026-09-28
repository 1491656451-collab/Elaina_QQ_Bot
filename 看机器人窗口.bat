@echo off
chcp 65001 >nul
if not exist "%~dp0server.txt" (
  echo 找不到 %~dp0server.txt ，请新建这个文件，里面写一行：admin@你的服务器IP
  pause
  exit /b 1
)
set /p SERVER=<"%~dp0server.txt"
title 机器人（服务器）
echo 正在连接服务器，显示机器人的实时输出（和以前 start.bat 窗口里的一样，过了半夜也会接着显示）……
echo 关掉这个窗口、或者按 Ctrl+C，都只是不看了，机器人照常运行。
echo.
ssh -t %SERVER% "journalctl -fu qqbot -o cat -n 60"
pause
