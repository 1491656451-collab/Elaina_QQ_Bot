@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
    echo [首次运行] 创建虚拟环境并安装依赖...
    python -m venv .venv || (echo 未找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH & pause & exit /b)
    .venv\Scripts\python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple && copy /y requirements.txt .venv\requirements.installed >nul
)
rem 依赖有更新（requirements.txt 变了）就补装一次
fc /b requirements.txt .venv\requirements.installed >nul 2>&1 || (
    echo [更新] 安装新增的依赖...
    .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple && copy /y requirements.txt .venv\requirements.installed >nul
)
if not exist .env (
    copy .env.example .env >nul
    echo 已生成 .env，请先用记事本填好 DEEPSEEK_API_KEY 等配置后再运行本脚本。
    notepad .env
    pause
    exit /b
)
.venv\Scripts\python bot.py
pause
