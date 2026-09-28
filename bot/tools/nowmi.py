"""
先跳过 Windows 的 WMI 查询，再运行别的 Python 程序或模块（比如 pip）。

原因见《问题排查记录-启动卡住.md》：电脑上的 WMI 服务没响应时，pip、onnxruntime 这些库一查系统版本就会一直卡住，
Ctrl+C 也打断不了。bot.py 里已经做了同样的处理，这个小工具给其他脚本用。

用法：
  .venv\\Scripts\\python tools\\nowmi.py -m pip install xxx
  .venv\\Scripts\\python tools\\nowmi.py tools\\某个脚本.py
"""
import platform
import runpy
import sys

if sys.platform == "win32" and hasattr(platform, "_wmi_query"):
    def _skip_wmi(*args, **kwargs):
        raise OSError("跳过 WMI 查询")
    platform._wmi_query = _skip_wmi

args = sys.argv[1:]
if not args:
    sys.exit("用法：nowmi.py -m 模块名 参数… 或 nowmi.py 脚本.py 参数…")
if args[0] == "-m":
    sys.argv = [args[1]] + args[2:]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
