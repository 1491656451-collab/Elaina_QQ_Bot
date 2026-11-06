@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 识图“底片比对”离线测试（11/06，识图 agent）：她库里的图压缩、裁边后还能不能对上；库里没有的图会不会被误认成她
rem 不调模型、不花钱、不碰 data。结果在 docs\识图底片比对测试\
".venv\Scripts\python" tools\selfmatch_test.py
pause
