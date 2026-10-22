"""群里主动说话（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：群里旁听（没 @ 她的群消息）、
聊得正热时插一句、很久没人说话时冒个泡、/插话、/冒泡。
只改了一处写法：调 _in_hours() 写成 core._in_hours()，测试替换它才生效。
10/23：调模型、看她想不想说、分条发到群里这几段挪到 proactive_common.py（插话、冒泡、搭话、写信共用）；
手动 /插话、/冒泡 不再算进当天的次数和间隔（只记日志）"""
import asyncio
import json
import random
import re
import time
from datetime import datetime

from nonebot import get_bots, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule, to_me

from . import core, peak, proactive_common
from .core import BOT_DIR, cap_text, cfg, over_cap
from .prompts import BUBBLE_PROMPT, INTERJECT_PROMPT, system_prompt
from .history import _locks, _passive, _recent_chat, api_messages, get_history, save_history
from .turn_hints import _WEEKDAY, clock_text, human_gap, now_hint, short_hint
from .textin import clean_body, clip_input, group_body, is_bot_like, message_to_text, quote_note, sender_label, speaker_head, watch_block
from .quota import _global_window, _group_hour, _has_quota, _hour_window, quota_left
from .online import quiet_left
from .commands import is_slash_message
from .addressed import _away, _is_group_not_to_me, _last_bot_msg, _last_smart, allowed, away_left


passive = on_message(rule=Rule(_is_group_not_to_me) & allowed, priority=99, block=False)


@passive.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    text = message_to_text(event.get_message())
    # 群友转的卡片（B 站视频、链接、名片）也进旁听，只显示标题，不会因为它开口；Markdown、按钮这些机器人消息照旧不要
    if not text or is_slash_message(event, bot.config.command_start) or is_bot_like(event, allow_cards=True):
        return
    body = quote_note(event) + (message_to_text(group_body(event)) or "（只@了一下）")
    line = speaker_head(event) + clean_body(clip_input(body))
    if cfg.passive_buffer > 0:
        _passive[event.group_id].append((event.user_id, sender_label(event), line, time.time()))
    _recent_chat[event.group_id].append((time.monotonic(), event.user_id, sender_label(event), line))
    if cfg.interject_enabled:
        await maybe_interject(bot, event.group_id, text)


INTERJECT_FILE = BOT_DIR / "data" / "interject.json"


_interject_busy: set[int] = set()


_interject_skip_until: dict[int, float] = {}


_NO_INTERJECT_RE = re.compile(r"[\[【（(［]\s*不插\s*[\]】）)］]")


def _interject_state() -> dict:
    try:
        return json.loads(INTERJECT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_interject_state(st: dict) -> None:
    try:
        INTERJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
        INTERJECT_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"记录插话次数失败：{e}")


def interject_blocker(gid: int, text: str = "") -> str | None:
    """现在不能在这个群插话的原因；可以就返回 None（不含随机那一步）"""
    if away_left(f"group_{gid}") > 0:
        return "她说过要走了"
    if core.in_peak():
        return "高峰时段"
    if not core._in_hours(cfg.interject_hours):
        return "不在插话时段"
    if gid in _interject_busy:
        return "正在看"
    now_m, now_t = time.monotonic(), time.time()
    if now_m < _interject_skip_until.get(gid, 0):
        return "刚看过，没什么想说的"
    st = _interject_state().get(str(gid), {})
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    if st.get("day") == today and over_cap(st.get("count", 0), cfg.interject_daily_max):
        return "今天插够了"
    if now_t - float(st.get("last", 0)) < cfg.interject_min_gap_hours * 3600:
        return "离上次插话太近"
    last = _last_bot_msg.get(gid)
    if last and now_m - last[0] < 600:
        return "她刚在这个群说过话"
    if quota_left(gid) < 5:
        return "这一小时额度不多了"
    recent = [x for x in _recent_chat.get(gid, []) if now_m - x[0] <= cfg.interject_hot_minutes * 60]
    if len(recent) < cfg.interject_hot_messages or len({x[1] for x in recent}) < cfg.interject_hot_people:
        return "群里没在热聊"
    return None


def _take_group_quota(gid: int) -> bool:
    """插话也算进每分钟的限额、今天的钱和这个群的份额（开了每小时条数限额的话也算；不算个人冷却）"""
    now = time.monotonic()
    while _global_window and now - _global_window[0] > 60:
        _global_window.popleft()
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.popleft()
    gq = _group_hour[gid]
    while gq and now - gq[0] > 3600:
        gq.popleft()
    if (len(_global_window) >= cfg.global_rate_per_minute
            or 0 < cfg.global_rate_per_hour <= len(_hour_window)
            or 0 < cfg.group_rate_per_hour <= len(gq)
            or quota_left(gid) <= 0):
        return False
    _global_window.append(now)
    _hour_window.append(now)
    gq.append(now)
    return True


async def maybe_interject(bot: Bot, gid: int, text: str, force: bool = False) -> str | None:
    """看看要不要在这个群插一句；插了就返回说的话"""
    if quiet_left() > 0:
        return None
    if not force:
        if interject_blocker(gid, text):
            return None
        topical = any(w in text for w in cfg.interject_topics)
        if random.random() >= (cfg.interject_prob if topical else cfg.interject_offtopic_prob):
            return None
    if gid in _interject_busy or not cfg.deepseek_api_key:
        return None
    _interject_busy.add(gid)
    try:
        return await _interject(bot, gid, force)
    finally:
        _interject_busy.discard(gid)


async def _interject(bot: Bot, gid: int, force: bool) -> str | None:
    key = f"group_{gid}"
    if not force and not _has_quota(gid):          # 先看额度，不够就不调模型了
        logger.info(f"插话：群{gid} 额度不够，算了")
        return None
    async with _locks[key]:
        history = get_history(key)
        chat_lines = list(_recent_chat.get(gid, []))[-8:]
        if not chat_lines:
            return None
        now_m, now_t = time.monotonic(), time.time()
        watch = watch_block([(now_t - (now_m - m), ln) for m, _, _, ln in chat_lines])
        messages = api_messages(
            [{"role": "system", "content": system_prompt()}]
            + history[-6:]
            + [{"role": "user", "content": watch},
               {"role": "system", "content": now_hint() + "\n\n" + INTERJECT_PROMPT + short_hint()}]
        )
        reply = await proactive_common.ask(bot, messages, "interject", "插话：调用失败", group=gid)
        if reply is None:
            return None
        if proactive_common.declined(reply, _NO_INTERJECT_RE):
            _interject_skip_until[gid] = time.monotonic() + cfg.interject_retry_minutes * 60
            logger.info(f"插话：群{gid} 她看了看，没什么想说的")
            return None
        if not force and not _take_group_quota(gid):
            logger.info(f"插话：群{gid} 额度不够，算了")
            return None
        # 旁听记录并进短期记忆，再记她这句
        buf = list(_passive.pop(gid, []))
        if buf:
            history.append({"role": "user", "content": watch_block([(ts, ln) for _, _, ln, ts in buf]),
                            "speakers": {n: uid for uid, n, _, _ in buf}, "ts": time.time()})
        history.append({"role": "assistant", "content": reply, "ts": time.time()})
        save_history(key)
        # 插话不交给长期记忆整理：不因为这一句给谁加减好感
    if not await proactive_common.send_to_group(bot, gid, reply, "插话"):
        return None
    now = time.monotonic()
    _last_bot_msg[gid] = (now, 0, reply)     # 有人接她的话，走“她刚说完话”的判断
    _last_smart[gid] = now
    if force:                                # 10/23：手动 /插话 不算进今天的次数，也不推后下一次自动插话
        logger.info(f"插话：群{gid} 手动 /插话，她说了一句（不算进今天的次数和间隔）：{reply[:30]}")
        return reply
    st = _interject_state()
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    g = st.get(str(gid), {})
    count = g.get("count", 0) + 1 if g.get("day") == today else 1
    st[str(gid)] = {"day": today, "count": count, "last": time.time()}
    _save_interject_state(st)
    logger.info(f"插话：群{gid} 主动说了一句（今天第 {count} 次）：{reply[:30]}")
    return reply


interject_cmd = on_command("插话", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@interject_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """/插话：在这个群里马上让她看一眼要不要插话（测试用，不看次数和热度）；/插话 状态：看看现在能不能插"""
    if not isinstance(event, GroupMessageEvent):
        await interject_cmd.finish("（这个要在群里用）")
    gid = event.group_id
    if arg.extract_plain_text().strip() == "状态":
        st = _interject_state().get(str(gid), {})
        today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
        n = st.get("count", 0) if st.get("day") == today else 0
        why = interject_blocker(gid) or "可以，群里再有人说话就有机会插"
        b = st.get("bubble_count", 0) if st.get("bubble_day") == today else 0
        bwhy = bubble_blocker(gid) or "可以，下次检查时有机会冒泡"
        await interject_cmd.finish(f"（今天已插话 {n}/{cap_text(cfg.interject_daily_max)} 次｜现在：{why}）\n"
                                   f"（今天已冒泡 {b}/{cap_text(cfg.bubble_daily_max)} 次｜现在：{bwhy}）")
    said = await maybe_interject(bot, gid, "", force=True)
    if not said:
        await interject_cmd.finish("（她看了看最近的聊天，没什么想插的）")


_NO_BUBBLE_RE = re.compile(r"[\[【（(［]\s*不说\s*[\]】）)］]")


def _group_last_activity(gid: int) -> tuple[float | None, str | None]:
    """这个群最后一次有人（或她）说话的时间（time.time()），以及最后说话的是 user 还是 assistant"""
    h = get_history(f"group_{gid}")
    last_ts, last_role = None, None
    for e in reversed(h):
        if e.get("ts"):
            last_ts, last_role = float(e["ts"]), e["role"]
            break
    recent = _recent_chat.get(gid)
    if recent:                                        # 旁听里还有没进记忆的消息：以最新的为准
        ts = time.time() - (time.monotonic() - recent[-1][0])
        if last_ts is None or ts > last_ts:
            last_ts, last_role = ts, "user"
    return last_ts, last_role


def bubble_blocker(gid: int) -> str | None:
    """现在不能在这个群冒泡的原因；可以就返回 None（不含随机那一步）"""
    if not cfg.bubble_enabled:
        return "冒泡没开"
    if away_left(f"group_{gid}") > 0:
        return "她说过要走了"
    if core.in_peak():
        return "高峰时段"
    if not core._in_hours(cfg.bubble_hours):
        return "不在冒泡时段"
    if gid in _interject_busy:
        return "正在想"
    st = _interject_state().get(str(gid), {})
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    if st.get("bubble_day") == today and over_cap(st.get("bubble_count", 0), cfg.bubble_daily_max):
        return "今天冒过泡了"
    if time.time() - float(st.get("bubble_last", 0)) < cfg.bubble_min_gap_hours * 3600:
        return "离上次冒泡太近"
    all_st = _interject_state()
    today_total = sum(g.get("bubble_count", 0) for g in all_st.values() if isinstance(g, dict) and g.get("bubble_day") == today)
    if over_cap(today_total, cfg.bubble_global_daily_max):
        return "今天所有群加起来冒够了"
    last_ts, last_role = _group_last_activity(gid)
    if last_ts is None:
        return "还不知道这个群最近有没有人说话"
    if last_role == "assistant":
        return "最后一句是她说的，还没人接"
    if time.time() - last_ts < cfg.bubble_quiet_hours * 3600:
        return "群里最近有人说话"
    if quota_left(gid) < 5:
        return "这一小时额度不多了"
    return None


async def bubble(bot: Bot, gid: int, force: bool = False) -> str | None:
    """在这个群冒个泡；说了就返回说的话"""
    if gid in _interject_busy or not cfg.deepseek_api_key:
        return None
    if not force and not _has_quota(gid):          # 先看额度，不够就不调模型了
        return None
    _interject_busy.add(gid)
    try:
        key = f"group_{gid}"
        async with _locks[key]:
            history = get_history(key)
            last_ts, _ = _group_last_activity(gid)
            gap = human_gap(time.time() - last_ts) if last_ts else "好一阵子"
            now = datetime.now(peak.BEIJING)
            prompt = BUBBLE_PROMPT.format(now=f"{now.month} 月 {now.day} 日（星期{_WEEKDAY[now.weekday()]}）{clock_text(now)}", gap=gap)
            messages = api_messages(
                [{"role": "system", "content": system_prompt()}]
                + history[-6:]
                + [{"role": "system", "content": prompt + short_hint()}]
            )
            reply = await proactive_common.ask(bot, messages, "bubble", "冒泡：调用失败", group=gid)
            if reply is None:
                return None
            if proactive_common.declined(reply, _NO_BUBBLE_RE):
                logger.info(f"冒泡：群{gid} 她不想说话")
                return None
            if not force and not _take_group_quota(gid):
                return None
            history.append({"role": "assistant", "content": reply, "ts": time.time()})
            save_history(key)
            _away.pop(key, None)                      # 她已经自己开口了，下次聊天不用再说“刚回来”
        if not await proactive_common.send_to_group(bot, gid, reply, "冒泡"):
            return None
        now_m = time.monotonic()
        _last_bot_msg[gid] = (now_m, 0, reply)
        _last_smart[gid] = now_m
        if force:                            # 10/23：手动 /冒泡 不算进今天的次数，也不推后下一次自动冒泡
            logger.info(f"冒泡：群{gid} 手动 /冒泡，她说了一句（不算进今天的次数和间隔）：{reply[:30]}")
            return reply
        st = _interject_state()
        today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
        g = st.get(str(gid), {})
        g["bubble_count"] = g.get("bubble_count", 0) + 1 if g.get("bubble_day") == today else 1
        g["bubble_day"], g["bubble_last"] = today, time.time()
        st[str(gid)] = g
        _save_interject_state(st)
        logger.info(f"冒泡：群{gid} 很久没人说话，她说了一句：{reply[:30]}")
        return reply
    finally:
        _interject_busy.discard(gid)


async def check_bubbles() -> int:
    """看看白名单里哪些群该冒泡了；返回冒了几个"""
    if quiet_left() > 0:
        return 0
    if not (cfg.bubble_enabled and cfg.enable_group and cfg.deepseek_api_key and cfg.group_whitelist):
        return 0
    bots = list(get_bots().values())
    if not bots:
        return 0
    n = 0
    for gid in list(cfg.group_whitelist):
        if bubble_blocker(gid) is None and random.random() < cfg.bubble_prob:
            if await bubble(bots[0], gid):
                n += 1
                await asyncio.sleep(random.uniform(60, 300))   # 好几个群一起冒泡太假，隔开一点
    return n


async def _bubble_loop() -> None:
    await asyncio.sleep(300)                                    # 刚启动先别急
    while True:
        try:
            await check_bubbles()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查冒泡出错：{e}")
        await asyncio.sleep(cfg.bubble_check_minutes * 60)


bubble_cmd = on_command("冒泡", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@bubble_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    """/冒泡：在这个群里马上让她随口说一句（测试用，不看次数和安静多久）"""
    if not isinstance(event, GroupMessageEvent):
        await bubble_cmd.finish("（这个要在群里用）")
    said = await bubble(bot, event.group_id, force=True)
    if not said:
        await bubble_cmd.finish("（她这会儿不想说话）")
