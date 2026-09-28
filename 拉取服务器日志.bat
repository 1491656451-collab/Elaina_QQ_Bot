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
echo [1/6] 机器人日志
scp %SERVER%:QQBot/bot/data/logs/bot_*.log "%OUT%"
if not exist "%OUT%\bot_*.log" echo   ！！没下载到机器人日志，请把这个窗口截图发给 Claude
echo [2/6] 风控记录
scp %SERVER%:QQBot/bot/data/risk_events.log "%OUT%"
echo [3/6] 空间日志
scp %SERVER%:QQBot/bot/data/qzone/qzone.log "%OUT%"
echo [4/6] NapCat 日志（最近 2 万行）
ssh %SERVER% "tail -n 20000 ~/napcat.log" > "%OUT%\napcat.log"
echo [5/6] 聊天记录（短期记忆）
scp -r -q %SERVER%:QQBot/bot/data/history "%OUT%\history"
echo [6/6] 长期记忆、空间记录、表情标签、识图缓存
ssh %SERVER% "cd QQBot/bot/data && tar -czf - memory qzone/daily qzone/*.json same_names.json stickers/index.json vision_cache.json online_state.json 2>/dev/null" > "%OUT%\data.tgz"
mkdir "%OUT%\data" 2>nul
tar -xzf "%OUT%\data.tgz" -C "%OUT%\data"
if errorlevel 1 (echo   ！！长期记忆没解压成功，请截图发给 Claude) else (del "%OUT%\data.tgz")
> "%OUT%\说明.txt" echo 这是服务器在 %STAMP% 的日志和记忆快照，只用来在电脑上排查问题。
>> "%OUT%\说明.txt" echo 不要把这里的 history、data 复制到 D:\QQBot\bot\data 或传回服务器：服务器上的才是最新的，覆盖了会让她的记忆倒回去。
>> "%OUT%\说明.txt" echo data\memory\users\QQ号.json = 每个人的档案（印象、好感、性别、聊天次数）；data\memory\groups\群号.json = 群往事；data\memory\pending = 还没整理进档案的消息；
>> "%OUT%\说明.txt" echo data\qzone\daily = 每天的今日见闻；data\qzone\*.json = 发过的说说、看过的评论、空间状态；history = 每个群、每个私聊最近的对话（短期记忆）。
echo.
echo 完成：%OUT%
explorer "%OUT%"
pause
