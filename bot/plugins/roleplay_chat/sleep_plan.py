"""她自己定今晚的作息（11/13，聊天 agent，《方案-她自己定作息-1113》）：每天 SLEEP_PLAN_AT（20:00）问她一次今晚几点睡、明早几点起，
程序收进 SLEEP_START_RANGE / SLEEP_END_RANGE、睡 SLEEP_MIN_HOURS～SLEEP_MAX_HOURS 小时，存进 data/sleep_plan.json（sleep.py 读）。
提示里给她一个“今天的平常”（每晚在 SLEEP_USUAL_START / SLEEP_USUAL_END 里随机抽，11/13 第 3 轮），她按今天的情况在上面早点晚点。
问不到（出错、回的不是时间、SLEEP_BY_MODEL 关着）就照老办法（SLEEP_HOURS + 浮动）。当晚定了不再改，重启也不变。
不回消息、睡前困意、道晚安、醒来补看都走 sleep.sleep_window() / sleep.asleep()，跟着她定的时间走，不用改。
管理员：/作息 看今晚和最近 7 晚；/作息 重定 马上再问一次（测试用）"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import budget, core, peak, sleep
from .core import cfg
from .prompts import SLEEP_PLAN_PROMPT

_WEEKDAY = "一二三四五六日"
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")
_asked: dict[str, int] = {}          # 哪晚问过几次（失败了隔一会儿再问，最多 3 次）
_next_try: dict[str, float] = {}
_usual: dict[str, tuple[int, int]] = {}   # 每晚抽的“今天的平常”（离中午多少分钟）；同一晚重问用同一个
MAX_TRIES = 3
RETRY_SECONDS = 600


def _hm(text) -> int | None:
    m = _TIME_RE.search(str(text or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24:
        h = 0
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None


def _life_text(now: float) -> tuple[str, str]:
    """给她看的：现在在哪、待了几天；最近说过的几件事"""
    try:
        d = core.self_mem.life_for_proactive("sleep_plan", n=4, now=now)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"作息：取近况出错，这次不带：{e}")
        return "", ""
    place = d.get("place") or {}
    if place.get("name") and place.get("status") == "现在":
        kind = f"（{place['kind']}）" if place.get("kind") else ""
        life = f"你现在在{place['name']}{kind}，已经待了 {int(place.get('days_here') or 1)} 天。"
    elif place.get("name"):
        life = f"你{place.get('said') or '前几天'}说过在{place['name']}，之后没再提。"
    else:
        life = ""
    items = list(d.get("fresh") or []) + list(d.get("told_elsewhere") or [])
    recent = "最近说过的：" + "；".join(f"{it.get('when') or '最近'}：{it.get('text', '')}" for it in items[:5]) + "。" if items else ""
    return life, recent


def _prev_nights(night: str) -> str:
    out = []
    for k in sorted(sleep.plans()):
        if k < night:
            p = sleep.plans()[k]
            out.append(f"{k[5:]} 晚上 {sleep._hhmm(p['start'])} 睡、{sleep._hhmm(p['end'])} 起" + (f"（{p.get('why')}）" if p.get("why") else ""))
    return ("前两晚：" + "；".join(out[-2:]) + "。") if out else ""


def usual(night: str) -> tuple[int, int]:
    """这晚的“平常”：在 SLEEP_USUAL_START / SLEEP_USUAL_END 里各随机抽一个，5 分钟一档；抽不出来用 23:30 / 7:30"""
    if night not in _usual:
        got = []
        for text, dflt in ((cfg.sleep_usual_start, sleep._off(23 * 60 + 30)), (cfg.sleep_usual_end, sleep._off(7 * 60 + 30))):
            try:
                r = sleep._range_off(text)
            except Exception:  # noqa: BLE001  配错了（不是“时:分-时:分”）
                r = None
            got.append(random.randrange(r[0], r[1] + 1, 5) if r and r[0] <= r[1] else dflt)
        _usual[night] = (got[0], got[1])
        for k in [k for k in _usual if k < night][:-7]:
            _usual.pop(k, None)
    return _usual[night]


async def ask(night: str, now_dt: datetime) -> dict | None:
    """问她一次；定好了存起来并返回计划，没问到返回 None"""
    life, recent = _life_text(now_dt.timestamp())
    prev = _prev_nights(night)
    when = f"{now_dt.month} 月 {now_dt.day} 日（星期{_WEEKDAY[now_dt.weekday()]}）{now_dt:%H:%M}"
    us, uw = usual(night)
    prompt = SLEEP_PLAN_PROMPT.format(now=when, usual_sleep=sleep._hhmm(us), usual_wake=sleep._hhmm(uw), life=life, recent="\n".join(x for x in (recent or "今天还没说过自己做了什么。", prev) if x))
    try:
        resp = await core.client.chat.completions.create(
            model=cfg.deepseek_model, messages=[{"role": "user", "content": prompt}], temperature=cfg.llm_temperature,
            max_tokens=150, response_format={"type": "json_object"}, extra_body={"thinking": {"type": "disabled"}})
        budget.track(resp, "sleep_plan")
        raw = resp.choices[0].message.content or ""
        got = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"作息：问她今晚几点睡出错，这晚先照老办法：{e}")
        return None
    s, w = _hm(got.get("sleep")), _hm(got.get("wake"))
    if s is None or w is None:
        logger.warning(f"作息：她回的时间读不出来，这晚照老办法：{raw[:80]}")
        return None
    a, b, notes = sleep.clamp_plan(s, w)
    plan = {"start": a, "end": b, "said": f"{s // 60:02d}:{s % 60:02d}-{w // 60:02d}:{w % 60:02d}",
            "why": str(got.get("why") or "")[:40], "clamped": notes,
            "usual": f"{sleep._hhmm(us)}-{sleep._hhmm(uw)}", "at": round(time.time())}
    sleep.save_plan(night, plan)
    logger.info(f"作息：今晚 {sleep._hhmm(a)} 睡、明早 {sleep._hhmm(b)} 起（平常 {plan['usual']}，她说 {plan['said']}：{plan['why']}）"
                + (f"；收过：{'；'.join(notes)}" if notes else ""))
    return plan


async def maybe_plan(now_dt: datetime | None = None) -> dict | None:
    """到了 SLEEP_PLAN_AT、这晚还没定、还没到最早的入睡时间：问她一次（失败了隔 10 分钟再问，最多 3 次）"""
    if not (cfg.sleep_enabled and cfg.sleep_by_model and cfg.deepseek_api_key):
        return None
    now_dt = (now_dt or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    night = sleep.night_key(now_dt)
    if night in sleep.plans() or _asked.get(night, 0) >= MAX_TRIES or time.monotonic() < _next_try.get(night, 0):
        return None
    at = _hm(cfg.sleep_plan_at)
    sr = sleep._range_off(cfg.sleep_start_range)
    noon = datetime.strptime(night, "%Y-%m-%d").replace(hour=12, tzinfo=peak.BEIJING)
    off = int((now_dt - noon).total_seconds() // 60)
    if at is None or off < sleep._off(at) or (sr and off >= sr[0]):
        return None                              # 还没到问的时候；或者已经过了最早的入睡时间（这晚照老办法）
    _asked[night] = _asked.get(night, 0) + 1
    plan = await ask(night, now_dt)
    if plan is None:
        _next_try[night] = time.monotonic() + RETRY_SECONDS
    return plan


def describe(now_dt: datetime | None = None) -> str:
    now_dt = (now_dt or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    night = sleep.night_key(now_dt)
    w = sleep.sleep_window(night)
    head = (f"自己定作息：{'开' if cfg.sleep_by_model else '关'}（每天 {cfg.sleep_plan_at} 问；睡 {cfg.sleep_start_range}、起 {cfg.sleep_end_range}、"
            f"{cfg.sleep_min_hours:g}～{cfg.sleep_max_hours:g} 小时）")
    p = sleep.plans().get(night) if cfg.sleep_by_model else None
    if p:
        now_line = f"今晚（{night}）：{sleep._hhmm(p['start'])} 睡、{sleep._hhmm(p['end'])} 起，平常 {p.get('usual', '-')}，她说 {p.get('said')}：{p.get('why')}" \
                   + (f"（收过：{'；'.join(p['clamped'])}）" if p.get("clamped") else "")
    elif w:
        now_line = f"今晚（{night}）：{w[0]:%H:%M} 睡、{w[1]:%H:%M} 起（老办法：SLEEP_HOURS {cfg.sleep_hours} 加浮动）"
    else:
        now_line = "今晚：没有休息时间"
    lines = [head, now_line]
    olds = [k for k in sorted(sleep.plans()) if k < night][-7:]
    if olds:
        lines.append("最近几晚：" + "；".join(f"{k[5:]} {sleep._hhmm(sleep.plans()[k]['start'])}～{sleep._hhmm(sleep.plans()[k]['end'])}"
                                              f"（{sleep.plans()[k].get('why', '')}）" for k in olds))
    return "\n".join(lines)


sleep_plan_cmd = on_command("作息", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@sleep_plan_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    words = arg.extract_plain_text().split()
    if words and words[0] in ("重定", "重新定", "再问"):
        if not cfg.sleep_by_model:
            await sleep_plan_cmd.finish("（SLEEP_BY_MODEL 没开，不问她）")
        now_dt = datetime.now(peak.BEIJING)
        night = sleep.night_key(now_dt)
        sleep.plans().pop(night, None)
        _asked.pop(night, None); _next_try.pop(night, None)
        plan = await ask(night, now_dt)
        await sleep_plan_cmd.finish("（重定了）\n" + describe() if plan else "（问了，没定成，看日志；这晚照老办法）")
    await sleep_plan_cmd.finish(describe())
