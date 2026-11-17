"""表情包回放（11/17，聊天 agent，《问题排查记录-表情包当图片-1117》）：对方拿图当表情回她，她是接住情绪，还是去评论“图里画的是谁”。

题目：tools\\sticker_cases.json，图放在 tools\\表情包对比\\（文件名见题目；没有就跳过那题）。
做法：
  X 改前：图一律写成「[图片：……]」，没有“（是表情包）”，没有【表情包】【只发了图】提示，聊天规则里没那半句
  N 改后（11/17）
同一张图只看一次（本机认人 + 写描述），X、N 用同一份描述；原图也照线上给聊天模型看（VISION_CHAT_SEES_IMAGE 按 .env）。
走真实的私聊流程（拼提示、人设、聊天规则、关系提示），长期记忆关掉，不碰真的 data，不连 QQ。可以只输出 [不回]。

怎么用：双击 bot\\表情包回放.bat。结果在 docs\\表情包回放\\表情包回放_时间.md 和 .json。
环境变量（bat 会设好）：REPLAY_BOT = bot 目录；STK_ONLY = 只跑某几题（K1,K4）；STK_SAMPLES = 每种每题几次；
REPLAY_FAKE=1：程序自测，不调模型、不认人（图找不到时用一张小图代替）
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

import replay_chat as rc

BEIJING = timezone(timedelta(hours=8))
BOT_DIR = Path(os.environ.get("REPLAY_BOT", ".")).resolve()
CASES_FILE = BOT_DIR / "tools" / "sticker_cases.json"
OUT_DIR = BOT_DIR.parent / "docs" / "表情包回放"
ONLY = [x for x in os.environ.get("STK_ONLY", "").split(",") if x]
FAKE = os.environ.get("REPLAY_FAKE") == "1"
VARIANTS = {"X": "改前（不分表情包、没提示）", "N": "改后（11/17）"}
SCENES = {1: "QQ 标了表情的（表情包）", 2: "QQ 没标、其实是当表情发的", 3: "普通照片（不是表情）"}
# 回复里在评论“图本身”（画的是谁、画得怎样）
PIC_RE = re.compile(r"画的|画得|画风|这是谁|是谁啊|这张图|这图|图里|图上|巫女|灵梦|哪来的图|表情包")
OOC_RE = re.compile(r"动漫|动画|二次元|番剧|角色|AI|人工智能|模型|插画师|画师")
SELF_RE = re.compile(r"是我|我本人|画的我|画的是我|这不就是我|就是我|本小姐|我的画像|我吗？?没错")
NOT_SELF_RE = re.compile(r"不是我|才不是我|哪里像我|不像我|别人|谁啊")
DOLL_RE = re.compile(r"人偶|手办|玩偶|娃娃|小人|模型|摆件")


class _Bot:
    self_id = "123"

    def __init__(self):
        from types import SimpleNamespace
        self.config = SimpleNamespace(superusers=set(), command_start={"/"}, nickname=set())
        self.sent: list[str] = []

    async def send(self, event, msg):
        self.sent.append(str(msg))

    async def call_api(self, api, **kw):
        raise RuntimeError("回放里不连 QQ")


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 180, 160)).save(buf, "PNG")
    return buf.getvalue()


def _cases() -> tuple[list, int]:
    d = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = [c for c in d["cases"] if not ONLY or c["id"] in ONLY]
    return cases, int(os.environ.get("STK_SAMPLES") or d.get("samples", 3))


def _flags(case: dict, reply: str) -> list[str]:
    out = []
    if reply == "（没回）":
        out.append("没回")
    elif PIC_RE.search(reply) and not case.get("pic_ok"):
        out.append("评论了图本身：" + PIC_RE.search(reply).group(0))
    m = OOC_RE.search(reply)
    if m:
        out.append("出戏词：" + m.group(0))
    return out


def _apply(p, var: str, base: dict) -> None:
    """X：改前的样子；N：现在的样子"""
    pr = p.prompts
    p.textin.STICKER_NOTE = base["note"] if var == "N" else ""
    p.reply.STICKER_NOTE = base["note"] if var == "N" else "\x00"
    p.reply.media_hint = base["media_hint"] if var == "N" else (lambda text: "")
    pr.CHAT_RULES = base["rules"] if var == "N" else base["rules"].replace(base["rule_add"], "")


@pytest.mark.asyncio
async def test_sticker_compare(monkeypatch):
    import plugins.roleplay_chat as p
    import importlib
    vmod = importlib.import_module("plugins.roleplay_chat.vision")    # 插件包里的 vision 是识图对象，模块要这样拿（11/06 修）
    cases, samples = _cases()
    env = rc._env()
    # ---- 看图：同一个 Vision，换成真的看图客户端和本机认人，读本地图片代替下载
    v = p.core.vision
    tmpdir = Path(tempfile.mkdtemp())
    v.cache_file = tmpdir / "vision_cache.json"
    v._cache, v._raw = {}, type(v._raw)()
    tiny = _tiny_png()
    raws: dict[str, bytes] = {}

    async def get_raw(data, key):
        path = str(data.get("file") or "")
        if path not in raws:
            f = Path(path)
            raws[path] = vmod.shrink(f.read_bytes()) if f.exists() else (tiny if FAKE else None)
        return raws[path]
    monkeypatch.setattr(v, "_get_raw", get_raw)
    if FAKE:
        async def describe(data):
            name = Path(str(data.get("file"))).name
            return f"私聊截图，左边说“这台词我听过”，右边说“跟我结婚吧”（{name[:8]}）"
        monkeypatch.setattr(v, "describe", describe)
    else:
        from openai import AsyncOpenAI
        v.client = AsyncOpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
        v.model = env.get("VISION_MODEL") or "deepseek-flash"
        from plugins.roleplay_chat.tagger import Tagger
        tg = Tagger(BOT_DIR / "data" / "models", env.get("VISION_TAGGER_REPO") or "SmilingWolf/wd-swinv2-tagger-v3-int8", [],
                    threshold=float(env.get("VISION_TAGGER_THRESHOLD") or 0.72), threads=1, auto_download=False)
        tg._load()
        assert tg.ready, "本机认人模型没加载起来（bot\\data\\models 里要有 wd-swinv2-tagger-v3-int8），测不了第 3、4 项"
        v.tagger = tg
    monkeypatch.setattr(p.cfg, "vision_enabled", True)
    # ---- 聊天：真的模型，记下每次聊天调用的用量
    usage: list[dict] = []
    if FAKE:
        async def chat(**kw):
            last = kw["messages"][-1]["content"]
            has = isinstance(last, list) and any(x.get("type") == "image_url" for x in last)
            usage.append({"prompt": 1000 + (500 if has else 0), "out": 10})
            return rc._resp("（带了原图）嗯。" if has else "嗯。")
    else:
        real = rc._real_client().chat.completions.create

        async def chat(**kw):
            r = await real(**kw)
            u = getattr(r, "usage", None)
            if u is not None and kw.get("max_tokens") != 2 and "response_format" not in kw:
                usage.append({"prompt": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0})
            return r
    monkeypatch.setattr(p.client.chat.completions, "create", chat)
    for key, attr, conv in (("DEEPSEEK_MODEL", "deepseek_model", str), ("LLM_TEMPERATURE", "llm_temperature", float)):
        if env.get(key):
            monkeypatch.setattr(p.cfg, attr, conv(env[key]))
    monkeypatch.setattr(p.ltm, "enabled", False)
    monkeypatch.setattr(p.cfg, "dislike_ignore_prob", 0.0)
    monkeypatch.setattr(p.cfg, "vision_chat_max_images", 2)
    if hasattr(p.cfg, "redo_on_more"):
        monkeypatch.setattr(p.cfg, "redo_on_more", False)
    real_fam = p.familiarity_of
    rule_add = "后面标着「（是表情包）」的，是对方拿来当表情发的，看它表达的情绪、接对方的意思就好，别去评论图里画的是谁、画得怎样。"
    assert rule_add in p.prompts.CHAT_RULES, "聊天规则里那半句改了，X 要跟着改"
    base = {"note": p.textin.STICKER_NOTE, "media_hint": p.reply.media_hint, "rules": p.prompts.CHAT_RULES, "rule_add": rule_add}

    results, skipped = [], []
    uid = 77000
    try:
        for case in cases:
            img = BOT_DIR / case["image"]
            if not img.exists() and not FAKE:
                skipped.append(case["id"])
                print(f"{case['id']}：没找到图 {case['image']}，跳过")
                continue
            rc.set_plugin_attr(p, "familiarity_of", lambda q, f=case.get("fam") or "acquaintance": f)
            for var in VARIANTS:
                _apply(p, var, base)
                for k in range(samples):
                    uid += 1
                    p._histories.clear(); p._last_trigger.clear(); p._global_window.clear(); p._hour_window.clear()
                    p._inbox.clear(); p.reply._turn_images.clear(); p._farewell_at.clear(); p._away.clear()
                    shutil.rmtree("data/history", ignore_errors=True)
                    Path("data/history").mkdir(parents=True, exist_ok=True)
                    now = _time.time()
                    hist = []
                    for i, c in enumerate(case.get("context") or []):
                        hist.append({"role": "assistant" if c["who"] == "her" else "user", "content": c["text"],
                                     "ts": now - 120 + i * 10})
                    p._histories[f"private_{uid}"] = hist
                    seg = MessageSegment.image(file=str(img))
                    seg.data.update(case.get("seg") or {})        # QQ 给的 sub_type / summary（11/17 那张灵梦：sub_type=11、没有说明）
                    m = Message(case["text"]) + seg if case.get("text") else Message([seg])
                    ev = PrivateMessageEvent(time=int(now), self_id=123, post_type="message", sub_type="friend", user_id=uid,
                                             message_type="private", message_id=uid, message=m, original_message=m,
                                             raw_message=str(m), font=0, sender=Sender(user_id=uid, nickname="测试的人"), to_me=True)
                    bot = _Bot()
                    n0 = len(usage)
                    await p.converse(bot, ev)
                    reply = " / ".join(x for x in bot.sent if not x.startswith("[CQ:image")) or "（没回）"
                    h = p.get_history(f"private_{uid}")
                    seen = next((x["content"] for x in h if x.get("role") == "user" and "[图片" in str(x.get("content"))), "")
                    u = usage[n0:]
                    results.append({"id": case["id"], "scene": case["scene"], "variant": var, "sample": k, "reply": reply,
                                    "seen": seen, "prompt_tokens": sum(x["prompt"] for x in u), "out_tokens": sum(x["out"] for x in u),
                                    "flags": _flags(case, reply)})
                    print(f"{case['id']} {var}#{k}: {reply[:60]!r} {results[-1]['flags']}")
    finally:
        rc.set_plugin_attr(p, "familiarity_of", real_fam)
        _apply(p, "N", base)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    (OUT_DIR / f"表情包回放_{stamp}.json").write_text(json.dumps({"fake": FAKE, "samples": samples, "skipped": skipped, "results": results},
                                                                ensure_ascii=False, indent=1), encoding="utf-8")

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0
    md = [f"# 表情包回放 {stamp}", "",
          f"> 聊天 agent 的工具 `tools\\sticker_compare.py`。X = 改前，N = 改后（11/17）。每种每题 {samples} 次。“没回”是她只输出了 [不回]。"
          + ("**程序自测（假模型）**" if FAKE else ""),
          f"> 跳过的题（图不在）：{'、'.join(skipped) or '无'}", "",
          "## 汇总", "", "| 项 | X 改前 | N 改后 |", "|---|---|---|"]
    for name, test in (("评论了图本身（表情包题）", lambda r: r["scene"] in (1, 2) and any(x.startswith("评论了图本身") for x in r["flags"])),
                       ("没回（[不回]）", lambda r: "没回" in r["flags"]),
                       ("出戏词", lambda r: any(x.startswith("出戏词") for x in r["flags"]))):
        md.append(f"| {name} | " + " | ".join(str(sum(test(r) for r in results if r["variant"] == v)) for v in VARIANTS) + " |")
    md.append("| 回复平均字数 | " + " | ".join(f"{avg([len(r['reply']) for r in results if r['variant'] == v and r['reply'] != '（没回）']):.0f}" for v in VARIANTS) + " |")
    md += ["", "自动标记只是关键词，最后要人看原话：表情包题看她有没有接住对方的情绪；普通照片题看她还会不会好好聊图里的东西。", ""]
    for case in cases:
        rs = [r for r in results if r["id"] == case["id"]]
        if not rs:
            continue
        md += [f"## {case['id']}（{SCENES.get(case['scene'], '')}）", "", f"- 图：`{case['image']}`，QQ 给的：{json.dumps(case.get('seg') or {}, ensure_ascii=False)}",
               f"- 看什么：{case.get('look', '')}"]
        if case.get("context"):
            md.append("- 之前的聊天：" + "；".join(("她" if c["who"] == "her" else "对方") + "：" + c["text"].replace("\n", " / ") for c in case["context"]))
        for v in VARIANTS:
            seen = next((r["seen"] for r in rs if r["variant"] == v and r["seen"]), "")
            md.append(f"- {v} 她看到的：{seen}")
        md += ["", "| 做法 | 回复 | 标记 |", "|---|---|---|"]
        for r in rs:
            md.append(f"| {r['variant']}#{r['sample']} | {r['reply'].replace('|', '｜')} | {'；'.join(r['flags'])} |")
        md.append("")
    out = OUT_DIR / f"表情包回放_{stamp}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n结果在 {out}")
