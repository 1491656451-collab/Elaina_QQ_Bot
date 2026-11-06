@echo off
chcp 65001 >nul
rem 把电脑上的人设 bot\personas\elaina.md 传到服务器（11/05，部署 agent）。
rem 先比对，一样就不传；不一样先在服务器上备份旧的（只留最近 5 份），上传后再核对一遍。
rem 服务器地址读同目录的 server.txt。不重启机器人：接着跑 ~/update.sh 就行，只改了人设就在 QQ 里发 /重载人设。
cd /d "%~dp0"
if not exist "bot\.venv\Scripts\python.exe" (
  echo 找不到 bot\.venv\Scripts\python.exe
  pause
  exit /b 1
)
"bot\.venv\Scripts\python.exe" "bot\tools\upload_persona.py"
echo.
pause
