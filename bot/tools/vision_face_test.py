"""识图“表情看准”新旧对比测试（11/17，识图 agent）

起因：K3（闭眼张嘴大笑、托下巴的得意脸）被看图模型写成“张嘴吐舌……无奈又嫌弃”，她接话意思全反；动图 G5、G9 也是同类。
第 2 版（11/17 15:1x）：本机认人顺便读出表情标签（闭眼、张嘴、笑、汗滴、眼泪……）告诉看图模型，提示词加一句 FACE_RULE（情绪合起来看）。
这个测试拿同一批图，不加（旧）和加了（新）各描述一次，比较：
1. 要修的 K3、G5、G9、G20 改过来没有
2. 本来就对的负面表情（嫌弃、无语、生气、哭）还写不写得出来，会不会全变成“笑”
3. 认人（“伊蕾娜本人的画像”开头）、聊天截图、实物照片的写法变没变
4. 描述长度变没变

用的图（编号固定，方便对照）：
- K：tools\\表情包对比\\ 里的图（聊天 agent 表情包回放用的）
- G：动图，编号和《动图测试报告-1117》一样（data\\stickers、tools\\底片比对对照图、tools\\识图动图 里的动图按顺序）
- E：表情库 data\\stickers 里的静图（表格里附上表情库自带的标签，方便看情绪对不对）
- D：tools\\底片比对对照图 里别的静态表情包
- S / P：聊天截图、实物照片各两张，看别的规则有没有被带歪
走的是线上同一套 describe()，不碰 data\\，不连 QQ。结果写到 docs\\识图表情测试\\表情_日期_时间.md。
用法：双击 bot\\识图表情测试.bat
复测几张（只跑新版，每张看几次，看结果稳不稳）：python tools\\vision_face_test.py --only K3,G14 --repeat 3
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
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
STATIC_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
SHOTS = ["tools/识图截图/Screenshot_20261106_204627_com_tencent_mobileqq_.jpg",
         "tools/识图截图/Screenshot_20261106_210122_com_tencent_mobileqq_.jpg"]
PHOTOS = ["tools/识图实物照片/IMG_20261024_131642.jpg",
          "tools/识图实物照片/4c91dbbb3d8d48d389b943cb0ddbefe9.jpg"]
FOCUS = {"K3": "闭眼张嘴大笑、托下巴，得意", "G5": "闭眼张嘴、一脸无奈（嫌弃）", "G9": "鼓腮、嘟嘴",
         "G20": "闭眼微笑 → 眼眶含泪、还在笑（笑着流泪）"}
PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}   # 元 / 百万 token，空闲价
NEG_RE = re.compile(r"嫌弃|无语|不屑|鄙视|白眼|生气|愤怒|恼|怒|哭|泪|委屈|难过|伤心|皱眉|无奈|不满|撇嘴")
POS_RE = re.compile(r"笑|开心|得意|高兴|愉快|欢快|满足|害羞|兴奋")
TONGUE_RE = re.compile(r"吐舌|伸舌")
TEAR_RE = re.compile(r"泪|哭")
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
        p.__path__ = [str(PLUGIN)]
        sys.modules[pkg] = p
        b = types.ModuleType(pkg + ".budget")
        b.track = track
        sys.modules[pkg + ".budget"] = b
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


async def look(vis, tagger, client, model, path: Path, tmp: Path) -> str:
    v = vis.Vision(client, model, tmp / f"vc_{time.time_ns()}.json", tagger=tagger, maybe=0.35)
    v.cache_file = tmp / "vision_cache.json"     # 底片比对的指纹存在临时文件夹里，两版共用
    data = path.read_bytes()
    v._download_sync = lambda _url: data
    d = await v.describe({"url": "http://local/" + path.name, "file": path.name}) or "（没有描述）"
    ent = next(iter(v._cache.values()), {}) if v._cache else {}
    return d, "、".join(ent.get("e") or [])


def collect():
    """[(编号, 组, 路径, 表情库标签)]"""
    from PIL import Image
    items = []
    k_dir = BOT / "tools" / "表情包对比"
    if k_dir.is_dir():
        for f in sorted(k_dir.iterdir()):
            if re.fullmatch(r"K\d+", f.stem) and f.suffix.lower() in STATIC_EXT + (".gif",):
                items.append((f.stem, "K 表情包回放", f, ""))
    gi = 0
    for d in GIF_DIRS:
        folder = BOT / d
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in (".gif", ".webp"):
                continue
            try:
                n = getattr(Image.open(f), "n_frames", 1)
            except Exception:  # noqa: BLE001
                continue
            if n >= 2:
                gi += 1
                items.append((f"G{gi}", "G 动图", f, ""))
    tags = {}
    try:
        idx = json.loads((BOT / "data" / "stickers" / "index.json").read_text(encoding="utf-8"))
        for it in idx.get("items", {}).values():
            tags[it.get("file", "")] = "、".join(it.get("tags") or [])
    except (OSError, ValueError, AttributeError):
        pass
    animated = {str(it[2]) for it in items}
    for prefix, group, d in (("E", "E 表情库静图", "data/stickers"), ("D", "D 别的表情包", "tools/底片比对对照图")):
        folder = BOT / d
        if not folder.is_dir():
            continue
        i = 0
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in STATIC_EXT + (".gif",) or str(f) in animated:
                continue
            i += 1
            items.append((f"{prefix}{i}", group, f, tags.get(f.name, "")))
    for prefix, group, lst in (("S", "S 聊天截图", SHOTS), ("P", "P 实物照片", PHOTOS)):
        for i, rel in enumerate(lst, 1):
            f = BOT / rel
            if f.is_file():
                items.append((f"{prefix}{i}", group, f, ""))
    return items


async def recheck(new, tagger, client, model, items, repeat: int) -> None:
    """只跑新版，每张看 repeat 次（每次都是新缓存），看描述稳不稳"""
    tmp = Path(tempfile.mkdtemp())
    lines = [f"# 识图表情复测（{datetime.now():%m-%d %H:%M}）", "",
             f"> 只跑新版（现在的 `vision.py`），每张看 {repeat} 次。", "",
             "| 编号 | 本机表情 | 第几次 | 描述 |", "|---|---|---|---|"]
    for no, _group, f, _tag in items:
        for k in range(1, repeat + 1):
            d, ex = await look(new, tagger, client, model, f, tmp)
            lines.append(f"| {no} | {ex} | {k} | {d} |")
            print(f"{no:<4} 第{k}次：{d[:50]}")
    out_dir = BOT.parent / "docs" / "识图表情测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"复测_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"结果在 {out}")


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
    if not getattr(new, "FACE_RULE", "") or new.FACE_RULE not in new.DESCRIBE_PROMPT:
        print("vision.py 里没有 FACE_RULE，不是 11/17 改过的版本，测不了。")
        return
    old.DESCRIBE_PROMPT = old.DESCRIBE_PROMPT.replace(old.FACE_RULE, "")   # 旧版：去掉“情绪合起来看”这一句
    old.EXPR_ON = False                                                     # 旧版：不告诉看图模型表情特征
    tg = load("vnew", "tagger")
    tagger = tg.Tagger(BOT / "data" / "models", "SmilingWolf/wd-swinv2-tagger-v3-int8", [],
                       threshold=0.72, threads=1, auto_download=False)
    tagger._load()
    if not tagger.ready:
        print("识图模型没加载起来，测不了（data\\models 里要有 wd-swinv2-tagger-v3-int8）。")
        return

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=base, timeout=60)

    items = collect()
    only = next((a.split("=", 1)[-1] for a in sys.argv[1:] if a.startswith("--only")), "")
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 0
    if only:
        want = {x.strip() for x in only.split(",") if x.strip()}
        items = [it for it in items if it[0] in want]
    if repeat:
        await recheck(new, tagger, client, model, items, repeat)
        return
    print(f"图 {len(items)} 张，新旧各看一次，约 {len(items) * 2} 次看图调用")
    tmp = Path(tempfile.mkdtemp())
    rows = []
    for no, group, f, tag in items:
        try:
            do, _ = await look(old, tagger, client, model, f, tmp)
            dn, ex = await look(new, tagger, client, model, f, tmp)
        except Exception as e:  # noqa: BLE001
            do = dn = f"（出错：{e}）"
            ex = ""
        rows.append((no, group, f.name, tag, do, dn, ex))
        print(f"{no:<4} 旧：{do[:30]}  新：{dn[:30]}")

    cost = (TOK["hit"] * PRICE["hit"] + TOK["miss"] * PRICE["miss"] + TOK["out"] * PRICE["out"]) / 1e6

    def cnt(rx, col, group=None):
        return sum(bool(rx.search(r[col])) for r in rows if group is None or r[1].startswith(group))

    head_diff = [r[0] for r in rows if r[4].startswith("伊蕾娜本人的画像") != r[5].startswith("伊蕾娜本人的画像")]
    tongue_new = [r[0] for r in rows if TONGUE_RE.search(r[5])]
    tear_gain = [r[0] for r in rows if TEAR_RE.search(r[5]) and not TEAR_RE.search(r[4])]
    tear_lost = [r[0] for r in rows if TEAR_RE.search(r[4]) and not TEAR_RE.search(r[5])]
    neg_lost = [r[0] for r in rows if NEG_RE.search(r[4]) and not NEG_RE.search(r[5])]
    neg_gain = [r[0] for r in rows if NEG_RE.search(r[5]) and not NEG_RE.search(r[4])]
    shot_bad = [r[0] for r in rows if r[1].startswith("S") and not re.match(r"(群聊|私聊)截图", r[5])]
    stickers = [r for r in rows if r[1][0] in "KGED"]
    len_old = sum(len(r[4]) for r in stickers) / max(1, len(stickers))
    len_new = sum(len(r[5]) for r in stickers) / max(1, len(stickers))
    lines = [
        f"# 识图表情测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 模型 `{model}`；新版 = 现在的 `vision.py`（告诉看图模型本机读出的表情特征 + FACE_RULE），旧版 = 同一个文件把这两样关掉。各看一次。",
        f"> {TOK['calls']} 次调用，输入 {TOK['hit'] + TOK['miss']} token（命中缓存 {TOK['hit']}），输出 {TOK['out']} token，按空闲价约 {cost:.3f} 元。",
        "",
        "## 汇总",
        "",
        f"- 图 {len(rows)} 张（K {sum(r[1][0] == 'K' for r in rows)}、G {sum(r[1][0] == 'G' for r in rows)}、"
        f"E {sum(r[1][0] == 'E' for r in rows)}、D {sum(r[1][0] == 'D' for r in rows)}、"
        f"S {sum(r[1][0] == 'S' for r in rows)}、P {sum(r[1][0] == 'P' for r in rows)}）",
        f"- 写了“吐舌”的：旧 {cnt(TONGUE_RE, 4)} 张 → 新 {cnt(TONGUE_RE, 5)} 张" + ("（" + "、".join(tongue_new) + "）" if tongue_new else ""),
        f"- 写了眼泪、哭的：旧 {cnt(TEAR_RE, 4)} 张 → 新 {cnt(TEAR_RE, 5)} 张；新多出来的："
        + ("、".join(tear_gain) or "无") + "；新没了的：" + ("、".join(tear_lost) or "无"),
        f"- 写了负面情绪词（嫌弃、无语、生气、哭、皱眉……）的：旧 {cnt(NEG_RE, 4)} 张 → 新 {cnt(NEG_RE, 5)} 张",
        f"  - 旧有新没有（要人眼看：是修好了，还是该写的没写）：" + ("、".join(neg_lost) or "无"),
        f"  - 旧没有新有：" + ("、".join(neg_gain) or "无"),
        f"- 写了正面情绪词（笑、开心、得意……）的：旧 {cnt(POS_RE, 4)} 张 → 新 {cnt(POS_RE, 5)} 张",
        f"- “伊蕾娜本人的画像”开头新旧不一样的：**{len(head_diff)} 张**" + ("（" + "、".join(head_diff) + "）" if head_diff else ""),
        f"- 聊天截图新版没以“群聊截图 / 私聊截图”开头的：**{len(shot_bad)} 张**" + ("（" + "、".join(shot_bad) + "）" if shot_bad else ""),
        f"- 表情包平均字数：旧 {len_old:.0f} → 新 {len_new:.0f}",
        "",
        "## 重点看的几张",
        "",
        "| 编号 | 实际是什么 | 旧 | 新 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        if r[0] in FOCUS:
            lines.append(f"| {r[0]} | {FOCUS[r[0]]} | {r[4]} | {r[5]}（本机表情：{r[6] or '无'}） |")
    lines += ["", "## 逐张", "", "“本机表情”是本机识别读出、告诉新版看图模型的表情特征（动图只看第一帧）。", "",
              "| 编号 | 组 | 文件 | 表情库标签 | 本机表情 | 旧 | 新 |", "|---|---|---|---|---|---|---|"]
    for no, group, name, tag, do, dn, ex in rows:
        lines.append(f"| {no} | {group[2:]} | `{name[:22]}` | {tag} | {ex} | {do} | {dn} |")
    out_dir = BOT.parent / "docs" / "识图表情测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"表情_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln in lines[7:17]:
        print(ln)
    print(f"花费约 {cost:.3f} 元。结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
