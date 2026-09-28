@echo off
chcp 65001 >nul
rem 把服务器上的日志和聊天记录下载到 D:\QQBot\server_logs\日期时间\ ，方便在电脑上排查问题。
rem 只下载，不改服务器上的任何东西。server_logs 已写进 .gitignore，不会传到 GitHub。
rem 服务器地址写在同目录的 server.txt 里（一行，比如 admin@1.2.3.4）。server.txt 不传 GitHub，免得公开服务器 IP
if not exist "%~dp0server.txt" (
  echo 找不到 %~dp0server.txt ，请新建这个文件，里面写一行：admin@你的服务器IP
  pause
  exit /b 1
)
set /p SERVER=<"%~dp0server.txt"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
set OUT=%~dp0server_logs\%STAMP%
mkdir "%OUT%" 2>nul
echo 正在从服务器下载日志到 %OUT% ……
echo.
echo [1/5] 机器人日志
scp %SERVER%:QQBot/bot/data/logs/bot_*.log "%OUT%"
if not exist "%OUT%\bot_*.log" echo   ！！没下载到机器人日志，请把这个窗口截图发给 Claude
echo [2/5] 风控记录
scp %SERVER%:QQBot/bot/data/risk_events.log "%OUT%"
echo [3/5] 空间日志
scp %SERVER%:QQBot/bot/data/qzone/qzone.log "%OUT%"
echo [4/5] NapCat 日志（最近 2 万行）
ssh %SERVER% "tail -n 20000 ~/napcat.log" > "%OUT%\napcat.log"
echo [5/5] 聊天记录（短期记忆）
scp -r -q %SERVER%:QQBot/bot/data/history "%OUT%\history"
echo.
echo 完成：%OUT%
explorer "%OUT%"
pause
