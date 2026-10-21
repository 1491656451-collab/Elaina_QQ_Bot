"""措辞对比（10/18）：她有些话不像正常人说的（“找你的人别只拿戳的”“我又不是许愿池里的雕像”），
拿当时真实的上下文，用几种做法各重新生成几次，看哪种说得更通顺、更像真人随手打的字。

做法（VARIANTS）：
  A 现在的样子
  B 规则加一条“说人话”
  C 放松“每次说法不一样”，挖苦从“偶尔要有”改成“想到了才说”
  D 温度 1.0（其他和 A 一样）
  E B + C
  F B + 温度 1.0（10/18 第二轮加的）
  L / M / H：日常闲聊的长短提示固定成一档（10/18 第三轮）：L 现在的“可以多说两句”，M 新写法“多说一两句，说完就停”，H “一句话”

怎么用：双击 bot\\措辞对比.bat。会把代码拷到临时目录，用 pytest 跑这个文件；不碰真的 data。
- 题目：tools\\phrasing_cases.json。群聊题借用 replay_chat.py 的回放（旁听、合并、拼提示都走真实流程）；私聊题按 NapCat 日志把前面的聊天放进记录，再把关键那几句交给真实流程
- 长期记忆关掉（服务器上的档案是事后的），每个人的关系档位按题目里写的；识图关掉
- 结果在 docs\\措辞对比\\：
  措辞对比_时间.json（全部原话，标了是哪种做法）
  措辞对比_时间_盲评.md（每题的上下文 + 打乱顺序、只标编号的回复，用来盲评）
  措辞对比_时间_答案.json（编号对应哪种做法）
环境变量（bat 会设好）：REPLAY_BOT = bot 目录；PHRASING_ONLY = 只跑某几题（G1,P1）；PHRASING_VARIANTS = 只跑某几种（A,B）；PHRASING_SAMPLES = 每种每题几次
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nonebug import App
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

import replay_chat as rc

BEIJING = timezone(timedelta(hours=8))
BOT_DIR = Path(os.environ.get("REPLAY_BOT", ".")).resolve()
CASES_FILE = BOT_DIR / "tools" / "phrasing_cases.json"
OUT_DIR = BOT_DIR.parent / "docs" / "措辞对比"
ONLY = [x for x in os.environ.get("PHRASING_ONLY", "").split(",") if x]
ONLY_V = [x for x in os.environ.get("PHRASING_VARIANTS", "").split(",") if x]

# ------------------------------------------------------------------ 几种做法
SPEAK_PLAIN = ("- 说人话：像真人在 QQ 上随手打字，一句话让人一读就懂。宁可说得平常，也别硬凹比喻、金句和绕口的说法；"
               "意思没说完就多说几个字，别压成半句；对方没说过的事（“天天问候”“搭档”这类）别凭空接。")

# “每次说法不一样”“换着说”这类：删掉（C、E 用）
_VARY = [
    "，每次说法不一样", "，说法每次不同", "这些情况每次的说法都不一样，别用同一句。", "，说法每次不一样", "，每次换一种",
    "；每次的反应都不一样", "反应每次不一样，", "每次的反应都不一样，别总是同一句。", "，每次换个角度，别老拿“闲”说事",
    "，怎么说每次都不一样", "，每次换个说法", "同一个意思每次换个说法，长短、语气词、先说什么后说什么都可以变。",
    "，但每次的反应不一样", "，每次说法不同", "……每次不一样", "；每次换一种", "，每次挑的角度不一样",
]
# 挖苦从“偶尔要有”改成“想到了才说”
_TEASE = [
    ("偶尔挖苦对方一下", "想到了才挖苦一句，没想到就正常聊"),
    ("偶尔挖苦一下，点到为止", "想到了才挖苦一句，点到为止"),
    ("再顺手损对方一句", "想到了就顺手损一句，没想到就算了"),
]


def _relax(text: str, hits: dict) -> str:
    for s in _VARY:
        if s in text:
            hits[s] = hits.get(s, 0) + text.count(s)
            text = text.replace(s, "")
    for a, b in _TEASE:
        if a in text:
            hits[a] = hits.get(a, 0) + text.count(a)
            text = text.replace(a, b)
    return text


VARIANTS = {"A": "现在的样子", "B": "加“说人话”", "C": "放松“换着说”和挖苦", "D": "温度 1.0", "E": "B + C", "F": "B + 温度 1.0",
            "L": "长短固定为“可以多说两句”（现在那档）", "M": "长短固定为新写法“多说一两句，说完就停”", "H": "长短固定为“一句话”"}

# 10/18 第三轮：怪的常是第二条（“半个月。/ 算了，你说的戏码我就当看戏”），怀疑是“可以多说两句”这档让她硬凑一句
LEN_MORE_NEW = "（这轮可以多说一两句，用换行分开，像连着发了几条消息，总共不超过 40 字；不过意思一句就说完了的话就停，别为了凑够几句硬补一句。）"


def apply_variant(p, v: str, base: dict) -> dict:
    """把插件改成这种做法；返回替换命中的情况（检查替换有没有生效）"""
    pr = getattr(p, "prompts", p)   # 10/22 拆分后人设和聊天规则在 prompts.py，要改那边的才生效
    pr._persona, pr.CHAT_RULES = base["persona"], base["rules"]
    p.FAMILIARITY_HINT.clear(); p.FAMILIARITY_HINT.update(base["fam"])
    p.cfg.llm_temperature = base["temp"]
    p.SHORT_VARIANTS[:] = base["short"]
    hits: dict = {}
    if v in ("L", "M", "H"):                     # 只管日常闲聊那几档长短（讲故事、倾诉等不变）
        pick = {"L": base["short"][2][1], "M": LEN_MORE_NEW, "H": base["short"][1][1]}[v]
        p.SHORT_VARIANTS[:] = [(1.0, pick), (1.0, pick)]    # 对方写了 30 字以上时 short_hint 会跳过第一档，所以放两份
        hits["长短"] = pick[:12]
    if v in ("B", "E", "F"):
        pr.CHAT_RULES = pr.CHAT_RULES.replace("\n- 不要主动问对方是男是女。", "\n" + SPEAK_PLAIN + "\n- 不要主动问对方是男是女。")
        hits["说人话"] = int(SPEAK_PLAIN in pr.CHAT_RULES)
    if v in ("C", "E"):
        pr._persona = _relax(pr._persona, hits)
        pr.CHAT_RULES = _relax(pr.CHAT_RULES, hits)
        for k in list(p.FAMILIARITY_HINT):
            p.FAMILIARITY_HINT[k] = _relax(p.FAMILIARITY_HINT[k], hits)
    if v in ("D", "F"):
        p.cfg.llm_temperature = 1.0
    return hits


# ------------------------------------------------------------------ 私聊回放
_NCP_RE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) \S+ (?:\S+ \| )?(接收 <-|发送 ->) 私聊 \((\d+)\) (.*)$")
_PQ_RE = re.compile(r"^\[回复消息 私聊 \(\d+\) .*?\] ")


def read_private(folder: str, uid: int) -> list[tuple[float, str, str]]:
    """(时间, user/assistant, 文字)；去掉指令和指令的回复、去掉 NapCat 重连时重复的行"""
    out, seen = [], set()
    for f in sorted((rc.LOG_ROOT / folder).glob("napcat*.log")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = _NCP_RE.match(re.sub(r"\x1b\[[0-9;]*m", "", line))
            if not m or int(m.group(3)) != uid:
                continue
            role = "user" if m.group(2).startswith("接收") else "assistant"
            text = _PQ_RE.sub("", m.group(4)).strip()
            if not text or text.startswith(("/", "／")) or (role == "assistant" and text.startswith(("（", "【QQ"))):
                continue
            key = (role, text)
            t = rc._ts(m.group(1))
            if key in seen and any(abs(t - x[0]) < 1 for x in out if (x[1], x[2]) == key):
                continue
            seen.add(key)
            out.append((t, role, text))
    return sorted(out, key=lambda x: x[0])


class _Bot:
    self_id = str(rc.SELF_ID)
    config = types.SimpleNamespace(superusers=set(), command_start={"/"}, nickname=set())

    def __init__(self):
        self.sent = []

    async def send(self, event, msg, **kw):
        self.sent.append(str(msg))
        return {"message_id": 1}

    async def call_api(self, api, **data):
        if "message" in data:
            self.sent.append(str(data["message"]))
        return {"message_id": 1}

    async def send_private_msg(self, user_id, message):
        self.sent.append(str(message))
        return {"message_id": 1}


async def replay_private(p, case: dict, lines: list, real) -> dict:
    uid = int(case["user"])
    target_t = rc._ts(f"{case['date']} {case['target']}")
    start = target_t - float(case.get("window_min", 15)) * 60
    before = [x for x in lines if start <= x[0] <= target_t + 0.5]
    # 关键那一轮：她最后一次开口之后、到关键那句为止，对方连着发的几句
    last_a = max((i for i, x in enumerate(before) if x[1] == "assistant"), default=-1)
    burst = [x for x in before[last_a + 1:] if x[1] == "user"]
    assert burst, f"{case['id']}：找不到关键那句"
    hist, cur = [], None
    for t, role, text in before[:last_a + 1]:
        if cur and cur["role"] == role and t - cur["ts"] < 120:
            cur["content"] += "\n" + text
            cur["ts"] = t
        else:
            cur = {"role": role, "content": text, "ts": t}
            if role == "user":
                cur.update({"uid": uid, "name": "群鲨鱼"})
            hist.append(cur)
    p._histories[f"private_{uid}"] = hist
    clock = rc.Clock()
    clock.now = target_t
    rc.set_plugin_attr(p, "time", clock)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            d = datetime.fromtimestamp(clock.now, tz or BEIJING)
            return d if tz else d.replace(tzinfo=None)
    rc.set_plugin_attr(p, "datetime", _DT)
    state = {"prompt": None}

    async def create(**kw):
        if "response_format" in kw:
            return rc._resp('{"ok": true}')
        if kw.get("max_tokens") == 2:
            return rc._resp("是")
        state["prompt"] = kw.get("messages")
        return await real(**kw)
    p.client.chat.completions.create = create
    text = "\n".join(x[2] for x in burst)
    msg = Message(text)
    ev = PrivateMessageEvent(time=int(target_t), self_id=rc.SELF_ID, post_type="message", sub_type="friend", user_id=uid,
                             message_type="private", message_id=900000 + int(target_t) % 100000, message=msg, original_message=msg,
                             raw_message=text, font=0, sender=Sender(user_id=uid, nickname="群鲨鱼"), to_me=True)
    bot = _Bot()
    await p.converse(bot, ev)
    sent = [re.sub(r"\[CQ:reply,id=\d+\]|\[reply:id=\d+\]", "", s).strip() for s in bot.sent]
    return {"reply": "\n".join(x for x in sent if x), "prompt": state["prompt"]}


def context_lines(case: dict, events_cache: dict) -> list[str]:
    """盲评时给看的上下文：关键那句之前的最后几句（她当时说的照原样）"""
    target_t = rc._ts(f"{case['date']} {case['target']}")
    if case["kind"] == "private":
        lines = read_private(case["log"], int(case["user"]))
        prior = [x for x in lines if x[0] <= target_t + 0.5][-8:]
        return [f"{datetime.fromtimestamp(t, BEIJING):%m-%d %H:%M} {'她' if r == 'assistant' else '对方'}：{s}" for t, r, s in prior]
    events, sends = events_cache[(case["log"], case["group"])]
    def _text(raw):
        try:
            t = "".join(s.data.get("text", "") for s in rc._segments(raw) if s.type == "text").strip()
        except Exception:  # noqa: BLE001
            t = ""
        return t or "[图片/表情]"
    rows = [(e["t"], e["name"], _text(e["raw"])) for e in events if target_t - 900 <= e["t"] <= target_t + 0.5]
    rows += [(t, "她", s) for t, s, _, _ in sends if target_t - 900 <= t < target_t]
    rows.sort(key=lambda x: x[0])
    rows = rows[-10:]
    seeds = [(rc._ts(f"{case['date']} {x['at']}"), "她", x["content"].replace("\n", " / ")) for x in case.get("seed", [])]
    seeds = [x for x in seeds if not rows or x[0] < rows[0][0]]        # 回放前放进记录的话（冒泡），盲评也要看得到（10/18 第一轮 G6 漏了）
    return [f"{datetime.fromtimestamp(t, BEIJING):%m-%d %H:%M} {n}：{s}" for t, n, s in seeds + rows]


def _cases() -> tuple[list, int, list]:
    d = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    only = ONLY or d.get("only") or []
    cases = [c for c in d["cases"] if not only or c["id"] in only]
    variants = ONLY_V or d.get("variants") or list(VARIANTS)
    return cases, int(os.environ.get("PHRASING_SAMPLES") or d.get("samples", 3)), [v for v in VARIANTS if v in variants]


@pytest.mark.asyncio
async def test_phrasing(app: App, monkeypatch):
    import plugins.roleplay_chat as p
    fake = os.environ.get("REPLAY_FAKE") == "1"
    if fake:                                            # 程序自测：不调模型
        async def real(**kw):
            if kw.get("max_tokens") == 2:
                return rc._resp("是")
            sys_text = "".join(str(m.get("content")) for m in kw.get("messages") or [] if m.get("role") == "system")
            flags = [f"温度{kw.get('temperature')}", "说人话" if "说人话：" in sys_text else "", "换着说" if "每次换个说法" in sys_text else "",
                     "有日期" if "【现在】" in sys_text else ""]
            return rc._resp("好的 " + " ".join(x for x in flags if x))
    else:
        real = rc._real_client().chat.completions.create
    env = rc._env()
    for key, attr, conv in (("DEEPSEEK_MODEL", "deepseek_model", str), ("LLM_TEMPERATURE", "llm_temperature", float),
                            ("LLM_THINKING", "llm_thinking", lambda v: v.lower() in ("1", "true", "yes"))):
        if env.get(key):
            monkeypatch.setattr(p.cfg, attr, conv(env[key]))
    monkeypatch.setattr(p.ltm, "enabled", False)
    monkeypatch.setattr(p.cfg, "vision_enabled", False)
    monkeypatch.setattr(p.cfg, "smart_min_interval", 0)
    monkeypatch.setattr(p.cfg, "multi_message", False)
    monkeypatch.setattr(p.cfg, "dislike_ignore_prob", 0.0)       # 讨厌的人也照回（只看说得顺不顺）
    nicks = rc._nicknames()
    import nonebot
    monkeypatch.setattr(nonebot.get_driver().config, "nickname", set(nicks))
    pr = getattr(p, "prompts", p)
    base = {"persona": pr._persona, "rules": pr.CHAT_RULES, "fam": dict(p.FAMILIARITY_HINT), "temp": p.cfg.llm_temperature,
            "short": list(p.SHORT_VARIANTS)}
    assert "多说两句" in base["short"][2][1] and "一句话" in base["short"][1][1], "SHORT_VARIANTS 的顺序变了，L/H 要跟着改"
    real_time_mod, real_dt, real_fam = p.time, p.datetime, p.familiarity_of
    cases, samples, variants = _cases()
    events_cache, private_cache, results, hits_all = {}, {}, [], {}
    try:
        for case in cases:
            if case["kind"] == "group" and (case["log"], case["group"]) not in events_cache:
                events_cache[(case["log"], case["group"])] = rc.read_log(case["log"], int(case["group"]))
            if case["kind"] == "private" and (case["log"], case["user"]) not in private_cache:
                private_cache[(case["log"], case["user"])] = read_private(case["log"], int(case["user"]))
            fam_map = case.get("fam")
            if isinstance(fam_map, dict):
                rc.set_plugin_attr(p, "familiarity_of", lambda q, m=fam_map: m.get(str(q), "stranger"))
            else:
                rc.set_plugin_attr(p, "familiarity_of", lambda q, f=fam_map or "stranger": f)
            for v in variants:
                hits_all[v] = apply_variant(p, v, base)
                for k in range(samples):
                    p._histories.clear(); p._passive.clear(); p._last_trigger.clear(); p._global_window.clear()
                    p._hour_window.clear(); p._group_hour.clear(); p._farewell_at.clear(); p._recent_chat.clear()
                    p._last_bot_msg.clear(); p._chat_seq.clear(); p._arrival_seq.clear(); p._private_hour.clear(); p._away.clear()
                    rc.set_plugin_attr(p, "time", real_time_mod); rc.set_plugin_attr(p, "datetime", real_dt)
                    shutil.rmtree("data/history", ignore_errors=True)
                    Path("data/history").mkdir(parents=True, exist_ok=True)
                    if case["kind"] == "group":
                        c = dict(case, force_reply=True)
                        if case.get("seed_start"):          # 回放从这里开始（之前是冒泡，用 seed 放进记录）
                            c["window_min"] = (rc._ts(f"{case['date']} {case['target']}") - rc._ts(f"{case['date']} {case['seed_start']}")) / 60
                        if case.get("seed"):
                            p._histories[f"group_{case['group']}"] = [
                                {"role": "assistant", "content": s["content"], "ts": rc._ts(f"{case['date']} {s['at']}")} for s in case["seed"]]
                        events, sends = events_cache[(case["log"], case["group"])]
                        r = await rc.replay_once(app, p, c, events, sends, real, nicks)
                    else:
                        r = await replay_private(p, case, private_cache[(case["log"], case["user"])], real)
                    row = {"id": case["id"], "variant": v, "sample": k, "reply": r["reply"]}
                    if v == variants[0] and k == 0:         # 每题留一份提示（最后几条），查回放对不对
                        row["prompt_tail"] = [{"role": m["role"], "content": str(m["content"])[:1500]} for m in (r["prompt"] or [])[-5:]]
                    results.append(row)
                    print(f"{case['id']} {v}#{k}: {r['reply'][:60]!r}")
    finally:
        rc.set_plugin_attr(p, "time", real_time_mod); rc.set_plugin_attr(p, "datetime", real_dt); rc.set_plugin_attr(p, "familiarity_of", real_fam)
        apply_variant(p, "A", base)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    temp = base["temp"]
    meta = {"stamp": stamp, "variants": {v: VARIANTS[v] for v in variants}, "base_temperature": temp,
            "replacement_hits": hits_all, "samples": samples, "fake": fake}
    (OUT_DIR / f"措辞对比_{stamp}.json").write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    # 盲评：每题的回复打乱，只标编号
    rnd = random.Random(stamp)
    key, md = {}, [f"# 措辞对比 {stamp}：盲评用", "",
                   "每题先看上下文，再看下面打乱顺序的回复。给每条打分：2 = 像正常人随口说的、一读就懂；1 = 能懂但有点别扭；0 = 读不通、硬凹、接不上。",
                   "不用管她该不该提面包、记没记错事，只看话说得顺不顺、像不像真人。", ""]
    for case in cases:
        rs = [r for r in results if r["id"] == case["id"]]
        rnd.shuffle(rs)
        md += [f"## {case['id']}", "", "上下文（她的话是当时真实说的）：", "", "```"]
        md += context_lines(case, events_cache)
        md += ["```", "", f"当时她回的：{case.get('orig', '')}", ""]
        for i, r in enumerate(rs, 1):
            code = f"{case['id']}-{i:02d}"
            key[code] = r["variant"]
            md.append(f"- **{code}** {r['reply'].replace(chr(10), ' / ') or '（没回）'}")
        md.append("")
    (OUT_DIR / f"措辞对比_{stamp}_盲评.md").write_text("\n".join(md), encoding="utf-8")
    (OUT_DIR / f"措辞对比_{stamp}_答案.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果在 {OUT_DIR}（措辞对比_{stamp}_盲评.md 用来盲评；答案在 措辞对比_{stamp}_答案.json）")
    for v, h in hits_all.items():
        print(f"做法 {v}（{VARIANTS[v]}）替换命中：{h}")
