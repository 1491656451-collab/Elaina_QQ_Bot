"""私聊主动说话（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：主动写信、冷场后搭话、/写信、/搭话。
只改了一处写法：调 _in_hours() 写成 core._in_hours()，测试替换它才生效"""
import asyncio
import random
import re
import time
from datetime import datetime

from nonebot import get_bots, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import budget, core, peak
from .oocheck import ooc_words
from .core import _hands, alert_admins, cap_text, cfg, classify_error, client, ltm, mark_sent, over_cap, switch_pause, tier_value
from .prompts import FAMILIARITY_HINT, LETTER_PROMPT, NUDGE_PROMPT, system_prompt
from .history import _inbox, _locks, api_messages, get_history, save_history
from .turn_hints import human_gap, now_hint, short_hint
from .textout import CHAT_STOP, clean_reply, split_bubbles
from .quota import _count_sent, _global_window, _has_quota, _hour_window, _private_hour, bubble_gap, quota_left, typing_delay
from .memory_chat import _parse_target
from .online import quiet_left
from .addressed import away_left


_letters_sent: dict[str, int] = {}      # 日期 -> 今天已寄出几封


def _letter_window_now() -> bool:
    now = datetime.now(peak.BEIJING)
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in peak.parse_ranges(cfg.letter_hours)) and not core.in_peak()


def letter_daily_prob(qq: int) -> float:
    """这个人每天收到信的概率（按关系取 LETTER_DAILY_PROB；0 = 这个档位不写信）"""
    return float(tier_value(cfg.letter_daily_prob, core.familiarity_of(qq), 0.0))


def _letter_prob_per_check(daily: float) -> float:
    """把“每天的概率”折算成每次检查的概率（只在可寄信的时段里检查）"""
    window = sum(b - a for a, b in peak.parse_ranges(cfg.letter_hours)) * 60 or 86400
    checks = max(1.0, window / max(60, cfg.letter_check_interval))
    return 1 - (1 - min(max(daily, 0.0), 0.999)) ** (1 / checks)


def letter_blocker(qq: int, friends: set[int], force: bool = False) -> str | None:
    """不能给这个人写信的原因；可以写就返回 None。force=True 时只检查硬性条件（好友、私聊开关）"""
    if not cfg.enable_private:
        return "私聊功能关着"
    if qq not in friends:
        return "不是机器人的 QQ 好友"
    if cfg.private_whitelist and qq not in cfg.private_whitelist:
        return "不在私聊白名单里"
    if force:
        return None
    if letter_daily_prob(qq) <= 0:
        return "还没到熟人"
    prof = ltm.get_user(qq)
    seen, letter, now = ltm.last_seen(qq), prof.get("last_letter"), time.time()
    if not seen:
        return "还没聊过天"
    if now - seen < cfg.letter_min_silence_hours * 3600:
        return "最近刚聊过"
    if letter and now - letter < cfg.letter_min_days * 86400:
        return "刚写过信"
    if letter and letter > seen:
        return "上一封信还没回"
    return None


async def write_letter(bot: Bot, qq: int) -> str | None:
    """写一封信并私聊发出去；成功返回信的内容"""
    key = f"private_{qq}"
    prof = ltm.get_user(qq)
    name = prof.get("name") or str(qq)
    seen = ltm.last_seen(qq)
    gap = f"你们已经 {human_gap(time.time() - seen)}没说话了。" if seen else ""
    async with _locks[key]:
        history = get_history(key)
        long_memo = ltm.context_for(qq, name, None)
        messages = api_messages(
            [{"role": "system", "content": system_prompt()}]
            + history[-6:]
            + [{"role": "system", "content": "\n\n".join(x for x in (long_memo, now_hint(), LETTER_PROMPT.format(name=name, gap=gap)) if x)}]
        )
        try:
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model,
                messages=messages,
                temperature=cfg.llm_temperature,
                max_tokens=cfg.letter_max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "letter", user=qq)
            choice = resp.choices[0]
            letter = clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            logger.error(f"写信失败（{kind or '其他错误'}）：{e}")
            if kind:
                await alert_admins(bot, kind)
            return None
        if not letter or ooc_words(letter, ""):
            logger.info(f"信写得不好（出戏或为空），这次不寄：{letter[:40]}")
            return None
        try:
            async with _hands:
                await switch_pause(key)
                await asyncio.sleep(typing_delay(letter[:40]))
                await bot.send_private_msg(user_id=qq, message=letter)
                mark_sent(key)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"寄信给 {qq} 失败：{e}")
            return None
        history.append({"role": "assistant", "content": letter, "ts": time.time()})
        save_history(key)
    ltm.mark_letter(qq)
    _private_hour[qq].append(time.monotonic())
    _hour_window.append(time.monotonic())
    _global_window.append(time.monotonic())
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    _letters_sent[today] = _letters_sent.get(today, 0) + 1
    logger.info(f"给 {name}（{qq}）寄了一封信")
    return letter


async def check_letters() -> int:
    """看看今天要不要给谁写信；返回寄出了几封"""
    if quiet_left() > 0:
        return 0
    if not (cfg.letter_enabled and cfg.deepseek_api_key and _letter_window_now()):
        return 0
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    if over_cap(_letters_sent.get(today, 0), cfg.letter_max_per_day) or quota_left(None) <= 2:
        return 0
    bots = list(get_bots().values())
    if not bots:
        return 0
    bot = bots[0]
    try:
        friends = {int(f["user_id"]) for f in await bot.get_friend_list()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"获取好友列表失败，这次不写信：{e}")
        return 0
    candidates = [qq for qq in set(ltm.user_ids()) | set(cfg.close_friends) if letter_blocker(qq, friends) is None]
    random.shuffle(candidates)
    candidates.sort(key=lambda q: core.familiarity_of(q) != "close")    # 很熟的排前面（同档位内保持随机），每天的名额别被熟人挤掉
    sent = 0
    for qq in candidates:
        if over_cap(_letters_sent.get(today, 0), cfg.letter_max_per_day):
            break
        p = _letter_prob_per_check(letter_daily_prob(qq))
        if random.random() < p and await write_letter(bot, qq):
            sent += 1
            await asyncio.sleep(random.uniform(30, 120))    # 连着寄几封时隔开一点
    return sent


async def _letter_loop() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await check_letters()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查写信出错：{e}")
        await asyncio.sleep(cfg.letter_check_interval)


_NO_NUDGE_RE = re.compile(r"[\[【（(［]\s*不说\s*[\]】）)］]")


_BYE_RE = re.compile(r"(晚安|拜拜|再见|先走了|先下了|下线了|睡了|去睡|去忙|忙去了|回聊|改天聊|明天聊|88|bye|good ?night)", re.I)


_nudge_due: dict[int, dict] = {}          # QQ -> {"at": 什么时候搭话, "armed": 她那句回复的时间}


_nudge_count: dict[str, dict] = {}        # 日期 -> {QQ: 今天搭过几次, "all": 合计}


def arm_nudge(qq: int, user_text: str = "") -> bool:
    """她刚回完这个人：抽签决定这次冷场要不要搭话，要的话定好时间"""
    _nudge_due.pop(qq, None)
    if not (cfg.nudge_enabled and cfg.enable_private):
        return False
    if _BYE_RE.search(user_text or ""):        # 对方在道别：不搭
        return False
    if random.random() >= tier_value(cfg.nudge_prob, core.familiarity_of(qq), 0.0):
        return False
    lo, hi = sorted((cfg.nudge_delay_min, cfg.nudge_delay_max))
    _nudge_due[qq] = {"at": time.time() + random.uniform(lo, hi) * 60, "armed": time.time()}
    return True


def nudge_blocker(qq: int) -> str | None:
    """现在不能给这个人搭话的原因；可以就返回 None"""
    if quiet_left() > 0:
        return "刚重新上线"
    if away_left(f"private_{qq}") > 0:
        return "她说过要走了"
    if core.in_peak():
        return "高峰时段"
    if not core._in_hours(cfg.nudge_hours):
        return "不在搭话时段"
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    cnt = _nudge_count.get(today, {})
    if over_cap(cnt.get(qq, 0), cfg.nudge_per_user_daily_max):
        return "今天已经搭过话了"
    if over_cap(cnt.get("all", 0), cfg.nudge_daily_max):
        return "今天搭话次数用完了"
    if not _has_quota(None, qq):
        return "额度不够"
    return None


async def nudge(bot: Bot, qq: int, force: bool = False) -> str | None:
    """冷场了，主动再说一句；说了就返回说的话"""
    key = f"private_{qq}"
    if not cfg.deepseek_api_key:
        return None
    async with _locks[key]:
        history = get_history(key)
        last = history[-1] if history else None
        if not force and (not last or last.get("role") != "assistant"):
            return None                              # 对方已经回了（或者还没聊过）
        gap = human_gap(time.time() - float(last.get("ts") or time.time())) if last else "一阵子"
        fam = core.familiarity_of(qq)
        prof = ltm.get_user(qq)
        long_memo = ltm.context_for(qq, prof.get("name") or str(qq), None)
        extra = "\n\n".join(x for x in (long_memo, now_hint(), NUDGE_PROMPT.format(gap=gap), FAMILIARITY_HINT[fam] + short_hint()) if x)
        messages = api_messages([{"role": "system", "content": system_prompt()}] + history[-10:]
                                + [{"role": "system", "content": extra}])
        try:
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model, messages=messages, temperature=cfg.llm_temperature,
                max_tokens=cfg.short_reply_max_tokens, stop=CHAT_STOP, extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "nudge", user=qq)
            choice = resp.choices[0]
            reply = clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            logger.warning(f"主动搭话：调用失败（{kind or '其他错误'}）：{e}")
            if kind:
                await alert_admins(bot, kind)
            return None
        if not reply or _NO_NUDGE_RE.search(reply) or ooc_words(reply, ""):
            logger.info(f"主动搭话：{qq} 她想了想，没说")
            return None
    bubbles = split_bubbles(reply)[:2]

    def replied() -> bool:                           # 她想、打字的时候，对方回消息了：这句就不发了
        h = get_history(key)
        return bool(_inbox.get((key, qq))) or (bool(h) and h[-1] is not last)
    async with _hands:
        if not force and replied():
            logger.info(f"主动搭话：{qq} 对方刚好回了，不发了")
            return None
        await switch_pause(key)
        await asyncio.sleep(typing_delay(bubbles[0]))
        if not force and replied():
            logger.info(f"主动搭话：{qq} 对方刚好回了，不发了")
            return None
        for i, b in enumerate(bubbles):
            if i:
                await asyncio.sleep(bubble_gap(b))
            try:
                await bot.send_private_msg(user_id=qq, message=b)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"主动搭话：发给 {qq} 失败：{e}")
                if not i:
                    return None
                break
            _count_sent(None, qq)
        mark_sent(key)
    said = "\n".join(bubbles)
    get_history(key).append({"role": "assistant", "content": said, "ts": time.time()})
    save_history(key)
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    for d in [d for d in _nudge_count if d != today]:
        del _nudge_count[d]
    cnt = _nudge_count.setdefault(today, {})
    cnt[qq] = cnt.get(qq, 0) + 1
    cnt["all"] = cnt.get("all", 0) + 1
    logger.info(f"主动搭话：{ltm.TIER_NAMES.get(core.familiarity_of(qq), '')} {prof.get('name') or qq}（{qq}）冷场 {gap}，她说：{said[:30]}")
    return said


async def check_nudges(bot: Bot | None = None) -> int:
    """到点的冷场，挨个看要不要搭话；返回搭了几次"""
    now = time.time()
    due = [qq for qq, d in _nudge_due.items() if d["at"] <= now]
    if not due:
        return 0
    bots = [bot] if bot else list(get_bots().values())
    if not bots:
        return 0
    n = 0
    for qq in due:
        _nudge_due.pop(qq, None)                     # 只搭这一次；对方回了以后才会再抽签
        why = nudge_blocker(qq)
        if why:
            logger.info(f"主动搭话：{qq} 这次不搭（{why}）")
            continue
        if await nudge(bots[0], qq):
            n += 1
    return n


async def _nudge_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await check_nudges()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查主动搭话出错：{e}")


nudge_cmd = on_command("搭话", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@nudge_cmd.handle()
async def _(bot: Bot, arg: Message = CommandArg()):
    t = arg.extract_plain_text().strip()
    if not t.isdigit():
        pending = [f"{q}（{datetime.fromtimestamp(d['at'], peak.BEIJING):%H:%M}）" for q, d in _nudge_due.items()]
        await nudge_cmd.finish("（用法：/搭话 QQ号 —— 马上让她主动跟这个人说一句。等着搭话的："
                               + ("、".join(pending) if pending else "没有") + "）")
    said = await nudge(bot, int(t), force=True)
    await nudge_cmd.finish(f"（她说了：{said}）" if said else "（她想了想，没说。或者调用失败，看日志）")


letter_cmd = on_command("写信", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@letter_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """/写信 @某人（或 QQ号、我）：让她马上给这个人写一封（测试用，不看好感和时间）"""
    try:
        friends = {int(f["user_id"]) for f in await bot.get_friend_list()}
    except Exception:  # noqa: BLE001
        friends = set()
    if arg.extract_plain_text().strip() == "检查":
        close = [qq for qq in sorted(set(ltm.user_ids()) | set(cfg.close_friends)) if letter_daily_prob(qq) > 0]
        if not close:
            await letter_cmd.finish("（现在还没有“熟人”及以上的人，不会自动写信）")
        close.sort(key=lambda q: core.familiarity_of(q) != "close")
        lines = [f"· {ltm.get_user(qq).get('name') or qq}（{qq}｜{ltm.TIER_NAMES.get(core.familiarity_of(qq), '')}｜每天 {letter_daily_prob(qq):.0%}）："
                 f"{letter_blocker(qq, friends) or '符合，随时可能收到信'}" for qq in close]
        today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
        head = f"（今天已寄 {_letters_sent.get(today, 0)}/{cap_text(cfg.letter_max_per_day)} 封｜现在{'可以' if _letter_window_now() else '不在'}寄信时段）"
        await letter_cmd.finish(head + "\n" + "\n".join(lines))
    target = _parse_target(event, arg)
    if not target or target[0] != "user":
        await letter_cmd.finish("用法：/写信 @某人（或 QQ号、我）马上写一封；/写信 检查 → 看看谁符合自动写信的条件")
    qq = target[1]
    why = letter_blocker(qq, friends, force=True)
    if why:
        await letter_cmd.finish(f"（没法给 {qq} 写信：{why}）")
    letter = await write_letter(bot, qq)
    auto = letter_blocker(qq, friends)
    note = "自动写信条件：符合" if auto is None else f"平时不会自动写：{auto}"
    await letter_cmd.finish(f"（信已寄给 {qq}｜{note}）" if letter else "（这次没写成，看看后台日志）")
