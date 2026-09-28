@echo off
chcp 65001 >nul
cd /d %~dp0
rem 双击：处理 bot\qzone\images 里的图；把文件夹拖到这个文件上：处理那个文件夹里的图
rem 结果在 D:\QQBot\待上传配图\ ，原图不改
.venv\Scripts\python tools\nowmi.py tools\shrink_gallery.py "%~1"
echo.
echo 缩好的图在：%~dp0..\待上传配图\
explorer "%~dp0..\待上传配图"
pause
