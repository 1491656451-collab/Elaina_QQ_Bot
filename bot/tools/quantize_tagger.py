"""
把本机识图模型（wd-swinv2-tagger-v3，约 445 MB）压成 int8 版（约 120 MB），并对比压缩前后认伊蕾娜准不准。

为什么：2 核 2G 的服务器上，原版模型加载时内存不够，整台机器卡死。int8 版加载时占的内存小很多。

用法：双击 bot\\量化识图模型.bat（在你电脑上运行，不是服务器）。
结果：
- 新模型放在 data\\models\\wd-swinv2-tagger-v3-int8\\（model.onnx、selected_tags.csv、model.onnx.size）
- 对比结果打印在窗口里，同时写到 docs\\识图模型量化对比.txt
不改机器人的任何设置；原来的模型文件也不动。
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
import time
from pathlib import Path

# 电脑上的 WMI 服务没响应时，导入 onnxruntime 会一直卡住（见《问题排查记录-启动卡住.md》），先跳过
if sys.platform == "win32" and hasattr(platform, "_wmi_query"):
    def _skip_wmi(*args, **kwargs):
        raise OSError("跳过 WMI 查询")
    platform._wmi_query = _skip_wmi

BOT = Path(__file__).resolve().parents[1]
MODELS = BOT / "data" / "models"
SRC = MODELS / "wd-swinv2-tagger-v3"
DST = MODELS / "wd-swinv2-tagger-v3-int8"
REPORT = BOT.parent / "docs" / "识图模型量化对比.txt"
MAX_IMAGES = 40

lines: list[str] = []


def say(s: str = "") -> None:
    print(s, flush=True)
    lines.append(s)


def load_tagger_module():
    """只加载 tagger.py 这一个文件，不启动整个机器人插件"""
    path = BOT / "plugins" / "roleplay_chat" / "tagger.py"
    spec = importlib.util.spec_from_file_location("tagger_standalone", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def quantize() -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    DST.mkdir(parents=True, exist_ok=True)
    out = DST / "model.onnx"
    if out.exists():
        say(f"int8 模型已经有了，跳过压缩：{out}")
    else:
        say("开始压缩模型（大约 1～3 分钟）……")
        t0 = time.monotonic()
        tmp = DST / "model.tmp.onnx"
        quantize_dynamic(str(SRC / "model.onnx"), str(tmp),
                         op_types_to_quantize=["MatMul", "Gemm"], weight_type=QuantType.QInt8)
        # 保险：新模型的 IR 版本不能比原版高，否则较旧的 onnxruntime（比如服务器上的）可能读不了
        import onnx
        q = onnx.load(str(tmp))
        src_ir = onnx.load(str(SRC / "model.onnx"), load_external_data=False).ir_version
        if q.ir_version > src_ir:
            q.ir_version = src_ir
            onnx.save(q, str(tmp))
        del q
        tmp.replace(out)
        say(f"压缩完成，用了 {time.monotonic() - t0:.0f} 秒")
    shutil.copyfile(SRC / "selected_tags.csv", DST / "selected_tags.csv")
    # 机器人会用这个文件核对模型大小，对不上会当成“没下完整”删掉，所以写成新模型的真实大小
    (DST / "model.onnx.size").write_text(str(out.stat().st_size))
    say(f"原版：{(SRC / 'model.onnx').stat().st_size / 1024 / 1024:.0f} MB → int8：{out.stat().st_size / 1024 / 1024:.0f} MB")


def pick_images() -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    found: list[Path] = []
    for folder in (BOT / "data" / "stickers", BOT / "qzone" / "images"):
        if folder.exists():
            found += sorted(p for p in folder.iterdir() if p.suffix.lower() in exts)
    # 表情和说说图各取一些
    stick = [p for p in found if "stickers" in p.parts][: MAX_IMAGES // 2]
    other = [p for p in found if "stickers" not in p.parts][: MAX_IMAGES - len(stick)]
    return stick + other


def compare() -> None:
    mod = load_tagger_module()
    taggers = {}
    for name, repo in (("原版", "SmilingWolf/wd-swinv2-tagger-v3"), ("int8", "SmilingWolf/wd-swinv2-tagger-v3-int8")):
        t = mod.Tagger(MODELS, repo, [], threads=2, auto_download=False)
        t0 = time.monotonic()
        t._load()
        say(f"{name} 加载用了 {time.monotonic() - t0:.1f} 秒")
        taggers[name] = t

    images = pick_images()
    if not images:
        say("没找到可以对比的图（data\\stickers 和 qzone\\images 都是空的）")
        return
    say()
    say(f"对比 {len(images)} 张图里“伊蕾娜”的置信度（门槛 0.75，≥门槛才算认出她）：")
    say(f"{'图片':<44}{'原版':>8}{'int8':>8}{'差':>8}")
    diffs, flips = [], []
    counts = {"原版": 0, "int8": 0}
    times = {"原版": 0.0, "int8": 0.0}
    for p in images:
        raw = p.read_bytes()
        scores = {}
        for name, t in taggers.items():
            t0 = time.monotonic()
            try:
                _found, s = t._detect_sync(raw)
            except Exception as e:  # noqa: BLE001
                s = float("nan")
                say(f"  {p.name}：{name} 识别出错：{e}")
            times[name] += time.monotonic() - t0
            scores[name] = s
            if s >= 0.75:
                counts[name] += 1
        d = scores["int8"] - scores["原版"]
        diffs.append(abs(d))
        if (scores["原版"] >= 0.75) != (scores["int8"] >= 0.75):
            flips.append(p.name)
        label = ("stickers/" if "stickers" in p.parts else "images/") + p.name
        say(f"{label[:43]:<44}{scores['原版']:>8.2f}{scores['int8']:>8.2f}{d:>+8.2f}")

    n = len(images)
    say()
    say("—— 汇总 ——")
    say(f"认出是伊蕾娜：原版 {counts['原版']}/{n} 张，int8 {counts['int8']}/{n} 张")
    say(f"置信度平均差 {sum(diffs) / n:.3f}，最大差 {max(diffs):.3f}")
    say(f"结论不一样的（一个认出、一个没认出）：{len(flips)} 张" + (f"：{'、'.join(flips)}" if flips else ""))
    say(f"平均每张用时：原版 {times['原版'] / n:.2f} 秒，int8 {times['int8'] / n:.2f} 秒")


def main() -> None:
    if not (SRC / "model.onnx").exists():
        say(f"找不到原版模型：{SRC / 'model.onnx'}")
        return
    say(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    quantize()
    say()
    compare()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say()
    say(f"结果已保存到 {REPORT}")


if __name__ == "__main__":
    os.chdir(BOT)
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        say(f"出错了：{e}")
        sys.exit(1)
