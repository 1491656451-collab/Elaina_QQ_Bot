"""识图“动图看 4 帧”新旧对比测试（11/17，识图 agent）

以前动图只看第一帧：字后来才出现、靠动作表达意思的动图会看漏。现在 vision.py 从动图里均匀取 4 帧拼成 2×2 交给看图模型。
这个测试拿同一批动图，“只看第一帧”（旧）和“看 4 帧”（新）各让看图模型描述一次，比较：
1. 新版有没有写出后面几帧才有的东西（动作变化、后来出现的字）
2. 新版会不会把“拼图”说漏嘴（“四格图”“左上角”“四个画面”这类词）
3. 认人那套（“伊蕾娜本人的画像”开头、“画的不是你”结尾）新旧一样
4. 每张图多花多少 token

用的图：data\\stickers\\ 里的动图、tools\\底片比对对照图\\ 里的动图，以及 tools\\识图动图\\ 里你另放的动图（GIF / 动的 WebP）。
走的是线上同一套 describe()（本机认人 → 底片比对 → 拼提示 → 看图模型），不碰 data\\，不连 QQ。
结果写到 docs\\识图动图测试\\动图_日期_时间.md，同时把拼好的 4 帧图存在同一个文件夹的“拼图”子文件夹里，方便对着看。
用法：双击 bot\\识图动图测试.bat
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
PLUGIN = BOT / "plugins" / "roleplay_chat"
GIF_DIRS = ["data/stickers", "tools/底片比对对照图", "tools/识图动图"]
IMG_EXT = (".gif", ".webp")
PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}   # 元 / 百万 token，空闲价
LEAK_RE = re.compile(r"四格|拼图|拼在|四个画面|四张|左上|右上|左下|右下|画面[1-4１-４]|第[1-4一二三四]格|第[一二三四]个画面")
CHANGE_RE = re.compile(r"随后|然后|接着|之后|变成|变为|转为|逐渐|最后|最终|再|出现|弹出|浮现|冒出|从.{1,8}到")
TOK = {"hit": 0, "miss": 0, "out": 0, "calls": 0}


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in (BOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def track(resp, *_a, **_k):
    u = getattr(resp, "usage", None)
    if u is not None:
        hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        TOK["hit"] += hit
        TOK["miss"] += (getattr(u, "prompt_tokens", 0) or 0) - hit
        TOK["out"] += getattr(u, "completion_tokens", 0) or 0
    TOK["calls"] += 1
    time.sleep(0.3)
    return 0.0


def load(pkg: str, name: str):
    if pkg not in sys.modules:
        p = types.ModuleType(pkg)
        p.__path__ = [str(PLUGIN)]          # 让 vision.py 里的 `from .selfmatch import Library` 找得到
        sys.modules[pkg] = p
        b = types.ModuleType(pkg + ".budget")
        b.track = track
        sys.modules[pkg + ".budget"] = b
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


async def look(vis, tagger, client, model, path: Path, lib_cache: Path) -> tuple[str, int, int]:
    v = vis.Vision(client, model, lib_cache.parent / f"vc_{time.time_ns()}.json", tagger=tagger, maybe=0.35)
    v.cache_file = lib_cache.parent / "vision_cache.json"    # 底片比对的指纹存在临时文件夹里，两版共用
    data = path.read_bytes()
    v._download_sync = lambda _url: data
    t0 = TOK["miss"] + TOK["hit"]
    d = await v.describe({"url": "http://local/" + path.name, "file": path.name}) or "（没有描述）"
    return d, TOK["miss"] + TOK["hit"] - t0, v._sheets.get(path.name) is not None


async def main() -> None:
    env = read_env()
    key = env.get("DEEPSEEK_API_KEY", "")
    if not key or "在这里" in key:
        print("没在 .env 里找到 DEEPSEEK_API_KEY，测不了。")
        return
    base = env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = env.get("VISION_MODEL") or "deepseek-flash"

    new = load("vnew", "vision")
    old = load("vold", "vision")
    old.gif_sheet = lambda raw: None            # 旧版：不拼 4 帧，只看第一帧（小动图原样发）
    tg = load("vnew", "tagger")
    tagger = tg.Tagger(BOT / "data" / "models", "SmilingWolf/wd-swinv2-tagger-v3-int8", [],
                       threshold=0.72, threads=1, auto_download=False)
    tagger._load()
    if not tagger.ready:
        print("识图模型没加载起来，测不了（data\\models 里要有 wd-swinv2-tagger-v3-int8）。")
        return

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=base, timeout=60)
    from PIL import Image

    items = []
    for d in GIF_DIRS:
        folder = BOT / d
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in IMG_EXT:
                continue
            try:
                n = getattr(Image.open(f), "n_frames", 1)
            except Exception:  # noqa: BLE001
                continue
            if n >= 2:
                items.append((d.split("/")[-1], f, n))
    print(f"动图 {len(items)} 张，新旧各看一次，约 {len(items) * 2} 次看图调用")

    out_dir = BOT.parent / "docs" / "识图动图测试"
    sheet_dir = out_dir / "拼图"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    rows = []
    for i, (src, f, n) in enumerate(items, 1):
        do, to, _ = await look(old, tagger, client, model, f, tmp)
        dn, tn, had = await look(new, tagger, client, model, f, tmp)
        try:
            sheet = new.gif_sheet(f.read_bytes())
            if sheet:
                (sheet_dir / f"G{i}_{f.stem[:20]}.jpg").write_bytes(sheet)
        except Exception:  # noqa: BLE001
            pass
        rows.append((f"G{i}", src, f.name, n, do, dn, to, tn, had))
        print(f"G{i:<3} {f.name[:24]:<24} 旧：{do[:28]}  新：{dn[:28]}")

    cost = (TOK["hit"] * PRICE["hit"] + TOK["miss"] * PRICE["miss"] + TOK["out"] * PRICE["out"]) / 1e6
    leak = [r[0] for r in rows if LEAK_RE.search(r[5])]
    ch_old = sum(bool(CHANGE_RE.search(r[4])) for r in rows)
    ch_new = sum(bool(CHANGE_RE.search(r[5])) for r in rows)
    head_diff = [r[0] for r in rows if r[4].startswith("伊蕾娜本人的画像") != r[5].startswith("伊蕾娜本人的画像")]
    tok_old = sum(r[6] for r in rows) / max(1, len(rows))
    tok_new = sum(r[7] for r in rows) / max(1, len(rows))
    no_sheet = [r[0] for r in rows if not r[8]]
    lines = [
        f"# 识图动图测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 模型 `{model}`；新版 = 现在的 `vision.py`（动图取 4 帧拼图），旧版 = 同一个文件关掉拼图（只看第一帧）。各看一次。",
        f"> {TOK['calls']} 次调用，输入 {TOK['hit'] + TOK['miss']} token（命中缓存 {TOK['hit']}），输出 {TOK['out']} token，按空闲价约 {cost:.3f} 元。",
        "",
        "## 汇总",
        "",
        f"- 动图 {len(rows)} 张；新版没拼成 4 帧的：{len(no_sheet)} 张" + ("（" + "、".join(no_sheet) + "）" if no_sheet else ""),
        f"- 描述里写了动作变化、后来出现的东西（“随后”“变成”“出现”这类词，要人眼复核）：旧 {ch_old} 张 → **新 {ch_new} 张**",
        f"- 新版把拼图说漏嘴（“四格”“左上”“四个画面”这类词）：**{len(leak)} 张**" + ("（" + "、".join(leak) + "）" if leak else ""),
        f"- “伊蕾娜本人的画像”开头新旧不一样的：**{len(head_diff)} 张**" + ("（" + "、".join(head_diff) + "）" if head_diff else ""),
        f"- 每张输入 token：旧约 {tok_old:.0f} → 新约 {tok_new:.0f}",
        "",
        f"拼好的 4 帧图在 `docs\\识图动图测试\\拼图\\`，文件名开头的编号和下表对应。",
        "",
        "## 逐张",
        "",
        "| 编号 | 来源 | 文件 | 帧数 | 旧（第一帧） | 新（4 帧） |",
        "|---|---|---|---|---|---|",
    ]
    for no, src, name, n, do, dn, *_ in rows:
        lines.append(f"| {no} | {src} | `{name[:24]}` | {n} | {do} | {dn} |")
    out = out_dir / f"动图_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln in lines[7:12]:
        print(ln)
    print(f"花费约 {cost:.3f} 元。结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
