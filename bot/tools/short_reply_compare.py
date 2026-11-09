"""只回一两个字对比（11/09，聊天 agent，《交接-聊天真实感三项-聊天-1109》A）：对方随口一句（附和、感叹、水话）时，
她能不能只回一两个字 / 只甩一个表情，而不是每次都回一两句。

做法：
  现 现在线上的样子（【可以不回】只有“不回 / 正常回”两条路）
  甲 SKIP_SHORT_REPLY：【可以不回】改成三选一（不回 / 一两个字或一个表情 / 正常回），抽中表情时也可以只发表情
  乙 甲 + 这种时候长短提示固定用 SHORT_VARIANTS 第一档（带例子“嗯。”“不要。”；第 1 轮的样子）
  丁 甲 + SKIP_PIN_SHORT：长短提示固定用 SKIP_PIN_HINT（不带例子，第 2 轮加的：第 1 轮乙的短回八成是“嗯”）
  丙 乙 + 程序兜底（SKIP_FIRST_ONLY_PROB）：回了好几条、第一条不超过 6 个字，按概率只发第一条。
     不另外调模型：在乙的结果上找出会被砍的，报告里列出砍前砍后，看砍完还接不接得上
题目：tools\\short_reply_cases.json。私聊按 NapCat 日志把前面的聊天放进记录（照搬措辞对比的回放），群聊走 replay_chat 的回放。
表情：按线上的概率抽（STICKER_PROB × 关系倍数），抽中了能发；回放环境里没有表情库，发出去的记成“[表情:情绪]”。
长期记忆、识图关掉，不碰真的 data，不连 QQ。

怎么用：双击 bot\\短回对比.bat。结果在 docs\\短回对比\\短回对比_时间.md（汇总 + 逐条原话）和 .json。
环境变量（bat 会设好）：REPLAY_BOT = bot 目录；SHORT_ONLY = 只跑某几题（P1,G1）；SHORT_VARIANTS = 只跑某几种（X 现、A 甲、B 乙、D 丁，比如 D 或 A,D）；
SHORT_SCALE = 次数乘几；REPLAY_FAKE=1：程序自测，不调模型
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nonebug import App
from nonebot.adapters.onebot.v11 import MessageSegment

import replay_chat as rc
import phrasing_compare as pc

BEIJING = timezone(timedelta(hours=8))
BOT_DIR = Path(os.environ.get("REPLAY_BOT", ".")).resolve()
CASES_FILE = BOT_DIR / "tools" / "short_reply_cases.json"
OUT_DIR = BOT_DIR.parent / "docs" / "短回对比"
ONLY = [x for x in os.environ.get("SHORT_ONLY", "").split(",") if x]
_V_ASCII = {"X": "现", "A": "甲", "B": "乙", "D": "丁"}     # bat 里写英文字母，免得命令行编码出问题
ONLY_V = [_V_ASCII.get(x, x) for x in os.environ.get("SHORT_VARIANTS", "").split(",") if x]
SCALE = float(os.environ.get("SHORT_SCALE") or 1)
FAKE = os.environ.get("REPLAY_FAKE") == "1"
VARIANTS = {"现": "现在线上", "甲": "只改提示（三选一）", "乙": "甲 + 长短固定第一档（带“嗯。”的例子）",
            "丁": "甲 + 长短固定“几个字”（不带例子）"}
CUT_MAX = 6                    # 丙：第一条不超过几个字才砍
STICKER_RE = re.compile(r"\[表情:([^\]]+)\]")
FAMN = {"close": "很熟", "acquaintance": "熟人", "friend": "普通朋友", "stranger": "陌生人", "disliked": "讨厌"}


def _fam_label(f) -> str:
    return FAMN.get(f, f) if isinstance(f, str) else "按群里的人"


def classify(reply: str) -> str:
    """不回 / 只发表情 / 短回（文字 ≤4 字，可带表情）/ 正常回"""
    text = STICKER_RE.sub("", reply)
    words = re.sub(r"[\s\n。，、！？!?,.…~～/]", "", text)
    if not reply.strip():
        return "不回"
    if not words:
        return "只发表情"
    if len(words) <= 4:
        return "短回"
    return "正常回"


def bubbles_of(reply: str) -> list[str]:
    return [x for x in (STICKER_RE.sub("", s).strip() for s in reply.split("\n")) if x]


def would_cut(reply: str) -> str | None:
    """丙：乙的这条会被砍成什么（不会砍返回 None）"""
    bs = bubbles_of(reply)
    first_ok = bool(bs) and bool(re.search(r"[\u4e00-\u9fffA-Za-z]", bs[0])) and bs[0][:1] not in "（(【["   # 和 reply.py 一样：半截括号不砍
    if len(bs) > 1 and first_ok and len(bs[0]) <= CUT_MAX:
        return bs[0]
    return None


@pytest.mark.asyncio
async def test_short_reply(app: App, monkeypatch):
    import plugins.roleplay_chat as p
    import importlib
    r = importlib.import_module("plugins.roleplay_chat.reply")
    sc = importlib.import_module("plugins.roleplay_chat.stickers_chat")
    from plugins.roleplay_chat.stickers import EMOTIONS
    d = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = [c for c in d["cases"] if not ONLY or c["id"] in ONLY]
    samples = {v: max(1, round(n * SCALE)) for v, n in d["samples"].items() if not ONLY_V or v in ONLY_V}
    env = rc._env()
    if FAKE:
        async def real(**kw):
            if kw.get("max_tokens") == 2:
                return rc._resp("是")
            sys_text = "".join(str(m.get("content")) for m in kw.get("messages") or [] if m.get("role") == "system")
            if "也可以只回一两个字" in sys_text:
                return rc._resp(random.choice(["[不回]", "哦。", "是喔。\n不过你这语气像在敷衍我", "[表情:无语]"]))
            return rc._resp("是喔。\n不过你这语气像在敷衍我")
    else:
        real = rc._real_client().chat.completions.create
    for key, attr, conv in (("DEEPSEEK_MODEL", "deepseek_model", str), ("LLM_TEMPERATURE", "llm_temperature", float),
                            ("STICKER_PROB", "sticker_prob", float), ("STICKER_ONLY_PROB", "sticker_only_prob", float)):
        if env.get(key):
            monkeypatch.setattr(p.cfg, attr, conv(env[key]))
    monkeypatch.setattr(p.ltm, "enabled", False)
    monkeypatch.setattr(p.cfg, "vision_enabled", False)
    monkeypatch.setattr(p.cfg, "smart_min_interval", 0)
    monkeypatch.setattr(p.cfg, "multi_message", True)             # 线上是拆条发的，要数第一条多长
    monkeypatch.setattr(p.cfg, "dislike_ignore_prob", 0.0)
    monkeypatch.setattr(p.cfg, "skip_first_only_prob", 0.0)       # 丙在乙的结果上算，不在这里砍
    if hasattr(p.cfg, "flood_rounds"):
        monkeypatch.setattr(p.cfg, "flood_rounds", 0)
    if hasattr(p.cfg, "redo_on_more"):
        monkeypatch.setattr(p.cfg, "redo_on_more", False)
    # 表情：按线上的概率抽，回放环境里没有表情库，发出去的记成文字“[表情:情绪]”
    roll_log: list[bool] = []

    def roll(target, fam, is_group, group_id, self_image):
        hit = random.random() < p.cfg.sticker_prob * p.core.tier_value(p.cfg.sticker_tier_multiplier, fam, 1.0)
        roll_log.append(hit)
        return hit, None, list(EMOTIONS)
    monkeypatch.setattr(r, "sticker_roll", roll)
    store = p.core.stickers
    monkeypatch.setattr(store, "pick", lambda emotion, *a, **k: {"no": 0, "tags": [emotion], "desc": "", "file": f"fake_{emotion}"})
    monkeypatch.setattr(store, "segment", lambda st, *a, **k: MessageSegment.text(f"[表情:{st['tags'][0]}]"))
    nicks = rc._nicknames()
    import nonebot
    monkeypatch.setattr(nonebot.get_driver().config, "nickname", set(nicks))
    real_time_mod, real_dt, real_fam = p.time, p.datetime, p.familiarity_of
    pin_new = r.SKIP_PIN_HINT
    events_cache, private_cache, results = {}, {}, []
    try:
        for case in cases:
            if case["kind"] == "group" and (case["log"], case["group"]) not in events_cache:
                events_cache[(case["log"], case["group"])] = rc.read_log(case["log"], int(case["group"]))
            if case["kind"] == "private" and (case["log"], case["user"]) not in private_cache:
                private_cache[(case["log"], case["user"])] = pc.read_private(case["log"], int(case["user"]))
            for fam in case["fams"]:
                if isinstance(fam, dict):
                    rc.set_plugin_attr(p, "familiarity_of", lambda q, m=fam: m.get(str(q), "stranger"))
                else:
                    rc.set_plugin_attr(p, "familiarity_of", lambda q, f=fam: f)
                for v, n in samples.items():
                    monkeypatch.setattr(p.cfg, "skip_short_reply", v in ("甲", "乙", "丁"))
                    monkeypatch.setattr(p.cfg, "skip_pin_short", v in ("乙", "丁"))
                    monkeypatch.setattr(r, "SKIP_PIN_HINT", p.SHORT_VARIANTS[0][1] if v == "乙" else pin_new)
                    for k in range(n):
                        p._histories.clear(); p._passive.clear(); p._last_trigger.clear(); p._global_window.clear()
                        p._hour_window.clear(); p._group_hour.clear(); p._farewell_at.clear(); p._recent_chat.clear()
                        p._last_bot_msg.clear(); p._chat_seq.clear(); p._arrival_seq.clear(); p._private_hour.clear(); p._away.clear()
                        sc._last_sticker.clear()
                        rc.set_plugin_attr(p, "time", real_time_mod); rc.set_plugin_attr(p, "datetime", real_dt)
                        shutil.rmtree("data/history", ignore_errors=True)
                        Path("data/history").mkdir(parents=True, exist_ok=True)
                        n_roll = len(roll_log)
                        if case["kind"] == "group":
                            res = await rc.replay_once(app, p, dict(case, force_reply=True), *events_cache[(case["log"], case["group"])], real, nicks)
                        else:
                            res = await pc.replay_private(p, case, private_cache[(case["log"], case["user"])], real)
                        sys_tail = " ".join(str(m["content"]) for m in (res["prompt"] or [])[-3:] if m.get("role") == "system")
                        mlen = re.search(r"（这轮[^）]*）", sys_tail)
                        reply = res["reply"].replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")   # 发出去的文字是转义过的
                        row = {"id": case["id"], "fam": _fam_label(fam), "variant": v, "sample": k, "reply": reply,
                               "kind": classify(reply), "skip_hint": "【可以不回】" in sys_tail,
                               "sticker_roll": any(roll_log[n_roll:]), "length_hint": mlen.group(0)[:24] if mlen else ""}
                        if v in ("乙", "丁"):
                            row["cut"] = would_cut(reply)
                        results.append(row)
                        print(f"{case['id']} {row['fam']} {v}#{k}: {reply[:50]!r} → {row['kind']}")
    finally:
        rc.set_plugin_attr(p, "time", real_time_mod); rc.set_plugin_attr(p, "datetime", real_dt)
        rc.set_plugin_attr(p, "familiarity_of", real_fam)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    (OUT_DIR / f"短回对比_{stamp}.json").write_text(json.dumps({"fake": FAKE, "samples": samples, "results": results},
                                                             ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = ["不回", "短回", "只发表情", "正常回"]

    def summary_rows(rs: list[dict], label: str) -> list[str]:
        out = []
        for v in samples:
            x = [r for r in rs if r["variant"] == v]
            if x:
                cnt = {kd: sum(r["kind"] == kd for r in x) for kd in kinds}
                out.append(f"| {label} | {v} | {len(x)} | " + " | ".join(str(cnt[kd]) for kd in kinds)
                           + f" | {sum(r['sticker_roll'] for r in x)} |")
        return out
    md = [f"# 短回对比 {stamp}", "",
          "> 聊天 agent 的工具 `tools\\short_reply_compare.py`（《交接-聊天真实感三项-聊天-1109》A）。"
          + "；".join(f"{v} = {VARIANTS[v]}" for v in samples) + "；丙 = 乙 + 回了好几条且第一条 ≤6 字时只发第一条（在乙的结果上算）。"
          + ("**程序自测（假模型）**" if FAKE else ""),
          "> 短回 = 文字去掉标点不超过 4 个字（可以带表情）。分类是程序按字数分的，接不接得上要看下面的原话。", "",
          "## 汇总", "", "| 关系 | 做法 | 次数 | 不回 | 短回 | 只发表情 | 正常回 | 这轮抽中了表情 |", "|---|---|---|---|---|---|---|---|"]
    for fl in sorted({r["fam"] for r in results}, key=lambda s: ["很熟", "按群里的人", "陌生人"].index(s) if s in ("很熟", "按群里的人", "陌生人") else 9):
        md += summary_rows([r for r in results if r["fam"] == fl], fl)
    md += summary_rows(results, "合计")
    for base in ("乙", "丁"):
        yi = [r for r in results if r["variant"] == base]
        if not yi:
            continue
        cuts = [r for r in yi if r.get("cut")]
        n_short = sum(r["kind"] in ("短回", "只发表情") for r in yi)
        md += ["", f"**丙（在{base}上砍）**：{base}的 {len(yi)} 条里有 {len(cuts)} 条会被砍（回了好几条、第一条 ≤{CUT_MAX} 字），按 50% 算大约砍 {len(cuts) / 2:.0f} 条，"
               f"短回 + 只发表情会从 {n_short} 条变成约 {n_short + len(cuts) / 2:.0f} 条。逐条看砍完接不接得上：", "",
               "| 题目 | 关系 | 原话 | 砍完 |", "|---|---|---|---|"]
        for c in cuts:
            md.append(f"| {c['id']} | {c['fam']} | {c['reply'].replace(chr(10), ' / ').replace('|', '｜')} | {c['cut']} |")
    md.append("")
    md += ["## 短回都说了什么", "", "| 做法 | 短回、只发表情的原话（次数） | 以“嗯”开头的回复（占全部） |", "|---|---|---|"]
    for v in samples:
        x = [r for r in results if r["variant"] == v]
        cnt: dict[str, int] = {}
        for r in x:
            if r["kind"] in ("短回", "只发表情"):
                k = r["reply"].replace(chr(10), " / ")
                cnt[k] = cnt.get(k, 0) + 1
        md.append(f"| {v} | " + "；".join(f"{k}（{n}）" for k, n in sorted(cnt.items(), key=lambda kv: -kv[1])) + f" | {sum(r['reply'].startswith('嗯') for r in x)}/{len(x)} |")
    md.append("")
    for case in cases:
        md += [f"## {case['id']}（{case.get('type', '')}）：「{case.get('text', '')}」", "", f"当时她回的：{case.get('orig', '')}", ""]
        rs = [r for r in results if r["id"] == case["id"]]
        md += ["| 关系 | 做法 | 原话 | 分类 | 抽中表情 | 长短提示 |", "|---|---|---|---|---|---|"]
        for x in rs:
            md.append(f"| {x['fam']} | {x['variant']}#{x['sample']} | {x['reply'].replace(chr(10), ' / ').replace('|', '｜') or '（没回）'} | "
                      f"{x['kind']} | {'是' if x['sticker_roll'] else ''} | {x['length_hint'][3:13]} |")
        if rs and not all(x["skip_hint"] for x in rs):
            md.append(f"\n注意：这题有 {sum(not x['skip_hint'] for x in rs)} 次没给【可以不回】（回放时没满足条件），这几次三种做法其实一样。")
        md.append("")
    out = OUT_DIR / f"短回对比_{stamp}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n结果在 {out}")
