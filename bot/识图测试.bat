@echo off
chcp 65001 >nul
cd /d %~dp0
if "%~1"=="" (
    echo 把一张或几张图片拖到这个文件上，就能看到本机角色识别给的分数。
    pause
    exit /b
)
.venv\Scripts\python tools\tag_test.py %*
pause
