@echo off
chcp 65001 >nul
if not exist "%~dp0server.txt" (
  echo 找不到 %~dp0server.txt ，请新建这个文件，里面写一行：admin@你的服务器IP
  pause
  exit /b 1
)
set /p SERVER=<"%~dp0server.txt"
title NapCat 网页后台（服务器）
echo 正在读取网页后台的 token……
for /f "delims=" %%t in ('ssh %SERVER% jq -r .token Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json') do set TOKEN=%%t
if "%TOKEN%"=="" (
  echo 没读到 token，请截图发给 Claude
  pause
  exit /b 1
)
echo 正在建立到服务器的加密通道，几秒后会自动打开浏览器……
echo 用完网页后台，关掉这个窗口就断开了（NapCat 照常运行）。
echo 注意：网页后台里别点“退出登录”“重启”，否则小号会下线、要重新扫码。
echo.
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:6099/webui?token=%TOKEN%"
ssh -N -L 6099:127.0.0.1:6099 %SERVER%
pause
