@echo off
chcp 65001 >nul
cd /d %~dp0
rem 虚拟环境没建好（第一次运行，或上次建到一半失败了）：删掉重建
if not exist .venv\Scripts\python.exe (
    if exist .venv rmdir /s /q .venv
    echo [首次运行] 创建虚拟环境并安装依赖...
    python -m venv .venv
    if errorlevel 1 (
        echo 创建虚拟环境失败：没找到 Python。请先安装 Python 3.10 以上版本，安装时勾选 Add to PATH，然后重新运行本脚本。
        if exist .venv rmdir /s /q .venv
        pause
        exit /b 1
    )
)
rem 第一次运行，或者 requirements.txt 变了：安装依赖。第一次就装失败：停下，不带着缺依赖的环境启动；以前装好过：先用旧的启动
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
    echo [安装] 安装依赖...
    .venv\Scripts\python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    .venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 goto install_failed
    copy /y requirements.txt .venv\requirements.installed >nul
)
goto check_env

:install_failed
echo.
if exist .venv\requirements.installed (
    echo 依赖更新失败，先用上次装好的依赖启动。网络好了再重开一次本脚本，会自动补装。
    goto check_env
)
echo 依赖安装失败，机器人没有启动。请检查网络后重新运行本脚本；把上面的报错发给我也行。
pause
exit /b 1

:check_env
if not exist .env (
    copy .env.example .env >nul
    echo 已生成 .env，请先用记事本填好 DEEPSEEK_API_KEY 等配置后再运行本脚本。
    notepad .env
    pause
    exit /b
)
.venv\Scripts\python bot.py
pause
