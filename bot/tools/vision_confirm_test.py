"""识图"再确认"测试（10/23，识图 agent）

想知道的事：把 VISION_TAGGER_MAYBE（再确认线）从 0.35 降到 0.1 安不安全。
"再确认"就是：本机认人觉得"有点像伊蕾娜但拿不准"时，让 DeepSeek 带着"画中人物有可能是伊蕾娜，请仔细看"的提示看图，
它确认了才算她。这一步线上还没真正发生过，所以先离线测：

- 是她的图：表情库、说说图库里本机分数偏低的 18 张（《识图抽查-1022》第 2.3 节 1～18 号），另加 2 张分数高的当对照
- 不是她的图：tools\\识图对照图\\ 里你放的图（别的银发、白发角色）
- 每张图都**强行**带上"可能是伊蕾娜"的提示问 2 次（线上只有分数落在再确认线和门槛之间才会带）。
  对"不是她"的图来说，这是最坏的情况：只要这里不乱认，降低再确认线就不会多出误认

用的提示词、缩图方式、模型参数都直接取自 plugins\\roleplay_chat\\vision.py，和线上一样。
不碰 data\\，不连 QQ。大约 58 次看图调用，按空闲价约 1 毛钱（白天高峰时段约 2 毛）。
结果写到 docs\\识图再确认测试\\再确认_日期_时间.md。
用法：双击 bot\\识图再确认测试.bat
"""
from __future__ import annotations

import base64
import importlib.util
import sys
import time
import types
from datetime import datetime
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
PLUGIN = BOT / "plugins" / "roleplay_chat"
RUNS = 2

# 是她：(来源文件夹, 文件名, 抽查报告里的编号)
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
    # 对照：分数高、本来就直接认出的
    ("qzone/images", "illust_138760642_20260330_025804.png", 19), ("data/stickers", "85344b9c0b51bddc.jpg", 27),
]
NEG_DIR = BOT / "tools" / "识图对照图"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}   # 元 / 百万 token，和 config.py 的 budget_price 一样（空闲价）


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in (BOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_module(name: str, path: Path):
    """按文件加载插件里的模块；vision.py 里的 `from . import budget` 用一个什么都不做的替身接住"""
    pkg = "vtest"
    if pkg not in sys.modules:
        p = types.ModuleType(pkg)
        p.__path__ = []
        sys.modules[pkg] = p
        b = types.ModuleType(pkg + ".budget")
        b.track = lambda *a, **k: 0.0
        sys.modules[pkg + ".budget"] = b
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    env = read_env()
    key = env.get("DEEPSEEK_API_KEY", "")
    if not key or "在这里" in key:
        print("没在 .env 里找到 DEEPSEEK_API_KEY，测不了。")
        return
    base = env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = env.get("VISION_MODEL") or "deepseek-flash"

    vision = load_module("vision", PLUGIN / "vision.py")
    tagger_mod = load_module("tagger", PLUGIN / "tagger.py")
    prompt = vision.DESCRIBE_PROMPT + vision.MAYBE_HINT

    repo = "SmilingWolf/wd-swinv2-tagger-v3-int8"     # 和服务器一样用压缩版
    t = tagger_mod.Tagger(BOT / "data" / "models", repo, [], threshold=0.0, threads=1, auto_download=False)
    try:
        t._load()
    except Exception as e:  # noqa: BLE001
        print(f"识图模型加载失败（{e}），本机分数一栏会空着，不影响 DeepSeek 那部分")
        t = None

    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base, timeout=60)

    items = [("是她", f"#{no}", BOT / sub / name) for sub, name, no in POSITIVES]
    negs = sorted(f for f in NEG_DIR.iterdir() if f.suffix.lower() in IMG_EXT) if NEG_DIR.exists() else []
    items += [("不是她", f"N{i + 1}", f) for i, f in enumerate(negs)]
    print(f"是她 {len(POSITIVES)} 张、不是她 {len(negs)} 张，每张问 {RUNS} 次，共 {len(items) * RUNS} 次调用")

    rows, tok = [], {"hit": 0, "miss": 0, "out": 0}
    for kind, no, path in items:
        if not path.exists():
            rows.append((kind, no, path.name, None, ["（文件不存在）"], []))
            continue
        try:
            small = vision.shrink(path.read_bytes())
        except Exception as e:  # noqa: BLE001
            rows.append((kind, no, path.name, None, [f"（缩图失败：{e}）"], []))
            continue
        score = None
        if t is not None:
            try:
                score = t._detect_sync(small)[1]
            except Exception:  # noqa: BLE001
                pass
        mime = vision._mime(small) or "image/jpeg"
        b64 = base64.b64encode(small).decode()
        descs, oks = [], []
        for _ in range(RUNS):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]}],
                    temperature=0.2, max_tokens=120,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                d = vision.sanitize((resp.choices[0].message.content or "").strip().replace("\n", " ")[:70])
                u = resp.usage
                hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
                tok["hit"] += hit
                tok["miss"] += (getattr(u, "prompt_tokens", 0) or 0) - hit
                tok["out"] += getattr(u, "completion_tokens", 0) or 0
            except Exception as e:  # noqa: BLE001
                d = f"（调用失败：{e}）"
            descs.append(d)
            oks.append(d.startswith("伊蕾娜本人的画像"))
            time.sleep(0.5)
        rows.append((kind, no, path.name, score, descs, oks))
        mark = "".join("是" if o else "否" for o in oks)
        print(f"{kind} {no:>4}  分数 {score if score is None else round(score, 3)}  确认：{mark}  {descs[0][:40]}")

    cost = sum(tok[k] * PRICE[k] for k in tok) / 1e6
    pos = [r for r in rows if r[0] == "是她" and r[5]]
    neg = [r for r in rows if r[0] == "不是她" and r[5]]
    pos_ok = sum(all(r[5]) for r in pos)
    pos_half = sum(any(r[5]) and not all(r[5]) for r in pos)
    neg_bad = sum(any(r[5]) for r in neg)
    lines = [
        f"# 识图再确认测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 模型 `{model}`；每张图强行带上\"可能是伊蕾娜\"的提示问 {RUNS} 次；提示词、缩图都取自现在的 `vision.py`。",
        f"> 调用 {len(items) * RUNS} 次，输入 {tok['hit'] + tok['miss']} token（命中缓存 {tok['hit']}），输出 {tok['out']} token，"
        f"按空闲价约 {cost:.3f} 元。",
        "",
        "## 汇总",
        "",
        f"- 是她（{len(pos)} 张）：{RUNS} 次都确认 {pos_ok} 张，只确认了 1 次 {pos_half} 张，一次都没确认 {len(pos) - pos_ok - pos_half} 张",
        f"- **不是她（{len(neg)} 张）：被确认成她的 {neg_bad} 张**（只要有 1 次就算）",
        "",
        "## 逐张",
        "",
        "| 类别 | 编号 | 文件 | 本机分数 | 确认 | 第 1 次描述 | 第 2 次描述 |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, no, name, score, descs, oks in rows:
        s = "—" if score is None else f"{score:.3f}"
        mark = "".join("✅" if o else "❌" for o in oks) or "—"
        d2 = descs[1] if len(descs) > 1 else ""
        lines.append(f"| {kind} | {no} | `{name[:28]}` | {s} | {mark} | {descs[0]} | {d2} |")
    out_dir = BOT.parent / "docs" / "识图再确认测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"再确认_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(lines[7])
    print(lines[8])
    print(f"花费约 {cost:.3f} 元。结果在 {out}")


if __name__ == "__main__":
    main()
