"""待久回放（11/09，聊天 agent，《交接-聊天真实感三项-聊天-1109》C）：她在一个地方待久了，冒泡、写信会不会自己掂量走不走，
会不会还在讲同一类海边小事。

近况用线上的 self.json（server_logs\\2026-11-09_0406\\data\\memory\\self.json：从 10/29 待在“另一片海边”，plans 空），
里面的日期整体挪到“今天”前后（11/09 当天的样子），再按题目把“到这儿的日子”改成 11 天前或 3 天前。
做法：
  N 现在的代码（待了 7 天以上加一句“接着待还是动身，你自己定”；“同一类”写明白了）
  O 改前（不加那句、“同一类”是原来的写法），对照用
  M1、M2 待久那句换成两种候选说法（第 3 轮用，见 STAY_TEXTS）；LIFESTAY_VARIANTS 选跑哪几种
题目：
  B11  冒泡，待了 11 天                         N 10 次、O 5 次
  B11+ 冒泡，待了 11 天，另一个群“刚才”冒泡讲了在沙滩上捡到个空瓶子（11/07 隔 4 分钟两个群都讲“在海边捡到东西”的样子）   N 10 次、O 5 次
  L11  写信，待了 11 天                         N 20 次、O 10 次
  B3   冒泡，待了 3 天                          N 5 次
  L3   写信，待了 3 天                          N 5 次
走的是真实的冒泡、写信流程（拼提示、人设），发出去的被拦下来记着；不交给近况整理、长期记忆关掉，不碰真的 data，不连 QQ。

怎么用：双击 bot\\待久回放.bat。结果在 docs\\待久回放\\待久回放_时间.md（逐条原话 + 自动标记）和 .json。
环境变量（bat 会设好）：REPLAY_BOT = bot 目录；LIFESTAY_SELF = self.json 的路径；LIFESTAY_ONLY = 只跑某几题（B11,L11）；
LIFESTAY_SCALE = 次数乘几（0.5 = 减半）；LIFESTAY_HOUR = 提示里的“现在”按几点算（默认 15 点，冒泡时段是 10～22 点；
-1 = 用真的时间）；REPLAY_FAKE=1：程序自测，不调模型
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import replay_chat as rc

BEIJING = timezone(timedelta(hours=8))
BOT_DIR = Path(os.environ.get("REPLAY_BOT", ".")).resolve()
SELF_FILE = Path(os.environ.get("LIFESTAY_SELF") or BOT_DIR.parent / "server_logs" / "2026-11-09_0406" / "data" / "memory" / "self.json")
OUT_DIR = BOT_DIR.parent / "docs" / "待久回放"
ONLY = [x for x in os.environ.get("LIFESTAY_ONLY", "").split(",") if x]
SCALE = float(os.environ.get("LIFESTAY_SCALE") or 1)
FAKE = os.environ.get("REPLAY_FAKE") == "1"
HOUR = int(os.environ.get("LIFESTAY_HOUR") or 15)   # 11/09 第 1 轮是凌晨 4 点半跑的，冒泡全在说“这个点”，第 2 轮起固定成下午
DATA_DAY = date(2026, 11, 9)          # self.json 是这天拷下来的
VARIANTS = {"N": "现在的代码", "O": "改前（对照）",
            "M1": "“待够了”说法（11/09 第 3 轮候选）", "M2": "“有时想再待、有时想走”说法（11/09 第 3 轮候选）"}
ONLY_V = [x for x in os.environ.get("LIFESTAY_VARIANTS", "").split(",") if x]
_STAY_HEAD = "在一个地方待了这么些天，接下来是接着待，还是动身去别处，你自己定"
_STAY_TAIL = "没走之前你还在这里，别说成已经换了地方。"
# 待久那句的候选（M1、M2 换掉 LIFE_LONG_STAY_TEXT；N 用代码里现在那句）
STAY_TEXTS = {
    "M1": _STAY_HEAD + "，接着待也完全可以；觉得待够了，就在这次说的话里顺口提一句，打算什么时候走、往哪边去。" + _STAY_TAIL,
    "M2": _STAY_HEAD + "：有时候你会想再待几天，有时候会觉得该走了；要走的话，就在这次说的话里顺口提一句，打算什么时候走、往哪边去。" + _STAY_TAIL,
}
OLD_ELSEWHERE = "这几天在别处讲过（这次别再讲同一件，也别讲同一类事，换个完全不同的话题）："
# (题目, 冒泡还是写信, 待了几天, 另一个群刚才讲过的, {做法: 次数})
CASES = [
    ("B11", "bubble", 11, "", {"N": 10, "O": 5, "M1": 10, "M2": 10}),
    ("B11+", "bubble", 11, "在沙滩上捡到一个被浪冲上来的空瓶子", {"N": 10, "O": 5, "M1": 10, "M2": 10}),
    ("L11", "letter", 11, "", {"N": 20, "O": 10, "M1": 15, "M2": 15}),
    ("B3", "bubble", 3, "", {"N": 5}),
    ("L3", "letter", 3, "", {"N": 5}),
]
LEAVE_RE = re.compile(r"动身|启程|上路|出发|离开|收拾|换个地方|换地方|去别处|别的地方|下一站|该走|要走|准备走|打算走|走了|往.{1,6}(走|去)|"
                      r"待腻|待够|待了.{0,4}(天|日)|住腻|看腻|多待|再待|接着待|继续待")
SEA_RE = re.compile(r"海|浪|潮|礁|贝|蟹|沙滩|海鸥|海风|渔")
PICK_RE = re.compile(r"捡|拾到|捞到|摸到")
DATE_RE = re.compile(r"^(\d{4}-\d\d-\d\d)")


def _shift(obj, days: int):
    """self.json 里的日期（YYYY-MM-DD 开头的字符串）和时间戳（at）整体往后挪 days 天"""
    if isinstance(obj, dict):
        return {k: (v + days * 86400 if k == "at" and isinstance(v, (int, float)) else _shift(v, days)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shift(x, days) for x in obj]
    if isinstance(obj, str):
        m = DATE_RE.match(obj)
        if m:
            try:
                d = date.fromisoformat(m.group(1)) + timedelta(days=days)
                return d.isoformat() + obj[10:]
            except ValueError:
                return obj
    return obj


def _self_data(stay: int, other: str) -> dict:
    today = datetime.now(BEIJING).date()
    d = _shift(json.loads(SELF_FILE.read_text(encoding="utf-8")), (today - DATA_DAY).days)
    d["place"]["since"] = (today - timedelta(days=stay - 1)).isoformat()
    d["place"]["seen"] = (today - timedelta(days=1)).isoformat()
    d["plans"] = []
    d["said"] = [{"text": other, "source": "冒泡", "key": "group_10002", "at": _time.time() - 240}] if other else []
    return d


def _flags(reply: str) -> list[str]:
    out = []
    if LEAVE_RE.search(reply):
        out.append("提到走 / 待久：" + LEAVE_RE.search(reply).group(0))
    if SEA_RE.search(reply):
        out.append("还在讲海边")
    if PICK_RE.search(reply):
        out.append("捡到东西")
    return out


class _Bot:
    self_id = "123"

    def __init__(self):
        from types import SimpleNamespace
        self.config = SimpleNamespace(superusers=set(), command_start={"/"}, nickname=set())
        self.sent: list[str] = []

    async def send_group_msg(self, group_id, message):
        self.sent.append(str(message))

    async def send_private_msg(self, user_id, message):
        self.sent.append(str(message))

    async def call_api(self, api, **kw):
        raise RuntimeError("回放里不连 QQ")


@pytest.mark.asyncio
async def test_life_stay(monkeypatch):
    import plugins.roleplay_chat as p
    import importlib
    th = importlib.import_module("plugins.roleplay_chat.turn_hints")
    pg = importlib.import_module("plugins.roleplay_chat.proactive_group")
    pp = importlib.import_module("plugins.roleplay_chat.proactive_private")
    pc = importlib.import_module("plugins.roleplay_chat.proactive_common")
    assert SELF_FILE.exists(), f"没找到 {SELF_FILE}"
    if HOUR >= 0:
        class _Day(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz).replace(hour=HOUR, minute=7, second=0)
        monkeypatch.setattr(pg, "datetime", _Day)
        monkeypatch.setattr(th, "datetime", _Day)
    env = rc._env()
    prompts_seen: list[str] = []
    if FAKE:
        async def chat(**kw):
            prompts_seen.append(kw["messages"][-1]["content"])
            return rc._resp("在这片海边待了好些天了，过两天往北边去看看。")
    else:
        real = rc._real_client().chat.completions.create

        async def chat(**kw):
            prompts_seen.append(kw["messages"][-1]["content"])
            return await real(**kw)
    monkeypatch.setattr(p.client.chat.completions, "create", chat)
    for key, attr, conv in (("DEEPSEEK_MODEL", "deepseek_model", str), ("LLM_TEMPERATURE", "llm_temperature", float)):
        if env.get(key):
            monkeypatch.setattr(p.cfg, attr, conv(env[key]))
    monkeypatch.setattr(p.ltm, "enabled", False)
    monkeypatch.setattr(p.ltm, "mark_letter", lambda qq: None)
    monkeypatch.setattr(p.ltm, "get_user", lambda qq: {"qq": qq, "name": "小林", "facts": []})
    monkeypatch.setattr(p.ltm, "last_seen", lambda qq: _time.time() - 3 * 86400)
    monkeypatch.setattr(pc, "note_said", lambda *a, **k: None)          # 不交给近况整理
    sm = p.core.self_mem
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(sm, "path", tmp / "self.json")
    monkeypatch.setattr(sm, "enabled", True)
    monkeypatch.setattr(sm, "_save", lambda *a, **k: None)
    new_head, new_days, new_text = th.LIFE_ELSEWHERE_HEAD, p.cfg.life_long_stay_days, th.LIFE_LONG_STAY_TEXT

    results = []
    gid = 10001
    uid = 88000
    for cid, kind, stay, other, runs in CASES:
        if ONLY and cid not in ONLY:
            continue
        for var, n in runs.items():
            n = max(1, round(n * SCALE))
            if ONLY_V and var not in ONLY_V:
                continue
            monkeypatch.setattr(th, "LIFE_ELSEWHERE_HEAD", OLD_ELSEWHERE if var == "O" else new_head)
            monkeypatch.setattr(p.cfg, "life_long_stay_days", 0 if var == "O" else new_days)
            monkeypatch.setattr(th, "LIFE_LONG_STAY_TEXT", STAY_TEXTS.get(var, new_text))
            for k in range(n):
                sm._data = copy.deepcopy(_self_data(stay, other))
                p._histories.clear(); pg._interject_busy.clear()
                shutil.rmtree("data/history", ignore_errors=True)
                Path("data/history").mkdir(parents=True, exist_ok=True)
                now = _time.time()
                bot = _Bot()
                n0 = len(prompts_seen)
                if kind == "bubble":
                    p._histories[f"group_{gid}"] = [{"role": "user", "content": "【群友】晚安", "ts": now - 6 * 3600}]
                    await pg.bubble(bot, gid, force=True)
                else:
                    uid += 1
                    p._histories[f"private_{uid}"] = [{"role": "user", "content": "我先睡了", "ts": now - 3 * 86400},
                                                      {"role": "assistant", "content": "睡吧。", "ts": now - 3 * 86400 + 30}]
                    await pp.write_letter(bot, uid, manual=True)
                reply = " / ".join(bot.sent) or "（没说）"
                memo = prompts_seen[n0] if len(prompts_seen) > n0 else ""
                life = memo[memo.find("【你的近况】"):].split("\n\n")[0] if "【你的近况】" in memo else ""
                results.append({"id": cid, "kind": kind, "stay": stay, "variant": var, "sample": k, "reply": reply,
                                "life": life, "flags": _flags(reply)})
                print(f"{cid} {var}#{k}: {reply[:60]!r} {results[-1]['flags']}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    (OUT_DIR / f"待久回放_{stamp}.json").write_text(json.dumps({"fake": FAKE, "results": results}, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    md = [f"# 待久回放 {stamp}", "",
          "> 聊天 agent 的工具 `tools\\life_stay_compare.py`（《交接-聊天真实感三项-聊天-1109》C）。"
          + "；".join(f"{k} = {v}" for k, v in VARIANTS.items() if not ONLY_V or k in ONLY_V) + "。"
          + ("**程序自测（假模型）**" if FAKE else ""),
          f"> 提示里的“现在”按 {HOUR} 点算。" if HOUR >= 0 else "> 提示里的“现在”是真的时间。",
          "> 自动标记只是关键词：“提到走 / 待久”包括“再待几天”这种说不走的，要人看原话。", "",
          "## 汇总", "", "| 题目 | 做法 | 次数 | 提到走 / 待久 | 还在讲海边 | 捡到东西 |", "|---|---|---|---|---|---|"]
    for cid, kind, stay, other, runs in CASES:
        for var in runs:
            rs = [r for r in results if r["id"] == cid and r["variant"] == var]
            if rs:
                cnt = lambda pre: sum(any(f.startswith(pre) for f in r["flags"]) for r in rs)
                md.append(f"| {cid} | {var} | {len(rs)} | {cnt('提到走')} | {cnt('还在讲海边')} | {cnt('捡到东西')} |")
    md.append("")
    for cid, kind, stay, other, runs in CASES:
        rs = [r for r in results if r["id"] == cid]
        if not rs:
            continue
        md += [f"## {cid}（{'冒泡' if kind == 'bubble' else '写信'}，待了 {stay} 天" + (f"，另一个群刚才讲了“{other}”" if other else "") + "）", ""]
        for var in runs:
            one = next((r for r in rs if r["variant"] == var), None)
            if one:
                md += [f"<details><summary>{var} 她看到的【你的近况】</summary>", "", "```", one["life"], "```", "", "</details>", ""]
        md += ["| 做法 | 原话 | 标记 |", "|---|---|---|"]
        for r in rs:
            md.append(f"| {r['variant']}#{r['sample']} | {r['reply'].replace('|', '｜').replace(chr(10), ' / ')} | {'；'.join(r['flags'])} |")
        md.append("")
    out = OUT_DIR / f"待久回放_{stamp}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n结果在 {out}")
