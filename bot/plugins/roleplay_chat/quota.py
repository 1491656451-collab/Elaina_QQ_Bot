"""限额（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：风控（冷却、限速、每小时条数、按人按群的钱）、
打字节奏、额度快用完 / 到点时的收尾、额度用完时告别、“今天累了”、/花费
只改了一处写法：调 asleep()、familiarity_of() 写成 sleep.asleep()、core.familiarity_of()，测试替换它们才生效"""
import asyncio
import contextlib
import random
import time
from collections import defaultdict, deque
from datetime import datetime

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import core, peak, sleep
from .core import _hands, _target_of, cfg, ltm, mark_sent, spend, switch_pause
from .history import get_history, save_history, session_key
from .prompts import WINDDOWN_SLEEP_HINT, WINDDOWN_TIRED_HINT
from .sleep import _goodnight, night_key, sleep_window


_last_trigger: dict[int, float] = {}


_global_window: deque = deque()


_hour_window: deque = deque()


_group_hour: dict[int, deque] = defaultdict(deque)


_private_hour: dict[int, deque] = defaultdict(deque)   # QQ -> 这一小时私聊发给这个人的条数（各人分开算）


_farewell_at: dict[str, float] = {}      # 限额范围 -> 上次说“我要上路了”的时间


_active_chats: dict[str, dict] = {}      # 会话（group_群号 / private_QQ）-> 她最近一次在这里说话的时间和对象


def _scope(group_id: int | None, user_id: int | None) -> "tuple[deque, int] | tuple[None, None]":
    """这条回复算在哪个范围的额度里：群聊按群，私聊按人；都没有（比如空间评论）就只算全局"""
    if group_id:
        return _group_hour[group_id], cfg.group_rate_per_hour
    if user_id:
        return _private_hour[user_id], cfg.private_rate_per_hour
    return None, None


_NO_LIMIT = 10 ** 6


def _rounds(money: float) -> int:
    """剩下的钱大约还够回几轮"""
    if money == float("inf"):
        return _NO_LIMIT
    return int(money / max(cfg.budget_round_estimate, 1e-6)) if money > 0 else 0


def quota_left(group_id: int | None, user_id: int | None = None) -> int:
    """还能回几轮：今天剩下的钱（和这个群 / 这个人私聊的份额）大约够几轮，再和每小时条数限额（开了的话）取小的"""
    if sleep.asleep():
        return 0                                          # 休息时间
    now = time.monotonic()
    left = _rounds(spend.chat_left())
    if cfg.global_rate_per_hour > 0:
        left = min(left, cfg.global_rate_per_hour - sum(1 for t in _hour_window if now - t <= 3600))
    q, limit = _scope(group_id, user_id)
    if q is not None and limit > 0:
        left = min(left, limit - sum(1 for t in q if now - t <= 3600))
    if group_id:
        left = min(left, _rounds(spend.group_left(group_id)))
    elif user_id:
        left = min(left, _rounds(spend.user_left(user_id)))
    return left


# 额度用完的那一轮，话还没说完（她在反问对方、或者对方还有话没回）：先不告别，给这个人最多再回 2 次，
# 把话收个尾再走；10 分钟内对方没接话就算了
WRAPUP_EXTRA = 2


WRAPUP_MINUTES = 10


_wrapup: dict[str, dict] = {}       # 会话 -> {"user": 在跟谁收尾, "left": 还能回几次, "at": 开始收尾的时间}


def in_wrapup(target: str | None, user_id: int) -> bool:
    w = _wrapup.get(target) if target else None
    if not w:
        return False
    if time.time() - w["at"] > WRAPUP_MINUTES * 60:
        _wrapup.pop(target, None)
        return False
    return w["user"] == user_id and w["left"] > 0


def rate_limited(user_id: int, group_id: int | None = None, private: bool = False, target: str | None = None) -> str | None:
    """private=True：私聊，按这个人单独算每小时额度。
    target：这个会话正在“收尾”（额度用完、话还没说完）时，每小时额度用完了也再放行这个人几次"""
    now = time.monotonic()
    if now - _last_trigger.get(user_id, -1e9) < cfg.user_cooldown:
        return "cooldown"
    while _global_window and now - _global_window[0] > 60:
        _global_window.popleft()
    if len(_global_window) >= cfg.global_rate_per_minute:
        return "global"
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.popleft()
    over = None
    if sleep.asleep():
        over = "sleep"                                # 休息时间
        _sleep_wrapup(target, user_id)
    elif spend.chat_left() <= 0:
        over = "budget"                               # 今天的钱花完了
    elif group_id and spend.group_left(group_id) <= 0:
        over = "budget_group"                         # 这个群今天的份额用完了
    elif spend.user_left(user_id) <= 0:
        over = "budget_user"                          # 这个人今天的份额用完了（群聊私聊合计）
    elif cfg.global_rate_per_hour > 0 and len(_hour_window) >= cfg.global_rate_per_hour:
        over = "hourly"
    gq, limit = _scope(group_id, user_id if private else None)
    if gq is not None:
        while gq and now - gq[0] > 3600:
            gq.popleft()
        if not over and limit > 0 and len(gq) >= limit:
            over = "group_hourly" if group_id else "private_hourly"
    if over:
        if not in_wrapup(target, user_id):
            return over
        _wrapup[target]["left"] -= 1          # 收尾：超出额度也再回这一次
        logger.info(f"额度已经用完（{over}），给 {target} 收个尾（还能回 {_wrapup[target]['left']} 次）")
    _last_trigger[user_id] = now
    _global_window.append(now)
    _hour_window.append(now)
    if gq is not None:
        gq.append(now)
    return None


def _crossed_mid_chat(target: str | None) -> bool:
    """钱用完时，这个群 / 私聊是不是还在聊着、却还没告别过：她最近 FAREWELL_ACTIVE_MINUTES 分钟里在这里说过话，
    一小时内也没在这里告别过（9/30：记忆整理在两轮之间把合计推过线时，回完那轮的告别检查赶不上）"""
    a = _active_chats.get(target) if target else None
    now = time.monotonic()
    return (bool(a) and cfg.farewell_on_limit and now - a["at"] <= cfg.farewell_active_minutes * 60
            and now - _farewell_at.get(target, -1e9) > 3600 and now - _farewell_at.get("global", -1e9) > 3600)


def _refund(stamp: float, group_id: int | None, user_id: int | None = None) -> None:
    """占了额度最后却没说话（模型出错、她不想接、出戏句子删光了、没发出去）：把这一条退回去"""
    for dq in (_hour_window, _global_window, _scope(group_id, user_id)[0]):
        if dq is None:
            continue
        try:
            dq.remove(stamp)
        except ValueError:
            pass


def _has_quota(gid: int | None, user_id: int | None = None) -> bool:
    """还有没有额度（只看，不占）"""
    now = time.monotonic()
    if sum(1 for t in _global_window if now - t <= 60) >= cfg.global_rate_per_minute:
        return False
    return quota_left(gid, user_id) > 0


def _count_sent(group_id: int | None, user_id: int | None = None) -> None:
    """多发了一条（分条的后几条、表情、“在忙”、告别）：算进每小时的额度（全局 + 这个群 / 这个人的私聊）"""
    now = time.monotonic()
    _hour_window.append(now)
    q = _scope(group_id, user_id)[0]
    if q is not None:
        q.append(now)


def typing_delay(reply: str) -> float:
    """第一条：先看消息、想一想，再按字数打字"""
    d = random.uniform(cfg.reply_delay_min, cfg.reply_delay_max) + len(reply) * cfg.reply_delay_per_char * random.uniform(0.8, 1.3)
    return min(d, cfg.reply_delay_cap)


def bubble_gap(bubble: str) -> float:
    """后面几条：停顿一下，再按这条的字数打字（打字速度每次略有快慢）"""
    d = random.uniform(cfg.bubble_gap_min, cfg.bubble_gap_max) + len(bubble) * cfg.reply_delay_per_char * random.uniform(0.8, 1.3)
    return min(d, cfg.bubble_gap_cap)


cost_cmd = on_command("花费", aliases={"花销"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@cost_cmd.handle()
async def _():
    """/花费：今天（从凌晨 BUDGET_RESET_HOUR 点算起）花了多少、花在哪、谁花得最多，本月合计"""
    names = {}
    d = spend.today()
    for q in list(d["users"])[:50]:
        with contextlib.suppress(Exception):
            names[int(q)] = ltm.get_user(int(q)).get("name") or q
    text = spend.report(names)
    if spend.enabled and spend.chat_left() <= 0:
        text += f"\n今天的钱已经花完了，{spend.next_reset():%H:%M} 以后恢复"
    await cost_cmd.finish(text)


TIRED_LINES = peak.TIRED_TIERS          # 台词按关系分档，在 peak.py 里


_tired_sent: dict[int, str] = {}          # QQ -> 哪一天（按花费的“一天”）已经回过“今天累了”


async def say_tired(bot: Bot, event: MessageEvent) -> None:
    """今天的钱花完了之后才来私聊的人：回一句固定的“今天累了”（每人每天一次；已经告别过的不再说），不调用模型"""
    if not cfg.budget_tired_reply:
        return
    target = _target_of(event)
    day = spend.day_key()
    if _tired_sent.get(event.user_id) == day or time.monotonic() - _farewell_at.get(target, -1e9) <= 3600:
        return
    _tired_sent[event.user_id] = day
    line = peak.tired_line(core.familiarity_of(event.user_id))
    async with _hands:
        await switch_pause(target)
        await asyncio.sleep(typing_delay(line))
        try:
            await bot.send(event, line)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"“今天累了”没发出去：{e}")
            return
        mark_sent(target)
    _farewell_at[target] = time.monotonic()
    key = session_key(event)
    get_history(key).append({"role": "assistant", "content": line, "ts": time.time()})
    save_history(key)
    logger.info(f"今天的额度用完了，对 {event.user_id} 说了句“今天累了”")


async def say_farewells(bot: Bot, event: MessageEvent | None, target: str | None, already_bye: bool = False,
                        skip=()) -> list[str]:
    """（已经拿着 _hands）限额用完或到了休息时间时告别。返回告别过的会话。
    - 今天的钱花完了 / 到了休息时间：对最近在聊的群和私聊都说一声（休息时说的是“要睡了”）；
      只是这个群 / 这个人的份额用完了：只对这里说。
    - already_bye：target 这个会话刚才已经互相道别过了，不再补一句（但照样记下“告别过了”，之后不回）
    - target 为 None：不是因为某条消息触发的（到点了，后台道晚安），只对最近在聊的说；skip 里的会话跳过"""
    sleeping = sleep.asleep()
    night = night_key()
    if quota_left(None) <= 0:
        scope = "global"
        now = time.monotonic()
        targets = [t for t, a in _active_chats.items()
                   if now - a["at"] <= cfg.farewell_active_minutes * 60 and t != target and t not in skip]
        targets = ([target] if target else []) + sorted(targets, key=lambda t: -_active_chats[t]["at"])
    else:
        scope, targets = target, [target]
    if not sleeping:
        if time.monotonic() - _farewell_at.get(scope, -1e9) <= 3600:
            return []
        _farewell_at[scope] = time.monotonic()
    done, used, sent_any = [], [], False
    for t in targets:
        if sleeping:
            if _goodnight.get(t) == night:
                continue                               # 今晚已经道过晚安了
            _goodnight[t] = night
        elif scope == "global" and time.monotonic() - _farewell_at.get(t, -1e9) <= 3600:
            continue                                   # 这个群刚因为本群限额告别过
        _farewell_at[t] = time.monotonic()
        private = t.startswith("private_")
        if t == target and already_bye:
            logger.info(f"{'要休息了' if sleeping else '额度用完'}，但刚才已经道别过了，不再补一句：{t}")
            continue
        fam = core.familiarity_of(_active_chats[t]["user_id"]) if private and _active_chats.get(t) else None
        line = (peak.sleep_line if sleeping else peak.farewell_line)(private=private, avoid=used, fam=fam)
        used.append(line)
        if sent_any:
            await switch_pause(t)
        await asyncio.sleep(bubble_gap(line) + (0 if sent_any else 1))
        try:
            if t == target and event is not None:
                await bot.send(event, line)
            elif private:
                await bot.send_private_msg(user_id=_active_chats[t]["user_id"], message=line)
            else:
                await bot.send_group_msg(group_id=_active_chats[t]["group_id"], message=line)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"告别没发出去（{t}）：{e}")
            continue
        sent_any = True
        mark_sent(t)
        _count_sent(None if private else _active_chats[t]["group_id"], _active_chats[t]["user_id"] if private else None)    # 告别也算一条
        # 记进这个会话的短期记忆（不去拿会话锁：这时拿着 _hands，别人可能拿着会话锁在等 _hands）
        hist_key = t if private or cfg.group_shared_memory else f"{t}_{_active_chats[t]['user_id']}"
        get_history(hist_key).append({"role": "assistant", "content": line, "ts": time.time()})
        save_history(hist_key)
        done.append(t)
    if done:
        why = "到了休息时间" if sleeping else ("今天的钱花完了" if scope == "global" else
                                          ("本群的份额用完" if scope.startswith("group_") else "这个人的份额用完"))
        logger.info(f"{why}，已告别：{'、'.join(done)}")
    return done


def winddown_hint(group_id: int | None, user_id: int, now: datetime | None = None) -> str:
    """快到睡觉时间、或者今天的钱（这个人 / 这个群的份额）快用完时，提示她把话题慢慢往收尾靠"""
    now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    if cfg.sleep_enabled and cfg.sleep_winddown_minutes > 0 and not sleep.asleep(now):
        w = sleep_window(night_key(now))
        if w and 0 < (w[0] - now).total_seconds() <= cfg.sleep_winddown_minutes * 60:
            return WINDDOWN_SLEEP_HINT.format(at=f"{w[0].hour} 点 {w[0].minute} 分" if w[0].minute else f"{w[0].hour} 点")
    if cfg.budget_winddown_rounds > 0:
        left = min(quota_left(group_id, None if group_id else user_id), _rounds(spend.user_left(user_id)))
        if 0 < left <= cfg.budget_winddown_rounds:
            return WINDDOWN_TIRED_HINT
    return ""


def _sleep_wrapup(target: str | None, user_id: int) -> None:
    """刚到休息时间，正在跟她聊的人又说话了：给他收个尾（最多再回 2 次），说完再道晚安"""
    if not target or target in _wrapup or _goodnight.get(target) == night_key():
        return
    a = _active_chats.get(target)
    if a and a.get("user_id") == user_id and time.monotonic() - a["at"] <= cfg.farewell_active_minutes * 60:
        _wrapup[target] = {"user": user_id, "left": WRAPUP_EXTRA, "at": time.time()}
        logger.info(f"到休息时间了，{user_id} 还在跟她聊：先把话说完再道晚安（{target}）")
