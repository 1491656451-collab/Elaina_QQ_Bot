"""识图“实物照片”新旧对比测试（10/25，识图 agent）

想知道的事：vision.py 10/25 的两处改动有没有用、会不会误伤
- A：拍的是实物（手办、人偶、玩偶、立牌……）时，描述开头写明“一尊人偶：……”（“手办”一律换成“人偶”）
- C：伊蕾娜分数 ≥ 0.1、又没认成她时算“拿不准”：不说“（画的不是你）”，改说“（看不太出是不是你）”，
  也不把别的角色名告诉看图模型

用的图：
- 实物照片：tools\\识图实物照片\\ 里你放的图，每张新旧各问 2 次
- 插画（检查不会被误写成实物，“画的不是你”该在的还在）：《识图抽查-1022》里分数偏低的 18 张她的图 + 2 张对照，
  再加 tools\\识图对照图\\ 里别的银发角色，每张新旧各问 1 次

“新版”就是现在的 plugins\\roleplay_chat\\vision.py；“旧版”是同一个文件把提示词换回 10/25 以前的写法、关掉 C。
走的是线上同一套 describe()（本机认人 → 拼提示 → DeepSeek 看图 → 加尾巴），不碰 data\\，不连 QQ。
结果写到 docs\\识图实物照片测试\\实物照片_日期_时间.md。用法：双击 bot\\识图实物照片测试.bat
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
PHOTO_DIR = BOT / "tools" / "识图实物照片"
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

# 10/25 以前的写法（从旧版 vision.py 原样抄来）
OLD_DESCRIBE = (
    "用简短的中文描述这张图片，不超过 50 字。"
    "写出主要内容；图里有文字就写出关键文字；如果是表情包，说明它表达的情绪或意思。"
    "画出来的人物写成“画中的少女”“画中人”之类，不要用“动漫”“动画”“二次元”“番剧”“角色”这些词。"
    "只输出描述本身，不要加“图片中”“这是一张”之类的开头。"
)
OLD_SELF = (
    "\n已确认：画中人物是伊蕾娜（灰色长发、黑色三角帽和长袍的旅行魔女）。"
    "描述请以“伊蕾娜本人的画像”开头，再写她在画里的样子、动作、表情，有配字就写出配字。"
    "例如：伊蕾娜本人的画像，戴着魔女帽一脸嫌弃，配字“嫌弃”。"
)
OLD_MAYBE = (
    "\n画中人物有可能是伊蕾娜（灰色长发、琉璃色眼睛；平时戴黑色三角帽、穿黑色长袍，"
    "但画里也可能穿便服、不戴帽子）。请仔细看：发色、眼睛和整体样子都和她一致，描述就以“伊蕾娜本人的画像”开头；"
    "不像的话就正常描述，不要提伊蕾娜。"
)
OLD_PERSON = re.compile(r"画中|少女|少年|女孩|男孩|女生|男生|人物|女子|男子|姑娘")
PHOTO_WORDS = re.compile(r"人偶|玩偶|手办|立牌|雕像|实物|模型|公仔|粘土人")


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


def make_old(vis):
    """同一份 vision.py，把提示词换回旧的、关掉 C"""
    vis.DESCRIBE_PROMPT, vis.SELF_HINT, vis.MAYBE_HINT = OLD_DESCRIBE, OLD_SELF, OLD_MAYBE
    vis._PERSON_RE = OLD_PERSON
    vis._REWRITE = [r for r in vis._REWRITE if "手办" not in r[0].pattern and "公仔" not in r[0].pattern
                    and "粘土人" not in r[0].pattern]
    vis.Vision._is_unsure = staticmethod(lambda entry: False)
    vis.Vision._stale = lambda self, entry: False
    return vis


async def look(vis, tagger, client, model, raw: bytes) -> tuple[str, float, list[str]]:
    v = vis.Vision(client, model, Path(tempfile.mkdtemp()) / "vc.json", tagger=tagger, maybe=0.35)

    async def get_raw(_data, _key):
        return raw
    v._get_raw = get_raw
    d = await v.describe({"file": "x"}) or "（没有描述）"
    e = v._cache.get("x", {})
    return d, float(e.get("s", 0) or 0), list(e.get("c", []))


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

    photos = sorted(f for f in PHOTO_DIR.iterdir() if f.suffix.lower() in IMG_EXT) if PHOTO_DIR.exists() else []
    negs = sorted(f for f in NEG_DIR.iterdir() if f.suffix.lower() in IMG_EXT) if NEG_DIR.exists() else []
    items = [("实物", f"P{i + 1}", f, 2) for i, f in enumerate(photos)]
    items += [("插画·是她", f"#{no}", BOT / sub / name, 1) for sub, name, no in POSITIVES]
    items += [("插画·不是她", f"N{i + 1}", f, 1) for i, f in enumerate(negs)]
    if not photos:
        print(f"{PHOTO_DIR} 里没有图，只测插画那部分。")
    calls = sum(r for *_, r in items) * 2
    print(f"实物 {len(photos)} 张、插画 {len(items) - len(photos)} 张，新旧两版共约 {calls} 次看图调用")

    rows = []
    for kind, no, path, runs in items:
        if not path.exists():
            rows.append((kind, no, path.name, None, [], ["（文件不存在）"], ["（文件不存在）"]))
            continue
        try:
            raw = new.shrink(path.read_bytes())
        except Exception as e:  # noqa: BLE001
            rows.append((kind, no, path.name, None, [], [f"（缩图失败：{e}）"], []))
            continue
        olds, news, score, chars = [], [], None, []
        for _ in range(runs):
            d, score, chars = await look(old, tagger, client, model, raw)
            olds.append(d)
            d, score, chars = await look(new, tagger, client, model, raw)
            news.append(d)
        rows.append((kind, no, path.name, score, chars, olds, news))
        print(f"{kind} {no:>4}  分数 {score:.3f}  新：{news[0][:50]}")

    cost = sum(TOK[k] * PRICE[k] for k in TOK) / 1e6

    def tail(d: str) -> str:
        return "看不太出" if d.endswith("（看不太出是不是你）") else "不是你" if d.endswith("（画的不是你）") else "—"

    ph = [r for r in rows if r[0] == "实物" and r[6]]
    ph_old = sum(sum(bool(PHOTO_WORDS.search(d)) for d in r[5]) for r in ph)
    ph_new = sum(sum(bool(PHOTO_WORDS.search(d)) for d in r[6]) for r in ph)
    ph_n = sum(len(r[6]) for r in ph)
    il = [r for r in rows if r[0] != "实物" and r[6]]
    il_wrong = [r for r in il if any(PHOTO_WORDS.search(d) for d in r[6])]
    neg = [r for r in rows if r[0] == "插画·不是她" and r[6]]
    neg_keep = sum(all(tail(d) == "不是你" for d in r[6]) for r in neg)
    neg_had = sum(all(tail(d) == "不是你" for d in r[5]) for r in neg)
    lines = [
        f"# 识图实物照片测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 模型 `{model}`；本机认人用压缩版、门槛 0.72，和服务器一样。新版 = 现在的 `vision.py`，旧版 = 换回 10/25 以前的提示词、关掉“拿不准”。",
        f"> 输入 {TOK['hit'] + TOK['miss']} token（命中缓存 {TOK['hit']}），输出 {TOK['out']} token，按空闲价约 {cost:.3f} 元。",
        "",
        "## 汇总",
        "",
        f"- 实物照片（{len(ph)} 张、{ph_n} 次）：描述里写明是人偶 / 玩偶等实物的，旧版 {ph_old} 次，**新版 {ph_new} 次**",
        f"- 插画（{len(il)} 张）：新版被误写成实物的 **{len(il_wrong)} 张**"
        + ("（" + "、".join(r[1] for r in il_wrong) + "）" if il_wrong else ""),
        f"- 别的角色的插画（{len(neg)} 张）：“（画的不是你）”旧版 {neg_had} 张都有，新版 {neg_keep} 张还在",
        "",
        "## 逐张",
        "",
        "| 类别 | 编号 | 文件 | 伊蕾娜分数 | 认出的别的角色 | 旧版 | 新版 |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, no, name, score, chars, olds, news in rows:
        s = "—" if score is None else f"{score:.3f}"
        c = "、".join(chars) or "—"
        lines.append(f"| {kind} | {no} | `{name[:28]}` | {s} | {c} | {'<br>'.join(olds)} | {'<br>'.join(news)} |")
    out_dir = BOT.parent / "docs" / "识图实物照片测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"实物照片_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln in lines[7:10]:
        print(ln)
    print(f"花费约 {cost:.3f} 元。结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
