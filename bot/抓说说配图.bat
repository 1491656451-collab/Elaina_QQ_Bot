@echo off
chcp 65001 >nul
cd /d %~dp0
rem 双击：从 Safebooru 抓 30 张没抓过的伊蕾娜同人图，放进 D:\QQBot\待选配图\ 给你挑
rem 想抓别的张数：把这一行的 30 改掉；代理默认试 127.0.0.1:7890
.venv\Scripts\python tools\nowmi.py tools\fetch_gallery.py 30
echo.
if exist "%~dp0..\待选配图" explorer "%~dp0..\待选配图"
pause
