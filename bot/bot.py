import os
import platform
import sys
from pathlib import Path

# Windows 上 Python 3.12+ 查系统版本会走 WMI；WMI 服务卡住时，import onnxruntime（识图用）会一直卡在这里。
# 让它跳过 WMI，直接用老办法（sys.getwindowsversion）取版本，结果一样。见《问题排查记录-启动卡住.md》
if sys.platform == "win32" and hasattr(platform, "_wmi_query"):
    def _skip_wmi(*args, **kwargs):
        raise OSError("跳过 WMI 查询")
    platform._wmi_query = _skip_wmi

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 不管从哪个目录启动（比如服务器上用 systemd 常驻），都按 bot 目录找 .env、plugins 和 data
os.chdir(Path(__file__).resolve().parent)

nonebot.init()
nonebot.get_driver().register_adapter(OneBotV11Adapter)
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
