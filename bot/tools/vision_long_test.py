"""识图描述新旧对比测试（11/05，识图 agent）

想知道的事：vision.py 11/05 改了看图的要求，两处：
- 放宽：一般还是 50 字以内，文字多、内容复杂的图（聊天截图、长段配文、多格漫画、通知公告）可以写到 100 字
- 不猜用意：表情、动作、配字、画里人物的情绪照写，但不替发图的人猜用意（“在调侃对方”“在敷衍”之类）
要确认：
1. 复杂的图：新版把关键的文字写全了，没有写到一半被截断
2. 简单的图（表情包、插画）：新版还是 50 字左右，没有跟着变长
3. 新版描述里“调侃对方”“表达对……”这类猜用意的说法变少了
4. 认人那套（“伊蕾娜本人的画像”开头、“（画的不是你）”结尾）不受影响

用的图：
- 复杂的图：tools\\识图复杂图\\ 里你放的图，每张新旧各问 2 次
- 简单的图：《识图抽查-1022》那 20 张她的表情和插画 + tools\\识图对照图\\ 里别的角色，每张新旧各问 1 次

“新版”就是现在的 plugins\\roleplay_chat\\vision.py；“旧版”是同一个文件把看图要求整段换回 11/05 以前的写法、
输出上限换回 120 token、截到 70 字。走的是线上同一套 describe()，不碰 data\\，不连 QQ。
结果写到 docs\\识图复杂图测试\\复杂图_日期_时间.md。用法：双击 bot\\识图复杂图测试.bat
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
LONG_DIR = BOT / "tools" / "识图复杂图"
NEG_DIR = BOT / "tools" / "识图对照图"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}   # 元 / 百万 token，空闲价
POSITIVES = [
    ("data/stickers", "20a05a4e4b11fe75.jpg", 1), ("data/stickers", "7e088ced9b420132.jpg", 2),
    ("data/stickers", "f88f963a639b81cb.jpg", 3), ("data/stickers", "1c229071197c3aa3.jpg", 4),
    ("data/stickers", "c1e6b3afcdc7888d.jpg", 5), ("data/stickers", "3d99ac3e14b38edb.jpg", 6),
    ("data/stickers", "3e302a75d8eacfb1.jpg", 7), ("data/stickers", "123cfda25dfd2c09.jpg", 8),
    ("qzone/images", "illust_116045283_20240520_224005.jpg", 9), ("data/stickers", "ca44c41fc3dc13a6.gif", 10),
    ("data/stickers", "7b931a7b4ecb1111.jpg", 11), ("data/stickers", "ff47854e028845f9.jpg", 12),
    ("data/stickers", "3f462e790ab903c8.jpg", 13), ("data/stickers", "9304614b216d2422.gif", 14),
    ("data/stickers", "071af60c977af60a.jpg", 15), ("data/stickers", "289189c7febd3e82.gif", 16),
    ("qzone/images", "illust_136837891_20260330_025509.png", 17), ("data/stickers", "07628c727e6f068d.jpg", 18),
    ("qzone/images", "illust_138760642_20260330_025804.png", 19), ("data/stickers", "85344b9c0b51bddc.jpg", 27),
]


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


def load(pkg: str, name: str):
    """按文件加载插件里的模块；vision.py 里的 `from . import budget` 用一个只记 token 的替身接住"""
    if pkg not in sys.modules:
        p = types.ModuleType(pkg)
        p.__path__ = []
        sys.modules[pkg] = p
        b = types.ModuleType(pkg + ".budget")
        b.track = track
        sys.modules[pkg + ".budget"] = b
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


TOK = {"hit": 0, "miss": 0, "out": 0}


def track(resp, *_a, **_k):
    u = getattr(resp, "usage", None)
    if u is None:
        return 0.0
    hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
    TOK["hit"] += hit
    TOK["miss"] += (getattr(u, "prompt_tokens", 0) or 0) - hit
    TOK["out"] += getattr(u, "completion_tokens", 0) or 0
    time.sleep(0.3)
    return 0.0


# 11/05 以前的看图要求（从旧版 vision.py 原样抄来）
OLD_DESCRIBE = (
    "用简短的中文描述这张图片，不超过 50 字。"
    "写出主要内容；图里有文字就写出关键文字；如果是表情包，说明它表达的情绪或意思。"
    "画出来的人物写成“画中的少女”“画中人”之类，不要用“动漫”“动画”“二次元”“番剧”“角色”这些词。"
    "如果拍的是实物（手办、人偶、玩偶、立牌、雕像之类），开头写明，比如“一尊人偶：……”“一个玩偶：……”，"
    "里面的人物写成“人偶”，不要写成“画中的少女”。"
    "只输出描述本身，不要加“图片中”“这是一张”之类的开头。"
)
# 替发图的人猜用意的说法（只用来数个数，不精确，逐张表里要人眼再看）
INTENT_RE = re.compile(r"调侃|讽刺|嘲讽|敷衍|示好|撒娇求|求安慰|暗示|表达对|向对方|对方|想让|在问|阴阳|挑衅|催促")


def make_old(vis):
    """同一份 vision.py，换回 11/05 以前的写法：旧的看图要求、最多 120 token、截到 70 字"""
    vis.DESCRIBE_PROMPT = OLD_DESCRIBE
    vis._clip_desc = lambda d: d[:70]
    return vis


async def look(vis, tagger, client, model, raw: bytes, max_tokens: int) -> str:
    v = vis.Vision(client, model, Path(tempfile.mkdtemp()) / "vc.json", tagger=tagger, maybe=0.35,
                   max_tokens=max_tokens)

    async def get_raw(_data, _key):
        return raw
    v._get_raw = get_raw
    d = await v.describe({"file": "x"}) or "（没有描述）"
    look.score = float(v._cache.get("x", {}).get("s", 0) or 0)
    return d


def core(d: str) -> str:
    """描述本身（去掉程序加的尾巴）"""
    for t in ("（画的不是你）", "（看不太出是不是你）"):
        d = d.removesuffix(t)
    return d


def core_len(d: str) -> int:
    return len(core(d))


async def main() -> None:
    env = read_env()
    key = env.get("DEEPSEEK_API_KEY", "")
    if not key or "在这里" in key:
        print("没在 .env 里找到 DEEPSEEK_API_KEY，测不了。")
        return
    base = env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = env.get("VISION_MODEL") or "deepseek-flash"

    new = load("vnew", "vision")
    old = make_old(load("vold", "vision"))
    tg = load("vnew", "tagger")
    tagger = tg.Tagger(BOT / "data" / "models", "SmilingWolf/wd-swinv2-tagger-v3-int8", [],
                       threshold=0.72, threads=1, auto_download=False)   # 和服务器一样：压缩版、门槛 0.72
    tagger._load()
    if not tagger.ready:
        print("识图模型没加载起来，测不了（data\\models 里要有 wd-swinv2-tagger-v3-int8）。")
        return

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=base, timeout=60)

    longs = sorted(f for f in LONG_DIR.iterdir() if f.suffix.lower() in IMG_EXT) if LONG_DIR.exists() else []
    negs = sorted(f for f in NEG_DIR.iterdir() if f.suffix.lower() in IMG_EXT) if NEG_DIR.exists() else []
    items = [("复杂", f"L{i + 1}", f, 2) for i, f in enumerate(longs)]
    items += [("简单·是她", f"#{no}", BOT / sub / name, 1) for sub, name, no in POSITIVES]
    items += [("简单·不是她", f"N{i + 1}", f, 1) for i, f in enumerate(negs)]
    if not longs:
        print(f"{LONG_DIR} 里没有图，只测简单的图那部分。")
    calls = sum(r for *_, r in items) * 2
    print(f"复杂的图 {len(longs)} 张、简单的图 {len(items) - len(longs)} 张，新旧两版共约 {calls} 次看图调用")

    rows = []
    for kind, no, path, runs in items:
        if not path.exists():
            rows.append((kind, no, path.name, None, ["（文件不存在）"], []))
            continue
        try:
            raw = new.shrink(path.read_bytes())
        except Exception as e:  # noqa: BLE001
            rows.append((kind, no, path.name, None, [f"（缩图失败：{e}）"], []))
            continue
        olds, news = [], []
        for _ in range(runs):
            olds.append(await look(old, tagger, client, model, raw, 120))
            news.append(await look(new, tagger, client, model, raw, new.Vision.__init__.__kwdefaults__["max_tokens"]))
        rows.append((kind, no, path.name, look.score, olds, news))
        print(f"{kind} {no:>4}  旧 {core_len(olds[0]):>3} 字 → 新 {core_len(news[0]):>3} 字  {news[0][:40]}")

    cost = sum(TOK[k] * PRICE[k] for k in TOK) / 1e6

    def stat(rs, i):
        ls = [core_len(d) for r in rs for d in r[i]]
        return (sum(ls) / len(ls) if ls else 0, max(ls) if ls else 0, ls)

    def same_head_tail(o: str, n: str) -> bool:
        """认人的结论一样：开头是不是“伊蕾娜本人的画像”、尾巴是哪种"""
        tail = lambda d: "不是你" if d.endswith("（画的不是你）") else "看不太出" if d.endswith("（看不太出是不是你）") else ""  # noqa: E731
        return o.startswith("伊蕾娜本人的画像") == n.startswith("伊蕾娜本人的画像") and tail(o) == tail(n)

    lg = [r for r in rows if r[0] == "复杂" and r[5]]
    sm = [r for r in rows if r[0] != "复杂" and r[5]]
    lo, ln = stat(lg, 4), stat(lg, 5)
    so, sn = stat(sm, 4), stat(sm, 5)
    cut_old = sum(len(d) >= 70 for r in lg for d in r[4])
    cut_new = sum(core(d).endswith("……") for r in lg for d in r[5])
    sm_over = [r[1] for r in sm if any(core_len(d) > 60 for d in r[5])]
    diff = [r[1] for r in sm if not same_head_tail(r[4][0], r[5][0])]
    io_ = sum(bool(INTENT_RE.search(core(d))) for r in rows if r[5] for d in r[4])
    in_ = sum(bool(INTENT_RE.search(core(d))) for r in rows if r[5] for d in r[5])
    n_all = sum(len(r[5]) for r in rows if r[5])
    lines = [
        f"# 识图复杂图测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 模型 `{model}`；本机认人用压缩版、门槛 0.72，和服务器一样。新版 = 现在的 `vision.py`，旧版 = 换回“不超过 50 字、最多 120 token、截到 70 字”。",
        f"> 输入 {TOK['hit'] + TOK['miss']} token（命中缓存 {TOK['hit']}），输出 {TOK['out']} token，按空闲价约 {cost:.3f} 元。",
        "",
        "## 汇总",
        "",
        f"- 复杂的图（{len(lg)} 张、{len(lo[2])} 次）：平均字数 旧 {lo[0]:.0f} → **新 {ln[0]:.0f}**，最长 旧 {lo[1]} → 新 {ln[1]}；"
        f"旧版写满 70 字被截断 {cut_old} 次，新版超过上限被截断 {cut_new} 次",
        f"- 简单的图（{len(sm)} 张）：平均字数 旧 {so[0]:.0f} → **新 {sn[0]:.0f}**，最长 旧 {so[1]} → 新 {sn[1]}；新版超过 60 字的 {len(sm_over)} 张"
        + ("（" + "、".join(sm_over) + "）" if sm_over else ""),
        f"- 描述里有“调侃”“表达对……”“对方”这类猜用意的说法（按关键词数，要人眼复核）：旧 {io_} 次 → **新 {in_} 次**（共 {n_all} 次）",
        f"- 认人的结论（开头“伊蕾娜本人的画像”、结尾“画的不是你 / 看不太出”）新旧不一样的：{len(diff)} 张"
        + ("（" + "、".join(diff) + "，看图模型每次说法会有出入，要看是不是放宽造成的）" if diff else ""),
        "",
        "## 逐张",
        "",
        "| 类别 | 编号 | 文件 | 伊蕾娜分数 | 旧版（字数） | 新版（字数） |",
        "|---|---|---|---|---|---|",
    ]
    for kind, no, name, score, olds, news in rows:
        s = "—" if score is None else f"{score:.3f}"
        fo = "<br>".join(f"{d}（{core_len(d)}）" for d in olds)
        fn = "<br>".join(f"{d}（{core_len(d)}）" for d in news)
        lines.append(f"| {kind} | {no} | `{name[:28]}` | {s} | {fo} | {fn} |")
    out_dir = BOT.parent / "docs" / "识图复杂图测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"复杂图_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln_ in lines[7:11]:
        print(ln_)
    print(f"花费约 {cost:.3f} 元。结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
