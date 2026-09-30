"""对话回放测试（9/30）：把服务器日志里一段真实的群聊，按原来的顺序、原来的时间间隔，喂给**现在的聊天代码**，
看她在关键那一句上怎么回。和 regression.py 的区别：regression 只截一两句拼提示；这里走的是完整的流程
（旁听、要不要回、合并连发、拼提示、检查），能还原“前面聊了什么、隔了多久”这些现场。

怎么用：双击 bot\\对话回放测试.bat（会把代码拷到临时目录，用 pytest 跑这个文件；不碰真的 data）。
- 题目：tools\\replay_cases.json（每题：哪份日志、哪个群、关键那句的时间、往前带几分钟、怎么判）
- 关键那句之前：她每次该回的，直接用她当时真实发出去的话（不调模型；按引用和先后对到是回哪条的，当时没回的就不出声）；要不要回，按日志里当时的判断
  （“没 @ 也回复 / 接着说”算回，“判断：不是在跟她说话”算不回），这样前文和当时一样
- 关键那句：真的调 DeepSeek，每题问几次（samples），每次都从头回放
- 插件里的 time / datetime 换成回放到的时间（“隔了多久”“现在几点”和当时一样）
- 长期记忆关掉（服务器上的档案是事后的，可能已经记下了“那句不是问你的”，会漏答案）；识图关掉；高峰、休息、限额按测试配置关掉
- 结果：docs\\对话回放\\回放_时间_版本.json 和 .md

环境变量（bat 会设好）：REPLAY_BOT = bot 目录，REPLAY_TAG = 这次是哪个版本（“改后”或备份名）
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import time as _real_time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nonebot
import pytest
from nonebug import App
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

BEIJING = timezone(timedelta(hours=8))
SELF_ID = 3394537326
BOT_DIR = Path(os.environ.get("REPLAY_BOT", ".")).resolve()
LOG_ROOT = BOT_DIR.parent / "server_logs"
OUT_DIR = BOT_DIR.parent / "docs" / "对话回放"
TAG = os.environ.get("REPLAY_TAG", "改后")
CASES_FILE = BOT_DIR / "tools" / "replay_cases.json"
ONLY = os.environ.get("REPLAY_ONLY", "")          # 只跑某几题：R01,R02


# ------------------------------------------------------------------ 读日志
_EV_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \| SUCCESS .*\[message\.group\.normal\]: Message (\d+) from (\d+)@\[群:(\d+)\] (.+)$")
_YES_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \| INFO .*_addressed:\d+ - (?:没 @ 也回复|接着说，直接回)：群(\d+) ")
_NO_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \| INFO .*_judge:\d+ - 判断：不是在跟她说话 群(\d+) ")
_NC_RE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) \S+ (?:\S+ \| )?(接收 <-|发送 ->) 群聊 \[[^\]]*\((\d+)\)\] \[(.*?)\((\d+)\)\] (.*)$")
_QUOTE_RE = re.compile(r"^\[回复消息 \[(.*?)\((\d+)\)\] (.*?)\] ")


def _ts(s: str, year: int = 2026) -> float:
    if len(s) == 14:                                    # napcat：09-30 13:07:50
        s = f"{year}-{s}"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING).timestamp()


def _segments(raw: str) -> Message:
    """把日志里的消息（“[reply:id=1][at:qq=2] 文字[image:summary=[动画表情],file=x]”）还原成 Message"""
    msg, i, buf = Message(), 0, ""
    while i < len(raw):
        m = re.match(r"\[(\w+):", raw[i:])
        if m:
            depth, j = 0, i
            while j < len(raw):
                if raw[j] == "[":
                    depth += 1
                elif raw[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            body = raw[i + len(m.group(0)):j]
            if buf:
                msg += MessageSegment.text(buf)
                buf = ""
            data = {}
            for part in re.split(r",(?=\w+=)", body):
                if "=" in part:
                    k, v = part.split("=", 1)
                    data[k] = v
            msg += MessageSegment(m.group(1), data)
            i = j + 1
        else:
            buf += raw[i]
            i += 1
    if buf:
        msg += MessageSegment.text(buf)
    return msg


def read_log(folder: str, gid: int):
    """这个群的消息（时间, 消息号, QQ, 名字, Message, 回复了谁）、她发出去的话（时间, 文字）、当时判断回不回（时间 → True/False）"""
    d = LOG_ROOT / folder
    events, sends, decided, names, quotes = [], [], {}, {}, {}
    for f in sorted(d.glob("napcat*.log")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            m = _NC_RE.match(line)
            if not m or int(m.group(3)) != gid:
                continue
            t, direction, name, uid, text = _ts(m.group(1)), m.group(2), m.group(4), int(m.group(5)), m.group(6)
            q = _QUOTE_RE.match(text)
            if direction.startswith("发送"):
                sends.append((t, (text[q.end():] if q else text).strip(), int(q.group(2)) if q else None, q.group(3) if q else None))
            else:
                names[uid] = name
                if q:
                    quotes.setdefault((int(t), uid), (q.group(1), int(q.group(2)), q.group(3)))
    for f in sorted(d.glob("bot_*.log")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = _EV_RE.match(line)
            if m and int(m.group(4)) == gid:
                try:
                    raw = ast.literal_eval(m.group(5))
                except (ValueError, SyntaxError):
                    raw = m.group(5).strip("'\"")
                t, uid = _ts(m.group(1)), int(m.group(3))
                events.append({"t": t, "mid": int(m.group(2)), "uid": uid, "raw": raw})
                continue
            m = _YES_RE.match(line) or _NO_RE.match(line)
            if m and int(m.group(2)) == gid:
                decided[_ts(m.group(1))] = "判断" not in line
    for e in events:
        e["name"] = names.get(e["uid"], str(e["uid"]))
        e["quote"] = quotes.get((int(e["t"]), e["uid"])) or quotes.get((int(e["t"]) + 1, e["uid"])) or quotes.get((int(e["t"]) - 1, e["uid"]))
        e["decided"] = next((v for t, v in decided.items() if 0 <= t - e["t"] <= 3), None)
    return events, sorted(sends)


def _plain(raw: str) -> str:
    return re.sub(r"\[\w+:[^\]]*\]|\s", "", raw)


def assign_replies(window: list, sends: list, before: float, addressed) -> dict:
    """把她当时发出去的话分成一段一段（带引用的另起一段；隔 30 秒以上另起一段），
    每段算作回的是哪条消息：带引用的按引用找；不带的，算给之前最近一条“当时回了”、还没分到回复的消息。
    返回 {消息号: (这段的话, 发完的时间)}"""
    bursts = []
    for t, text, q_uid, q_text in sends:
        if t >= before:
            break
        if not bursts or q_uid is not None or t - bursts[-1]["end"] > 30:
            bursts.append({"start": t, "end": t, "lines": [text], "q_uid": q_uid, "q_text": q_text})
        else:
            bursts[-1]["lines"].append(text)
            bursts[-1]["end"] = t
    out = {}
    for b in bursts:
        prior = [e for e in window if e["t"] <= b["start"]]
        hit = None
        if b["q_uid"] is not None:
            key = _plain(b["q_text"] or "")[:6]
            hit = next((e for e in reversed(prior) if e["uid"] == b["q_uid"] and key and key in _plain(e["raw"])), None)
            if hit is not None and hit["mid"] in out:      # 同一条回了两段：接在后面
                lines, _ = out[hit["mid"]]
                out[hit["mid"]] = (lines + b["lines"], b["end"])
                continue
        if hit is None:
            hit = next((e for e in reversed(prior) if addressed(e) and e["mid"] not in out), None)
        if hit is not None:
            out[hit["mid"]] = (b["lines"], b["end"])
    return out


# ------------------------------------------------------------------ 做成事件（照着 OneBot 适配器的规则认 to_me）
def _nicknames() -> list[str]:
    try:
        for line in (BOT_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
            if line.strip().upper().startswith("NICKNAME="):
                v = line.split("=", 1)[1].split(" #")[0].strip()
                return [str(x) for x in json.loads(v)]
    except (OSError, ValueError):
        pass
    return ["伊蕾娜小姐"]


def make_event(e: dict, gid: int, nicks: list[str]) -> GroupMessageEvent:
    msg = _segments(e["raw"])
    to_me, reply = False, None
    idx = next((i for i, s in enumerate(msg) if s.type == "reply"), None)
    if idx is not None:
        q = e.get("quote")
        if q:
            reply = Reply(time=int(e["t"]), message_type="group", message_id=int(msg[idx].data.get("id", 0) or 0),
                          real_id=int(msg[idx].data.get("id", 0) or 0),
                          sender=Sender(user_id=q[1], nickname=q[0], card=q[0]), message=Message(q[2]))
            to_me = q[1] == SELF_ID
        del msg[idx]
        if reply is not None and len(msg) > idx and msg[idx].type == "at" and str(msg[idx].data.get("qq")) == str(reply.sender.user_id):
            del msg[idx]
    for i in (0, len(msg) - 1):                         # 开头或结尾 @ 她
        if 0 <= i < len(msg) and msg[i].type == "at" and str(msg[i].data.get("qq")) == str(SELF_ID):
            to_me = True
            del msg[i]
            break
    if msg and msg[0].type == "text":                   # 以昵称开头
        text = msg[0].data["text"].lstrip()
        for n in sorted(nicks, key=len, reverse=True):
            mm = re.match(rf"^{re.escape(n)}([\s,，]*|$)", text, re.I)
            if mm:
                to_me = True
                msg[0].data["text"] = text[mm.end():]
                break
    if not msg or not str(msg).strip():
        msg = Message(MessageSegment.text(""))
    ev = GroupMessageEvent(time=int(e["t"]), self_id=SELF_ID, post_type="message", sub_type="normal", user_id=e["uid"],
                           message_type="group", message_id=e["mid"], message=msg, original_message=msg, raw_message=str(msg),
                           font=0, sender=Sender(user_id=e["uid"], nickname=e["name"], card=e["name"]), to_me=to_me, group_id=gid)
    if reply is not None:
        ev.reply = reply
    return ev


# ------------------------------------------------------------------ 回放
class Clock:
    """替换插件里的 time 模块：time() / monotonic() 返回回放到的时间，别的照旧"""
    def __init__(self):
        self.now = _real_time.time()

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def __getattr__(self, k):
        return getattr(_real_time, k)


def _env() -> dict:
    """读 bot\\.env（只在内存里用，不打印）"""
    env = {}
    try:
        for line in (BOT_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip().upper()] = v.split(" #")[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _real_client():
    from openai import AsyncOpenAI
    env = _env()
    return AsyncOpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)


def _resp(content: str):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content), finish_reason="stop")],
                                 usage=None)


def judge_reply(case: dict, reply: str) -> list[str]:
    bad = []
    if case.get("not") and re.search(case["not"], reply):
        bad.append("出现了不该有的：" + re.search(case["not"], reply).group(0))
    if case.get("must") and not re.search(case["must"], reply):
        bad.append("缺了该有的")
    if case.get("should_reply") is False and reply:
        bad.append("不该回却回了")
    if case.get("should_reply", True) and not reply and not case.get("allow_skip"):
        bad.append("该回却没回")
    return bad


async def replay_once(app: App, p, case: dict, events: list, sends: list, real, nicks: list[str]) -> dict:
    """从窗口开头回放到关键那句；返回她在关键那句上发出去的话、那次的提示"""
    gid = int(case["group"])
    target_t = _ts(f"{case['date']} {case['target']}")
    start = target_t - float(case.get("window_min", 15)) * 60
    window = [e for e in events if start <= e["t"] <= target_t + 0.5]
    target = next((e for e in reversed(window) if abs(e["t"] - target_t) < 1.5 and (not case.get("text") or case["text"] in e["raw"])), None)
    assert target, f"{case['id']}：日志里找不到 {case['target']} 那句"
    clock = Clock()
    p.time = clock

    class _DT(datetime):                                # 插件里的 datetime.now() 也用回放到的时间（深夜提示、日期）
        @classmethod
        def now(cls, tz=None):
            d = datetime.fromtimestamp(clock.now, tz or BEIJING)
            return d if tz else d.replace(tzinfo=None)
    p.datetime = _DT
    state = {"phase": "before", "prompt": None, "sent": [], "mid": None}
    def addressed(e):
        return e.get("decided") is True or make_event(e, gid, nicks).to_me
    replies = assign_replies(window, [x for x in sends if x[0] >= start], target_t, addressed)

    async def create(**kw):
        msgs = kw.get("messages") or []
        if kw.get("max_tokens") == 2:                   # 要不要回
            if state["phase"] == "target":
                return await real(**kw)
            return _resp("是" if state.get("decided") else "否")
        if "response_format" in kw:                     # 核对往事、记忆整理
            if state["phase"] == "target":
                return await real(**kw)
            return _resp('{"ok": true}')
        if state["phase"] == "target":
            state["prompt"] = msgs
            return await real(**kw)
        # 关键那句之前：用她当时回这条消息真实发出去的话；当时没回、现在要回的，让她“[不回]”（和当时一样不出声）
        got = replies.get(state["mid"])
        if got is None:
            return _resp("[不回]")
        lines, end = got
        replies.pop(state["mid"])
        clock.now = max(clock.now, end)                 # 时间走到她当时发完的那一刻（“她回完之后谁又说了话”要和当时一样）
        return _resp("\n".join(lines))

    p.client.chat.completions.create = create

    def got_send(bot, event, message, **kw):
        if state["phase"] == "target":
            state["sent"].append(str(message))
        return {"message_id": 1}

    def got_api(adapter, api, **data):
        if api in ("send_group_msg", "send_msg") and state["phase"] == "target":
            state["sent"].append(str(data.get("message", "")))
        return {"message_id": 1}

    for e in window:
        clock.now = e["t"]
        state["decided"] = e.get("decided")
        state["mid"] = e["mid"]
        is_target = e is target
        if is_target:
            state["phase"] = "target"
        ev = make_event(e, gid, nicks)
        if is_target and case.get("force_reply"):
            ev.to_me = True
        async with app.test_matcher() as ctx:
            ctx.got_call_send = got_send
            ctx.got_call_api = got_api
            bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id=str(SELF_ID))
            ctx.receive_event(bot, ev)
        if is_target:
            break
    sent = [re.sub(r"\[CQ:reply,id=\d+\]|\[reply:id=\d+\]", "", s).strip() for s in state["sent"]]
    return {"reply": "\n".join(x for x in sent if x), "prompt": state["prompt"]}


def _cases():
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    if ONLY:
        cases = [c for c in cases if c["id"] in ONLY.split(",")]
    return cases


@pytest.mark.asyncio
async def test_replay(app: App, monkeypatch):
    import plugins.roleplay_chat as p
    real = _real_client().chat.completions.create if os.environ.get("REPLAY_FAKE") != "1" else None
    if real is None:                                    # 程序自测：不调模型
        async def real(**kw):
            return _resp("是" if kw.get("max_tokens") == 2 else "好的，知道了。")
    env = _env()                                        # 模型、温度和线上一样（照 .env）
    for key, attr, conv in (("DEEPSEEK_MODEL", "deepseek_model", str), ("LLM_TEMPERATURE", "llm_temperature", float),
                            ("LLM_THINKING", "llm_thinking", lambda v: v.lower() in ("1", "true", "yes"))):
        if env.get(key):
            monkeypatch.setattr(p.cfg, attr, conv(env[key]))
    monkeypatch.setattr(p.ltm, "enabled", False)
    monkeypatch.setattr(p.cfg, "vision_enabled", False)
    monkeypatch.setattr(p.cfg, "smart_min_interval", 0)
    nicks = _nicknames()
    monkeypatch.setattr(nonebot.get_driver().config, "nickname", set(nicks))
    real_time_mod, real_dt = p.time, p.datetime
    results = []
    try:
        for case in _cases():
            events, sends = read_log(case["log"], int(case["group"]))
            for k in range(int(case.get("samples", 3))):
                p._histories.clear(); p._passive.clear(); p._last_trigger.clear(); p._global_window.clear()
                p._hour_window.clear(); p._group_hour.clear(); p._farewell_at.clear(); p._recent_chat.clear()
                p._last_bot_msg.clear(); p._chat_seq.clear(); p._arrival_seq.clear(); p._private_hour.clear()
                shutil.rmtree("data/history", ignore_errors=True)
                Path("data/history").mkdir(parents=True, exist_ok=True)
                r = await replay_once(app, p, case, events, sends, real, nicks)
                results.append({"id": case["id"], "desc": case.get("desc", ""), "sample": k, "reply": r["reply"],
                                "bad": judge_reply(case, r["reply"]),
                                "prompt_tail": [m for m in (r["prompt"] or [])[-4:]]})
    finally:
        p.time, p.datetime = real_time_mod, real_dt
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    safe_tag = re.sub(r"[^\w]", "_", TAG)
    (OUT_DIR / f"回放_{stamp}_{safe_tag}.json").write_text(json.dumps({"tag": TAG, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# 对话回放 {stamp}（{TAG}）", ""]
    for case in _cases():
        rs = [r for r in results if r["id"] == case["id"]]
        bad = sum(1 for r in rs if r["bad"])
        lines += [f"## {case['id']} {case.get('desc', '')}", "", f"翻车 {bad}/{len(rs)}", ""]
        lines += [f"- {r['reply'].replace(chr(10), ' / ') or '（没回）'}" + (f"　〔{'；'.join(r['bad'])}〕" if r["bad"] else "") for r in rs]
        if rs and rs[0]["prompt_tail"]:
            lines += ["", "<details><summary>第 1 次的提示（最后几条）</summary>", "", "```"]
            for m in rs[0]["prompt_tail"]:
                lines.append(f"[{m['role']}] {str(m['content'])[:600]}")
            lines += ["```", "", "</details>", ""]
    (OUT_DIR / f"回放_{stamp}_{safe_tag}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n结果在 {OUT_DIR / f'回放_{stamp}_{safe_tag}.md'}")
