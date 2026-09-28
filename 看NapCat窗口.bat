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
echo （QQ 每次启动都会打印一堆 Bugly、CrashHandler 开头的崩溃上报信息，不代表崩溃，这里已经过滤掉；二维码的方块字符也过滤掉了）
echo.
ssh -t %SERVER% "tail -n 200 -F ~/napcat.log | grep --line-buffered -v -E 'Bugly|CrashHandler|crash_files|pub.key|StartWithOptions|PostDelayedTask|SetLogger|fatalSetup|GetDllPath|linux-bugly|█|▀|▄'"
pause
