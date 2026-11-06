@echo off
chcp 65001 >nul
cd /d %~dp0
rem 双击：处理 bot\qzone\images 里的图；把文件夹拖到这个文件上：处理那个文件夹里的图
rem 11/05 起：服务器上已经有的图跳过，只缩、只传新的；缩好就自动 scp 上传（服务器地址在 D:\QQBot\server.txt）
.venv\Scripts\python tools\nowmi.py tools\shrink_gallery.py "%~1"
echo.
pause
