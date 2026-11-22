"""识图“手画小动物”新旧对比测试（11/22，识图 agent）

起因：《交接-手画小动物读不准-识图-1122》——空间二创那张拓印的两只兔子（tools\\空间二创\\1.jpg，说说原文“兔？！”），
看图模型 3 次都没认出兔子：耳朵的黑描边读成字母“m”，棕兔读成帽子，还写成了“人偶”。
vision.py 改了三处（都能单独关掉做对比）：
① SKETCH_RULE：简笔画、手绘、拓印先数清画了几个、各是什么；黑粗线可能是耳朵轮廓，不一定是字母；纸上的画不算实物
② 本机看出“画里没有人”（no_humans ≥ 0.8）就告诉看图模型，别写成人、人偶；这时不再给表情特征
③ describe(data, caption=...)：发图的人配的字可以一起给看图模型参考（要空间 agent 在 _pic_hint 里传说说原文才会用上）

这个测试比较：
1. 空间二创 4 张：旧（线上现在的样子，不带配文）vs 新（带说说原文）；1.jpg 另外新版带配文、不带配文各看 3 次
2. 回归：实物照片、复杂图、聊天截图、表情包回放的 K 图、别的表情包、表情库静图，新旧（都不带配文）各看一次，
   看“人偶”“画中的少女”“伊蕾娜本人的画像”、截图格式有没有被带歪
走线上同一套 describe()，不碰 data\\，不连 QQ。结果写到 docs\\识图手画测试\\手画_日期_时间.md。
用法：双击 bot\\识图手画测试.bat
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
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
ACG = [("1.jpg", "兔？！", "两只拓印的小兔子：白兔眯眼、耳朵是黑描边（像字母 m）；棕兔张着红嘴"),
       ("2.jpg", "实物到手上了！今天的阳光特别好 照的两个小孩暖暖的很安心（？）", "手里举着画卡，画着两个穿制服的少年靠在一起睡着"),
       ("3.jpg", "我家孤鸟（展示）（展示）", "很逼真的人物画：灰紫短发女孩，毛领皮甲"),
       ("4.jpg", "昼時亦未寝 相与步于卫戍协议。", "拼成聊天记录样子的图，背景是金发人物插画")]
REGRESS = [("P", "实物照片", "tools/识图实物照片"), ("C", "复杂图", "tools/识图复杂图"), ("S", "聊天截图", "tools/识图截图"),
           ("K", "表情包回放", "tools/表情包对比"), ("D", "别的表情包", "tools/底片比对对照图"), ("E", "表情库", "data/stickers")]
REPEAT = 3
PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}   # 元 / 百万 token，空闲价
TOK = {"hit": 0, "miss": 0, "out": 0, "calls": 0}
RABBIT_RE = re.compile(r"兔")
BAD_RE = re.compile(r"人偶|字母|帽")
PERSON_RE = re.compile(r"画中的少女|画中人|少女|女孩|男孩|人物")


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


async def look(vis, tagger, client, model, path: Path, tmp: Path, caption: str | None = None) -> tuple[str, str]:
    v = vis.Vision(client, model, tmp / f"vc_{time.time_ns()}.json", tagger=tagger, maybe=0.35)
    v.cache_file = tmp / "vision_cache.json"
    data = path.read_bytes()
    v._download_sync = lambda _url: data
    d = await v.describe({"url": "http://local/" + path.name, "file": path.name}, caption) if caption is not None \
        else await v.describe({"url": "http://local/" + path.name, "file": path.name})
    ent = next(iter(v._cache.values()), {}) if v._cache else {}
    return d or "（没有描述）", "没人" if ent.get("nh") else ""


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
    if not getattr(new, "SKETCH_RULE", "") or new.SKETCH_RULE not in new.DESCRIBE_PROMPT:
        print("vision.py 里没有 SKETCH_RULE，不是 11/22 改过的版本，测不了。")
        return
    old.DESCRIBE_PROMPT = old.DESCRIBE_PROMPT.replace(old.SKETCH_RULE, "")   # 旧版：去掉简笔画规则
    old.SKETCH_ON = False                                                       # 旧版：不说“画里没有人”，不用配文
    tg = load("vnew", "tagger")
    tagger = tg.Tagger(BOT / "data" / "models", "SmilingWolf/wd-swinv2-tagger-v3-int8", [],
                       threshold=0.72, threads=1, auto_download=False)
    tagger._load()
    if not tagger.ready:
        print("识图模型没加载起来，测不了（data\\models 里要有 wd-swinv2-tagger-v3-int8）。")
        return

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=base, timeout=60)
    tmp = Path(tempfile.mkdtemp())

    # 1. 空间二创
    acg_rows, rabbit_rows = [], []
    for name, cap, truth in ACG:
        f = BOT / "tools" / "空间二创" / name
        if not f.is_file():
            continue
        do, _ = await look(old, tagger, client, model, f, tmp)
        dn, nh = await look(new, tagger, client, model, f, tmp, cap)
        acg_rows.append((name, cap, truth, do, dn, nh))
        print(f"{name} 旧：{do[:30]}  新：{dn[:30]}")
        if name == "1.jpg":
            for k in range(1, REPEAT + 1):
                a, _ = await look(new, tagger, client, model, f, tmp, cap)
                b, _ = await look(new, tagger, client, model, f, tmp)
                rabbit_rows.append((k, a, b))
                print(f"1.jpg 第{k}次 带配文：{a[:30]}  不带：{b[:30]}")

    # 2. 回归
    rows = []
    for prefix, group, d in REGRESS:
        folder = BOT / d
        if not folder.is_dir():
            continue
        i = 0
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in IMG_EXT or f.stem == "头像":
                continue
            i += 1
            try:
                do, _ = await look(old, tagger, client, model, f, tmp)
                dn, nh = await look(new, tagger, client, model, f, tmp)
            except Exception as e:  # noqa: BLE001
                do = dn = f"（出错：{e}）"
                nh = ""
            rows.append((f"{prefix}{i}", group, f.name, nh, do, dn))
            print(f"{prefix}{i:<3} 旧：{do[:28]}  新：{dn[:28]}")

    cost = (TOK["hit"] * PRICE["hit"] + TOK["miss"] * PRICE["miss"] + TOK["out"] * PRICE["out"]) / 1e6

    def n(rx, col, grp=None):
        return sum(bool(rx.search(r[col])) for r in rows if grp is None or r[1] == grp)

    head_diff = [r[0] for r in rows if r[4].startswith("伊蕾娜本人的画像") != r[5].startswith("伊蕾娜本人的画像")]
    shot_bad = [r[0] for r in rows if r[1] == "聊天截图" and not re.match(r"(群聊|私聊)截图", r[5])]
    doll_lost = [r[0] for r in rows if r[1] == "实物照片" and "人偶" in r[4] and "人偶" not in r[5]]
    nh_rows = [r for r in rows if r[3]]
    nh_person = [r[0] for r in nh_rows if PERSON_RE.search(r[5])]
    len_old = sum(len(r[4]) for r in rows) / max(1, len(rows))
    len_new = sum(len(r[5]) for r in rows) / max(1, len(rows))
    rab_cap = sum(bool(RABBIT_RE.search(a)) for _, a, _ in rabbit_rows)
    rab_nocap = sum(bool(RABBIT_RE.search(b)) for _, _, b in rabbit_rows)
    bad_cap = sum(bool(BAD_RE.search(a)) for _, a, _ in rabbit_rows)
    bad_nocap = sum(bool(BAD_RE.search(b)) for _, _, b in rabbit_rows)
    lines = [
        f"# 识图手画测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 模型 `{model}`；新版 = 现在的 `vision.py`（简笔画规则 + “画里没有人”提示 + 配文），旧版 = 同一个文件把这三样关掉。",
        f"> {TOK['calls']} 次调用，输入 {TOK['hit'] + TOK['miss']} token（命中缓存 {TOK['hit']}），输出 {TOK['out']} token，按空闲价约 {cost:.3f} 元。",
        "",
        "## 汇总",
        "",
        f"- 1.jpg（拓印的两只兔子）新版看 {len(rabbit_rows)} 次：带配文写出“兔” **{rab_cap}/{len(rabbit_rows)}**，"
        f"不带配文 **{rab_nocap}/{len(rabbit_rows)}**；写了“人偶 / 字母 / 帽”：带配文 {bad_cap} 次，不带 {bad_nocap} 次",
        f"- 回归 {len(rows)} 张：“伊蕾娜本人的画像”开头新旧不一样的 **{len(head_diff)} 张**" + ("（" + "、".join(head_diff) + "）" if head_diff else ""),
        f"- 实物照片旧版写了“人偶”、新版没写的：**{len(doll_lost)} 张**" + ("（" + "、".join(doll_lost) + "）" if doll_lost else ""),
        f"- 聊天截图新版没以“群聊截图 / 私聊截图”开头的：**{len(shot_bad)} 张**" + ("（" + "、".join(shot_bad) + "）" if shot_bad else ""),
        f"- 本机看出“画里没有人”的：{len(nh_rows)} 张（{'、'.join(r[0] for r in nh_rows) or '无'}）；其中新版还写了人的：**{len(nh_person)} 张**"
        + ("（" + "、".join(nh_person) + "）" if nh_person else ""),
        f"- 写了“人偶”的：旧 {n(re.compile('人偶'), 4)} 张 → 新 {n(re.compile('人偶'), 5)} 张（实物照片里：旧 {n(re.compile('人偶'), 4, '实物照片')} → 新 {n(re.compile('人偶'), 5, '实物照片')}）",
        f"- 平均字数：旧 {len_old:.0f} → 新 {len_new:.0f}",
        "",
        "## 空间二创（旧：不带配文；新：带说说原文）",
        "",
        "| 图 | 说说原文 | 实际是什么 | 旧 | 新 |",
        "|---|---|---|---|---|",
    ]
    for name, cap, truth, do, dn, nh in acg_rows:
        lines.append(f"| {name} | {cap} | {truth} | {do} | {dn}{'（本机：画里没有人）' if nh else ''} |")
    lines += ["", f"## 1.jpg 新版各看 {REPEAT} 次", "", "| 第几次 | 带配文“兔？！” | 不带配文 |", "|---|---|---|"]
    for k, a, b in rabbit_rows:
        lines.append(f"| {k} | {a} | {b} |")
    lines += ["", "## 回归（新旧都不带配文）", "", "| 编号 | 组 | 文件 | 本机 | 旧 | 新 |", "|---|---|---|---|---|---|"]
    for no, group, name, nh, do, dn in rows:
        lines.append(f"| {no} | {group} | `{name[:22]}` | {nh} | {do} | {dn} |")
    out_dir = BOT.parent / "docs" / "识图手画测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"手画_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln in lines[7:15]:
        print(ln)
    print(f"花费约 {cost:.3f} 元。结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
