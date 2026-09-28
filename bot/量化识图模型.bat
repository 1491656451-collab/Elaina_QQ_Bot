@echo off
chcp 65001 >nul
cd /d %~dp0
echo 正在安装压缩模型要用的工具（onnx、sympy），只装进机器人的环境，不影响别的……
.venv\Scripts\python tools\nowmi.py -m pip install onnx sympy -i https://mirrors.aliyun.com/pypi/simple/ --prefer-binary
if errorlevel 1 (
  echo 安装失败，请把上面的报错发给 Claude
  pause
  exit /b 1
)
.venv\Scripts\python tools\nowmi.py tools\quantize_tagger.py
pause
