"""看图对比（11/06，聊天 agent）：回复这一轮把原图也给聊天模型看（识图 agent 的方案 A），和现在“只给描述”比一比。

题目：tools\\image_chat_cases.json（梗图接上下文、聊天截图、别的灰发角色、她自己的图、实物照片、普通照片）。
做法：
  D 只给描述（现在线上的样子，VISION_CHAT_SEES_IMAGE=false）
  I 描述 + 原图（VISION_CHAT_SEES_IMAGE=true）
同一张图只看一次（本机认人 + 写描述），D、I 用的是同一份描述，比的只是“多给原图”这一步。
走的是真实的私聊流程（拼提示、人设、聊天规则、关系提示），长期记忆关掉，不碰真的 data，不连 QQ。

怎么用：双击 bot\\看图对比.bat。结果在 docs\\看图对比\\看图对比_时间.md（逐题原话 + 自动标记）和 .json。
环境变量（bat 会设好）：REPLAY_BOT = bot 目录；IMGCMP_ONLY = 只跑某几题（M1,S1）；IMGCMP_SAMPLES = 每种每题几次；
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
CASES_FILE = BOT_DIR / "tools" / "image_chat_cases.json"
OUT_DIR = BOT_DIR.parent / "docs" / "看图对比"
ONLY = [x for x in os.environ.get("IMGCMP_ONLY", "").split(",") if x]
FAKE = os.environ.get("REPLAY_FAKE") == "1"
VARIANTS = {"D": "只给描述（现在）", "I": "描述 + 原图"}
SCENES = {1: "梗图接上下文", 2: "聊天截图", 3: "别的灰发角色（不能认成自己）", 4: "她自己的图（要认得出）", 5: "实物照片", 6: "普通照片"}
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
    return cases, int(os.environ.get("IMGCMP_SAMPLES") or d.get("samples", 3))


def _flags(case: dict, reply: str) -> list[str]:
    out = []
    if OOC_RE.search(reply) and not (case["scene"] == 5 and "模型" in reply and not re.search(r"动漫|二次元|角色|番剧", reply)):
        out.append("出戏词：" + OOC_RE.search(reply).group(0))
    if case["scene"] in (3,) or case["id"] == "S3":
        if SELF_RE.search(reply) and not NOT_SELF_RE.search(reply):
            out.append("可能认成了自己")
    if case["scene"] == 4 and NOT_SELF_RE.search(reply):
        out.append("可能没认出自己")
    if case["scene"] == 5 and not DOLL_RE.search(reply):
        out.append("没提到人偶 / 手办")
    return out


@pytest.mark.asyncio
async def test_image_compare(monkeypatch):
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
    monkeypatch.setattr(p.cfg, "skip_by_model", False)        # 都要回，才好比
    monkeypatch.setattr(p.cfg, "dislike_ignore_prob", 0.0)
    monkeypatch.setattr(p.cfg, "vision_chat_max_images", 2)
    if hasattr(p.cfg, "redo_on_more"):
        monkeypatch.setattr(p.cfg, "redo_on_more", False)
    real_fam = p.familiarity_of

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
                monkeypatch.setattr(p.cfg, "vision_chat_sees_image", var == "I")
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

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    (OUT_DIR / f"看图对比_{stamp}.json").write_text(json.dumps({"fake": FAKE, "samples": samples, "skipped": skipped, "results": results},
                                                                ensure_ascii=False, indent=1), encoding="utf-8")

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0
    md = [f"# 看图对比 {stamp}", "",
          f"> 聊天 agent 的工具 `tools\\image_chat_compare.py`。D = 只给描述（现在），I = 描述 + 原图。每种每题 {samples} 次。"
          + ("**程序自测（假模型）**" if FAKE else ""),
          f"> 跳过的题（图不在）：{'、'.join(skipped) or '无'}", "",
          "## 汇总", "", "| 项 | D 只给描述 | I 描述 + 原图 |", "|---|---|---|"]
    for name, test in (("出戏词", lambda f: any(x.startswith("出戏词") for x in f)),
                       ("别人的图可能认成自己（第 3 项、S3）", lambda f: "可能认成了自己" in f),
                       ("她的图可能没认出自己（第 4 项）", lambda f: "可能没认出自己" in f),
                       ("实物没提到人偶 / 手办（第 5 项）", lambda f: "没提到人偶 / 手办" in f)):
        md.append(f"| {name} | " + " | ".join(str(sum(test(r["flags"]) for r in results if r["variant"] == v)) for v in VARIANTS) + " |")
    md.append("| 回复平均字数 | " + " | ".join(f"{avg([len(r['reply']) for r in results if r['variant'] == v]):.0f}" for v in VARIANTS) + " |")
    md.append("| 每轮输入 token（平均） | " + " | ".join(f"{avg([r['prompt_tokens'] for r in results if r['variant'] == v]):.0f}" for v in VARIANTS) + " |")
    md += ["", "自动标记只是关键词，最后要人看原话：第 1、2 项看 I 有没有明显接得更好。", ""]
    for case in cases:
        rs = [r for r in results if r["id"] == case["id"]]
        if not rs:
            continue
        md += [f"## {case['id']}（{SCENES.get(case['scene'], '')}）", "", f"- 图：`{case['image']}`", f"- 看什么：{case.get('look', '')}"]
        if case.get("context"):
            md.append("- 之前的聊天：" + "；".join(("她" if c["who"] == "her" else "对方") + "：" + c["text"].replace("\n", " / ") for c in case["context"]))
        md += [f"- 她看到的（描述）：{rs[0]['seen']}", "", "| 做法 | 回复 | 标记 |", "|---|---|---|"]
        for r in rs:
            md.append(f"| {r['variant']}#{r['sample']} | {r['reply'].replace('|', '｜')} | {'；'.join(r['flags'])} |")
        md.append("")
    out = OUT_DIR / f"看图对比_{stamp}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n结果在 {out}")
