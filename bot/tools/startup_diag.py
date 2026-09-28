"""启动诊断：和 bot.py 一样加载插件，但不连 QQ；卡住时每 30 秒把“卡在哪一行”写进 data\\logs\\启动诊断.txt

用法：双击 bot\\启动诊断.bat，等它自己结束（最多约 1 分半），然后把 data\\logs\\启动诊断.txt 发给我
"""
import faulthandler
import os
import sys
import time
from pathlib import Path
import platform

if sys.platform == "win32" and hasattr(platform, "_wmi_query"):   # 和 bot.py 一样跳过 WMI
    def _skip_wmi(*args, **kwargs):
        raise OSError("跳过 WMI 查询")
    platform._wmi_query = _skip_wmi

BOT = Path(__file__).resolve().parent.parent
os.chdir(BOT)
sys.path.insert(0, str(BOT))
OUT = BOT / "data" / "logs" / "启动诊断.txt"
OUT.parent.mkdir(parents=True, exist_ok=True)
f = open(OUT, "w", encoding="utf-8", buffering=1)
T0 = time.time()


def step(msg: str):
    line = f"[{time.time() - T0:6.1f}s] {msg}"
    print(line, flush=True)
    f.write(line + "\n")
    f.flush()


# 卡住时：90 秒还没加载完，就把所有线程的调用栈写进文件，然后直接退出（卡在系统调用里也能退）
faulthandler.dump_traceback_later(90, repeat=False, file=f, exit=True)
step(f"Python {sys.version.split()[0]}，{sys.executable}")

for name in ("pydantic", "nonebot", "nonebot.adapters.onebot.v11", "openai", "httpx", "numpy", "PIL", "onnxruntime"):
    step(f"导入 {name} ……")
    __import__(name)
    step(f"导入 {name} 完成")

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter  # noqa: E402

step("nonebot.init() ……")
nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
step("nonebot.init() 完成；开始加载插件 plugins ……")


nonebot.load_plugins("plugins")
faulthandler.cancel_dump_traceback_later()
step("插件加载完成，启动过程没有卡住（诊断不连 QQ，到此结束）")
f.close()
