"""回消息主流程（10/22 从 __init__.py 拆出来；10/23 把 _converse 拆成五步，只挪不改）：
收到消息后合并、等对方说完、看图、拼提示、调模型、各道检查、发送、记账。
只改了两处写法：调 recall() 写成 story.recall()，用 _fc 写成 story._fc（测试要替换它们，启动时也会整个换掉 _fc）"""
import asyncio
import random
import re
import time
from datetime import datetime
from collections import defaultdict, deque
from types import SimpleNamespace

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.rule import Rule

from . import budget, commands, core, peak, quota, sleep, stickers, story, voice, voice_chat, wake
from .oocheck import ooc_words
from .echocheck import echo_hint
from .core import _arrival_seq, _chat_seq, _hands, _target_of, alert_admins, cfg, classify_error, client, ltm, mark_sent, switch_pause, tier_value
from .prompts import FAMILIARITY_HINT, LATE_HINT, WAKE_HINT, SHORT_VARIANTS, STORY_FIX_PROMPT, system_prompt, UNPROMPTED_HINT, WRAPUP_HINT, WRAPUP_SLEEP_HINT
from .history import _inbox, _locks, _passive, _unrecord, api_messages, get_history, save_history, session_key
from .turn_hints import BACK_HINT_HOURS, clock_text, gift_hint, human_gap, length_hint_for, life_hint, now_hint, reply_mode, time_hint, unprompted_hits
from .textin import _CALL_ONLY_RE, AT_ONLY_TEXT, caller_lead_start, clean_body, clip_input, gap_line, pull_own_lead, rich_text, sender_label, speaker_head, watch_block
from .textout import CHAT_STOP, clean_reply, cut_derailed, drop_ooc_sentences, is_fragment, speaker_names, split_bubbles, split_sticker
from .quota import _active_chats, _count_sent, _crossed_mid_chat, _farewell_at, _global_window, _hour_window, _last_trigger, _refund, _wrapup, bubble_gap, in_wrapup, quota_left, rate_limited, say_farewells, say_tired, typing_delay, WRAPUP_EXTRA, WRAPUP_MINUTES
from .voice_chat import _voice_count, split_voice_mark, tts, tts_ready, voice_day, voice_roll, voice_text, voice_wav_seconds
from .stickers_chat import _last_sticker, _recent_stickers, sticker_hint, sticker_roll
from .affection_turn import detect_gifts, gender_step
from .friends import is_friend_verify
from .addressed import _addressed, _away, _engaged, _last_bot_msg, _LEAVE_RE, _looks_like_question, _replied_at, allowed, away_left, note_leaving
from .proactive_private import _BYE_RE, _nudge_due, arm_nudge
from .story import _diary_topic, _last_recall, diary_followup, note_topic, story_check


# qzone_diary（问“今天的日记”时用）要等所有东西都定义好才能导入，在 __init__.py 最后导入；导入完由 __init__.py 填到这里
qzone_diary = None


# 回复长度上限（原来放在 __init__.py“表情包”一节的末尾）
def max_tokens_for(mode: str) -> int:
    return {"long": cfg.long_reply_max_tokens, "busy": cfg.peak_max_tokens}.get(mode, cfg.short_reply_max_tokens)


chat = on_message(rule=Rule(_addressed) & allowed, priority=10, block=True)


_peak_last_real: dict[int, float] = {}


_peak_last_busy: dict[int, float] = {}


_inbox_token: dict[tuple[str, int], int] = {}


_token_counter = 0


_inbox_first: dict[tuple[str, int], float] = {}   # (会话, QQ) -> 这一批第一条消息到的时间


_look_later: dict[tuple[str, int], list] = defaultdict(list)   # (会话, QQ) -> [(在收件箱里的位置, 消息事件)]：有图，要回的时候再看


def has_images(event: MessageEvent) -> bool:
    """这条消息（或它引用的消息）里有没有图片"""
    if any(seg.type == "image" for seg in event.get_message()):
        return True
    reply = getattr(event, "reply", None)
    return reply is not None and any(seg.type == "image" for seg in reply.message)


def _take_inbox(ik: tuple[str, int]) -> list[str]:
    _look_later.pop(ik, None)
    return _inbox.pop(ik, [])


async def look_at_images(ik: tuple[str, int], drop_at: bool) -> None:
    """确定要回了：把收件箱里带图的几条换成看过图的说法（调模型看图）"""
    todo = _look_later.pop(ik, [])
    box = _inbox.get(ik)
    for pos, ev in todo:
        if box is None or pos >= len(box):
            continue
        try:
            box[pos] = await rich_text(ev, look=True, drop_at=drop_at) or box[pos]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"看图失败，按没看处理：{e}")


_reply_started: dict[tuple[str, int], float] = {}  # (会话, QQ) -> 她开始回这个人的时间


_reply_done: dict[tuple[str, int], float] = {}     # (会话, QQ) -> 她回完这个人的时间
# 10/23 对方像机器人：每 (会话, QQ) 记她最后一条发出的时间、这一轮对方回了没有、最近几轮是不是“几秒内引用回”、不回到什么时候
_botlike: dict[tuple[str, int], dict] = {}


def note_round(ik: tuple[str, int], event: MessageEvent, self_id: int) -> None:
    """对方来了一条：如果是她发完以后的第一条（新的一轮），记下这一轮是不是“几秒内引用她的话回”"""
    st = _botlike.get(ik)
    if not st or not st.get("open"):
        return
    st["open"] = False
    rep = getattr(event, "reply", None)
    quoted = bool(rep) and str(getattr(getattr(rep, "sender", None), "user_id", "")) == str(self_id)
    gap = float(getattr(event, "time", 0) or time.time()) - st["last_out"]
    now = time.time()
    st["rounds"].append((now, quoted and gap <= cfg.botlike_seconds))
    while st["rounds"] and now - st["rounds"][0][0] > cfg.botlike_window_minutes * 60:
        st["rounds"].popleft()


def botlike_state(ik: tuple[str, int]) -> str | None:
    """"mute"：这段时间不回；"slow"：回之前多等一会儿；None：照常"""
    if not cfg.botlike_enabled:
        return None
    st = _botlike.get(ik)
    if not st:
        return None
    now = time.time()
    if st.get("mute_until", 0) > now:
        return "mute"
    rounds = list(st["rounds"])
    if sum(r for _, r in rounds) >= cfg.botlike_mute:
        st["mute_until"] = now + cfg.botlike_mute_minutes * 60
        st["rounds"].clear()
        logger.warning(f"对方像是另一个机器人：{ik[0]} 最近 {cfg.botlike_window_minutes:g} 分钟里 {cfg.botlike_mute} 轮都在几秒内引用她的话回，"
                       f"接下来 {cfg.botlike_mute_minutes:g} 分钟不回")
        return "mute"
    if sum(r for _, r in rounds[-cfg.botlike_rounds:]) >= cfg.botlike_slow:
        return "slow"
    return None


def note_sent_round(ik: tuple[str, int]) -> None:
    """她这一轮发完了：开始新的一轮"""
    st = _botlike.setdefault(ik, {"rounds": deque(maxlen=64)})
    st["last_out"], st["open"] = time.time(), True


_woke: dict[tuple[str, int], list] = {}    # (会话, QQ) -> 这一轮收件箱最前面几段是她睡着时收到的：[(原话, 收到的时间)]（10/24，见 wake.py）
_reply_owner: dict[tuple[str, int], "asyncio.Task | None"] = {}   # (会话, QQ) -> 正在回这个人的那一轮（10/23）


def is_sticker_only(event: MessageEvent) -> bool:
    """这条消息只有表情（QQ 表情、商城表情、表情包图片），没有文字"""
    segs = [x for x in event.get_message()
            if x.type != "at" and not (x.type == "text" and not x.data.get("text", "").strip())]
    def sticker(x) -> bool:
        if x.type in ("face", "mface"):
            return True
        return x.type == "image" and (str(x.data.get("sub_type")) == "1" or "表情" in str(x.data.get("summary", "")))
    return bool(segs) and all(sticker(x) for x in segs)


def sticker_follows_reply(ik: tuple[str, int]) -> bool:
    """她正在回这个人，或者刚回完：这时单独来的表情，算对方上一条消息的一部分"""
    now = time.monotonic()
    started, done = _reply_started.get(ik, -1e9), _reply_done.get(ik, -1e9)
    replying = started > done and now - started < 120
    return replying or now - done <= cfg.sticker_follow_seconds


async def wait_own_reply(ik: tuple[str, int], token: int) -> int | None:
    """她还在回这个人上一轮（在想、或者分几条还没发完）：等她发完，再等一小会儿，把这期间对方发的几条合在一起回。
    9/30 私聊：她第一条刚发出去，对方就接着回了两句，结果这两句被拆成两轮、各自在她没发完的时候就开始想，
    看到的“她说过的话”里有还没发出去的那条，前言不搭后语。期间对方又发了新消息，返回 None（交给最新那条）"""
    t0 = time.monotonic()
    waited = False
    while _reply_started.get(ik, -1e9) > _reply_done.get(ik, -1e9) and time.monotonic() - t0 < 120:
        waited = True
        await asyncio.sleep(0.5)
        if _inbox_token.get(ik) != token:
            return None
    if waited:
        await asyncio.sleep(cfg.merge_wait_complete)     # 她刚发完，对方可能正在回最后那条
        if _inbox_token.get(ik) != token:
            return None
    return token


# ---- 话说完了没有：决定等多久
_UNFINISHED_TAIL = ("，", ",", "、", "：", ":", "…", "...", "；", ";", "（", "(", "“", "—")


_UNFINISHED_WORDS = (
    "然后", "就是", "而且", "但是", "不过", "所以", "因为", "结果", "还有", "话说", "对了", "那个", "这个",
    "我跟你说", "跟你说", "听我说", "问你个事", "问你件事", "问你个问题", "问个问题", "问一下", "请问",
    "等下", "等等", "等一下", "我想", "我觉得", "其实", "比如", "关于", "和", "跟", "的话", "如果", "要是",
)


def merge_delay(text: str, names: list[str] | tuple = ()) -> float:
    """看这条消息像不像话还没说完：没说完多等一会儿，说完了少等一会儿"""
    t = text.strip()
    core = re.sub(r"[\s。.!！~～?？]+$", "", t)
    if not core:
        return cfg.merge_wait
    bare = re.sub(r"[\s,，。.!！~～?？]", "", t)
    if t == "（@了你一下，没说话）" or _CALL_ONLY_RE.fullmatch(bare) or any(bare == n or bare == f"@{n}" for n in names if n):
        return cfg.merge_wait_incomplete          # 只叫了她一声 / 问在不在：正事多半在下一条
    if t.endswith(_UNFINISHED_TAIL) or core in _UNFINISHED_WORDS or core.endswith(_UNFINISHED_WORDS):
        return cfg.merge_wait_incomplete
    if t[-1] in "?？!！。~～" or core.endswith(("吗", "呢", "吧", "嘛", "啊", "呀", "啦", "了")) or len(core) >= 12:
        return cfg.merge_wait_complete
    return cfg.merge_wait


async def wait_for_more(ik: tuple[str, int], text: str, names: list[str] | tuple = (),
                        look: "MessageEvent | None" = None) -> int | None:
    """放进收件箱等一会儿（按话说完没说完决定等多久）；期间同一个人又发了新消息，返回 None（让最新那条来回复）。
    look：这条有图，确定要回的时候再看"""
    global _token_counter
    now = time.monotonic()
    if not _inbox[ik]:
        _inbox_first[ik] = now
        _look_later.pop(ik, None)
    if look is not None:
        _look_later[ik].append((len(_inbox[ik]), look))
    _inbox[ik].append(text)
    _token_counter += 1
    token = _inbox_token[ik] = _token_counter
    left = cfg.merge_wait_max - (now - _inbox_first.get(ik, now))    # 从第一条算起最多等 merge_wait_max 秒
    await asyncio.sleep(max(0.0, min(merge_delay(text, names), left)))
    return token if _inbox_token.get(ik) == token else None


# ---- 水话：只是应一声、笑一下、发个表情
_FILLER_RE = re.compile(
    r"(?:哈|呵|嘿|嘻|hh|h|233+|w|嗯|恩|哦|噢|喔|额|呃|啊|好的?|好吧|好滴|好哒|行吧?|ok|okk|收到|知道了|晓得了|懂了|了解|明白了?"
    r"|确实|是的?|对的?|嗯呢|也是|笑死|绷|草|\[表情[^\]]*\]|\[语音\])+",
    re.I,
)


_SKIP_RE = re.compile(r"[\[【（(［]\s*不回\s*[\]】）)］]")


def is_filler(text: str) -> bool:
    bare = re.sub(r"[\s,，。.!！~～…]", "", text)
    return bool(bare) and bool(_FILLER_RE.fullmatch(bare))


def she_asked(history: list[dict]) -> bool:
    """她上一句是不是在问对方（问了的话，对方“嗯”一声也算回答，要接着聊）"""
    last = next((h["content"] for h in reversed(history) if h["role"] == "assistant"), "")
    last = re.sub(r"\[发了表情[^\]]*\]", "", last).strip()
    return bool(last) and (last[-1] in "?？" or last.endswith(("吗", "呢", "吧", "嘛")))


@chat.handle()
async def _(bot: Bot, event: MessageEvent):
    await converse(bot, event)


async def converse(bot: Bot, event: MessageEvent, catchup_age: float | None = None) -> None:
    """回复一条（或连着的几条）消息。catchup_age 不为空时，表示这是上线后补回的未读消息，值是最早那条过去了几秒"""
    ik = (session_key(event), event.user_id)
    budget.actor.set((event.user_id, event.group_id if isinstance(event, GroupMessageEvent) else None))   # 看图这些花的钱也算到这个人 / 这个群头上
    try:
        await _converse(bot, event, catchup_age)
    finally:
        # 不管是回完了，还是中途不回（出错、她觉得没必要接话、已经告别……），都记下“这一轮结束了”。
        # 10/23：只有真正开始回的那一轮才记。以前被后来的消息合并掉、根本没开始回的那几条也会记“结束了”，
        # 对方连着发的时候（10/23 晚某位新好友是个每条都回的机器人），她上一轮还在打字，下一轮就开始想了，
        # 一轮接一轮排队，对方停了以后她还发了三分多钟
        if _reply_owner.get(ik) is asyncio.current_task():
            _reply_owner.pop(ik, None)
            if _reply_started.get(ik, -1e9) > _reply_done.get(ik, -1e9):
                _reply_done[ik] = time.monotonic()


async def _converse(bot: Bot, event: MessageEvent, catchup_age: float | None = None) -> None:
    """10/23 拆成五步（只挪不改，每一步的代码和以前一模一样）：
    ① _admit 准入：合并连发、等对方说完、她不在 / 水话 / 高峰 / 讨厌的人 / 限流，不回的就在这里返回
    ② _build_prompt 拼提示：拿收件箱、旁听、回忆、长期记忆、各种本轮提示，拼成发给模型的 messages
    ③ _generate 调模型、检查、重说：出戏、记混、讲往事核对、外文截断、半截字、没人提的蘑菇
    ④ _finalize 定稿：[不回]、表情、语音、写进聊天记录
    ⑤ _send 发送：打字延迟、分条发、发不出去的退回、交给长期记忆、限额告别、搭话
    ②～④ 和以前一样在这个会话的锁里做，⑤在锁外"""
    turn = await _admit(bot, event, catchup_age)
    if turn is None:
        return
    async with _locks[turn.key]:
        if not await _build_prompt(turn):
            return
        got = await _generate(bot, turn)
        if got is None:
            return
        if not await _finalize(turn, *got):
            return
    await _send(bot, turn)


async def _admit(bot: Bot, event: MessageEvent, catchup_age: float | None) -> "SimpleNamespace | None":
    """① 准入。要回就返回这一轮的上下文（后面几步共用），不回返回 None"""
    global _token_counter
    # 先不调模型看图（本机角色识别照常，免费）：等确定要回了再看，被合并、限流、跳过的消息就不花这个钱
    drop_at = isinstance(event, GroupMessageEvent)
    text = await rich_text(event, look=False, drop_at=drop_at)
    look_later = has_images(event) and cfg.vision_enabled and (cfg.vision_in_peak or not core.in_peak())
    if not text:
        text = AT_ONLY_TEXT

    if not cfg.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，已跳过回复")
        await alert_admins(bot, "nokey")
        return None
    key = session_key(event)
    is_group = isinstance(event, GroupMessageEvent)

    # 连续发送：先把这条放进收件箱，等几秒；期间同一个人又发了，就交给最新那条一起回复
    ik = (key, event.user_id)
    if catchup_age is None:
        note_round(ik, event, int(bot.self_id))       # 10/23：对方是不是她一发完就引用着回（像机器人）
    # 消息发完接一个表情：她已经在回（或者刚回完）上一条了，这个表情算同一条，不单独回
    if (catchup_age is None and cfg.sticker_follow_seconds > 0 and is_sticker_only(event)
            and not _inbox.get(ik) and sticker_follows_reply(ik)):
        async with _locks[key]:
            history = get_history(key)
            name = sender_label(event)
            history.append({"role": "user", "content": speaker_head(event) + clean_body(text) if is_group else text,
                            "uid": event.user_id, "name": name, "ts": time.time()})
            save_history(key)
        logger.info(f"跟在消息后面的表情，算同一条，不单独回 user={event.user_id}")
        return None
    if catchup_age is None:
        names = list(cfg.smart_names) + list(getattr(bot.config, "nickname", None) or [])
        token = await wait_for_more(ik, text, names, event if look_later else None)
        if token is None:
            return None
        token = await wait_own_reply(ik, token)
        if token is None:
            return None
        if isinstance(event, PrivateMessageEvent) and len(_inbox.get(ik, [])) == 1 \
                and is_friend_verify(event.user_id, text, event.time):
            # “我是某某”这种验证消息先到、“加好友成功”的通知后到：等过这几秒再看一次
            _take_inbox(ik)
            logger.info(f"加好友的验证消息，不回 user={event.user_id}：{text[:30]}")
            return None
        # 10/24 她睡着时这个人说过的话（醒来以后还没看）：并进这一轮一起看，不再单独补
        earlier = wake.take(ik) if not sleep.asleep() else None
        if earlier:
            items, _first = earlier
            _inbox[ik][:0] = [t for t, _ in items]
            if _look_later.get(ik):
                _look_later[ik] = [(i + len(items), ev) for i, ev in _look_later[ik]]
            _woke[ik] = items
            logger.info(f"睡着时 {event.user_id} 说的 {len(items)} 段，并进这一轮一起看")
    else:                                 # 补回未读：几条已经合在一起了，不用再等
        if not _inbox[ik]:
            _look_later.pop(ik, None)
        if look_later:
            _look_later[ik].append((len(_inbox[ik]), event))
        _inbox[ik].append(text)
        _token_counter += 1
        token = _inbox_token[ik] = _token_counter

    # 10/23 对方像另一个机器人（她每发一轮，对方都在几秒内引用回）：先放慢，还这样就一阵子不回
    bl = botlike_state(ik) if catchup_age is None else None
    if bl == "slow":
        wait = random.uniform(*cfg.botlike_slow_delay)
        logger.info(f"对方像是另一个机器人，放慢：{key} 等 {wait:.0f} 秒再回")
        await asyncio.sleep(wait)
        if _inbox_token.get(ik) != token:
            return None
        bl = botlike_state(ik)
    if bl == "mute":
        texts = _take_inbox(ik)
        if texts:
            async with _locks[key]:
                history = get_history(key)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
                                "uid": event.user_id, "name": sender_label(event), "ts": time.time()})
                save_history(key)
        logger.info(f"对方像是另一个机器人，这阵子不回 {key} user={event.user_id}")
        return None
    # 她刚说过“我要去赶路了”：这段时间真的不在，对方的话记下来，回来以后再说
    target = _target_of(event)
    if catchup_age is None and away_left(target) > 0:
        texts = _take_inbox(ik)
        if texts:
            async with _locks[key]:
                history = get_history(key)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
                                "uid": event.user_id, "name": sender_label(event), "ts": time.time()})
                save_history(key)
        logger.info(f"她说过要走了，还有 {away_left(target) / 60:.0f} 分钟才回来，先不回 {target} user={event.user_id}")
        return None
    # 水话（“哈哈”“嗯”“好的”、一个表情）：按关系远近，有一定概率直接不接话（不调用模型）
    pending = _inbox.get(ik, [])
    if cfg.skip_filler and pending and all(is_filler(t) for t in pending):
        async with _locks[key]:
            history = get_history(key)
            if not she_asked(history) and random.random() < tier_value(cfg.skip_filler_prob, core.familiarity_of(event.user_id), 0.5):
                texts = _take_inbox(ik)
                name = sender_label(event)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
                                "uid": event.user_id, "name": name, "ts": time.time()})
                save_history(key)
                logger.info(f"水话，不接了 user={event.user_id}：{joined[:20]}")
                return None
    # 高峰时段：很忙。每人每 10 分钟最多一次正经回复；其余时候随机回一句“在忙”（不调用模型）或干脆不回
    busy = core.in_peak()
    if busy:
        now = time.monotonic()
        recent_real = now - _peak_last_real.get(event.user_id, -1e9) < cfg.peak_user_interval
        recent_busy = now - _peak_last_busy.get(event.user_id, -1e9) < cfg.peak_user_interval
        if recent_real or random.random() < cfg.peak_busy_prob:
            texts = _take_inbox(ik)
            if recent_busy or not texts:
                logger.info(f"高峰时段，忙着没回 user={event.user_id}")
                return None
            if quota_left(event.group_id if is_group else None, None if is_group else event.user_id) <= 0:     # “在忙”也算一条，额度用完就不回了
                logger.info(f"高峰时段，额度用完了，“在忙”也不回 user={event.user_id}")
                return None
            _peak_last_busy[event.user_id] = now
            _count_sent(event.group_id if is_group else None, None if is_group else event.user_id)
            line = peak.busy_line()
            async with _locks[key]:
                history = get_history(key)
                name = sender_label(event)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
                                "uid": event.user_id, "name": name, "ts": time.time()})
                history.append({"role": "assistant", "content": line, "ts": time.time()})
                save_history(key)
            await asyncio.sleep(random.uniform(cfg.peak_extra_delay_min, cfg.peak_extra_delay_max))
            async with _hands:
                await switch_pause(_target_of(event))
                try:
                    await bot.send(event, line)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"“在忙”没发出去：{e}")
                    return None
                mark_sent(_target_of(event))
            return None
        _peak_last_real[event.user_id] = now

    # 被讨厌的人：有一定概率直接不理（不花 token）
    if core.familiarity_of(event.user_id) == "disliked" and random.random() < cfg.dislike_ignore_prob:
        _take_inbox(ik)
        logger.info(f"讨厌的人，不想理 user={event.user_id}")
        return None
    limited = rate_limited(event.user_id, event.group_id if is_group else None, private=not is_group, target=_target_of(event))
    waited = 0.0
    while limited in ("cooldown", "global"):
        # 个人冷却 / 这一分钟回太多了：排队等一等再回，而不是直接丢掉
        if limited == "cooldown":
            wait = cfg.user_cooldown - (time.monotonic() - _last_trigger.get(event.user_id, -1e9))
        else:
            wait = 60 - (time.monotonic() - _global_window[0]) + random.uniform(1, 5) if _global_window else 1
        wait = max(0.1, wait)
        if waited + wait > max(cfg.queue_max_wait, cfg.user_cooldown + 1):
            break
        await asyncio.sleep(wait)
        waited += wait
        if _inbox_token.get(ik) != token:
            return None
        limited = rate_limited(event.user_id, event.group_id if is_group else None, private=not is_group, target=_target_of(event))
    if limited and limited.startswith("budget") and _crossed_mid_chat(_target_of(event)):
        # 9/30：线是在两轮之间被跨过去的（多半是回完话后的记忆整理），当时没来得及告别。
        # 她正在跟这个人聊、而且上一句在问对方：先把话收个尾（最多再回 2 次）；否则就在这里补一句告别
        target = _target_of(event)
        if _active_chats[target].get("user_id") == event.user_id and she_asked(get_history(key)) and target not in _wrapup:
            _wrapup[target] = {"user": event.user_id, "left": WRAPUP_EXTRA, "at": time.time()}
            logger.info(f"额度在两轮之间用完了，但她刚问了 {event.user_id} 话，先收个尾再走：{target}")
            limited = rate_limited(event.user_id, event.group_id if is_group else None, private=not is_group, target=target)
        else:
            logger.info(f"限流跳过 user={event.user_id} reason={limited}（额度在两轮之间用完了，补一句告别）")
            _take_inbox(ik)
            async with _hands:
                await say_farewells(bot, event, target)
            return None
    if limited:
        logger.info(f"限流跳过 user={event.user_id} reason={limited}")
        texts = _take_inbox(ik)
        _woke.pop(ik, None)
        if limited == "sleep" and catchup_age is None:
            wake.note(ik, event, "\n".join(texts))   # 10/24 睡着时来找她的：记下来，醒了照平常的规则看要不要回
        if limited.startswith("budget") and not is_group:
            await say_tired(bot, event)
        return None
    admitted_at = time.monotonic()           # 这条从这时开始算“要回”；之后如果已经告别了，就不发了
    stamp = _hour_window[-1] if _hour_window else admitted_at      # 这次占用的额度（最后没发出去就退回）
    gid_q = event.group_id if is_group else None
    uid_q = None if is_group else event.user_id
    if _look_later.get(ik):
        await look_at_images(ik, drop_at)
    return SimpleNamespace(bot=bot, event=event, catchup_age=catchup_age, drop_at=drop_at, key=key, is_group=is_group,
                           ik=ik, token=token, target=target, busy=busy, admitted_at=admitted_at, stamp=stamp,
                           gid_q=gid_q, uid_q=uid_q)


async def _build_prompt(turn: SimpleNamespace) -> bool:
    """② 拼提示（在会话锁里）。收件箱已经被前一条合并回复过了就返回 False"""
    event, key, ik, is_group, busy, catchup_age, target = turn.event, turn.key, turn.ik, turn.is_group, turn.busy, turn.catchup_age, turn.target
    texts = _take_inbox(ik)
    if not texts:                     # 已经被前一条合并回复过了
        return False
    woke_items = _woke.pop(ik, None)              # 10/24 收件箱最前面几段是她睡着时收到的（带着收到的时间）
    woke = woke_items[0][1] if woke_items else None
    _reply_started[ik] = time.monotonic()
    _reply_owner[ik] = asyncio.current_task()      # 谁开始回的，谁回完了才记“结束了”（见 converse）
    text = clip_input("\n".join(texts))
    # 这条消息到的时候的计数（之后有人插话、或者她先说了别的，就引用原句）
    seq_at_trigger = _arrival_seq.pop((_target_of(event), int(event.message_id)), _chat_seq[_target_of(event)])
    history = get_history(key)
    new_entries: list[dict] = []

    name = sender_label(event)
    buf: list = []
    if is_group and _passive.get(event.group_id):
        buf = list(_passive.pop(event.group_id))
        lead, buf = pull_own_lead(buf, event.user_id, text, time.time())
        if lead:                          # “对吧伊蕾娜小姐”“……/ @伊蕾娜”：把他前面那几句一起当成对她说的
            text = "\n".join(lead + ([] if text.strip() == AT_ONLY_TEXT else [text]))
    else:
        lead = []
    if buf:
        # 叫她的这句有内容、没把前几句合进来时：前几句是叫她的人刚说的，标出来让她自己判断是不是说给她听的
        mark = caller_lead_start(buf, event.user_id, time.time()) if not lead else None
        new_entries.append({
            "role": "user",
            "content": watch_block([(ts, ln) for _, _, ln, ts in buf], mark_from=mark, end_ts=time.time()),
            "speakers": {n: uid for uid, n, _, _ in buf},
            "ts": time.time(),
        })

    user_content = speaker_head(event) + clean_body(text) if is_group else text
    new_entries.append({"role": "user", "content": user_content, "uid": event.user_id, "name": name, "ts": time.time()})
    if woke_items and not lead:
        # 10/24 睡着时收到的那几段：每段单独一条，带上对方发的时间（“（凌晨 4:43）……”），记进聊天记录也用那个时间
        def _sleep_entry(t: str, at: float) -> dict:
            lab = f"（{clock_text(datetime.fromtimestamp(at, peak.BEIJING))}）{t}"
            return {"role": "user", "content": speaker_head(event) + clean_body(lab) if is_group else lab,
                    "uid": event.user_id, "name": name, "ts": at}
        sleep_entries = [_sleep_entry(t, at) for t, at in woke_items]
        cur = new_entries.pop()
        rest = texts[len(woke_items):]
        if rest:                                  # 醒来以后对方又说的：照常一条，时间是现在
            body = clip_input("\n".join(rest))
            cur["content"] = speaker_head(event) + clean_body(body) if is_group else body
            new_entries = sleep_entries + new_entries + [cur]
        else:
            new_entries = sleep_entries[:-1] + new_entries + sleep_entries[-1:]
    # 离上一段聊天隔得久：在这一轮前面标一行“（过了 X）”
    prev_ts = next((h["ts"] for h in reversed(history) if h.get("ts")), None)
    g = gap_line(prev_ts, new_entries[0]["ts"] if woke_items else time.time())
    if g:
        new_entries[0]["content"] = g + new_entries[0]["content"]

    # 检索回忆：用本条消息；找不到时带上此前最近一条用户消息再试一次（应对“那后来呢？”）
    prev_user = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
    prev_bot = next((h["content"] for h in reversed(history) if h["role"] == "assistant"), "")
    # 高峰时段不检索小说（省 token）
    # 在聊她的日记 / 说说（“有今天的旅行日记吗”“然后呢”）：给她日记的真实情况，不去翻以前的旅途故事，
    # 免得把小说里某段“日记”的故事当成今天的日记讲
    diary_force = diary_followup(key, text)
    diary_memo = qzone_diary.diary_context(text, force=diary_force)
    if diary_memo and qzone_diary.asks_today_diary(text, diary_force):
        _diary_topic[key] = time.time()
        _last_recall.pop(key, None)
        memo = ""
    else:
        memo = story.recall(text, f"{prev_user[-60:]} {prev_bot[-120:]}", key) if cfg.knowledge_enabled and not busy else ""
    long_memo = ltm.context_for(event.user_id, name, event.group_id if is_group else None,
                                text=text, recent=f"{prev_user[-60:]} {prev_bot[-120:]}")   # 按这句话挑几条；群里不带私聊知道的事
    try:                                          # 10/23【你的近况】：她自己说过的经历、在哪（出错不影响回复）
        life_memo = life_hint(core.self_mem.life_for_chat(key, text, f"{prev_user[-60:]} {prev_bot[-120:]}"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"近况：取用出错，这次不带：{e}")
        life_memo = ""
    space_memo = ""
    if not is_group:                              # 10/27 空间 agent 交接：私聊时带上最近在 QQ 空间的往来（两个人之间的事，群里不提）
        try:
            space_memo = qzone_diary.space_memo(event.user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"空间往来：取用出错，这次不带：{e}")
    mode = "busy" if busy else reply_mode(text)
    fam = core.familiarity_of(event.user_id)
    length_hint = length_hint_for(mode, text, fam)
    time_memo = "\n".join(x for x in (now_hint(), time_hint(history, ltm.last_seen(event.user_id), fam, ltm.get_user(event.user_id).get("last_letter"))) if x)
    if in_wrapup(target, event.user_id) or (_wrapup.get(target) or {}).get("user") == event.user_id:
        time_memo = "\n".join(x for x in (time_memo, WRAPUP_SLEEP_HINT if sleep.asleep() else WRAPUP_HINT) if x)
    else:
        time_memo = "\n".join(x for x in (time_memo, quota.winddown_hint(event.group_id if is_group else None, event.user_id)) if x)
    back = _away.pop(target, None)                # 她之前说要走、现在回来了
    if back and time.time() - back["said"] < BACK_HINT_HOURS * 3600:   # 隔太久了就不提“刚回来”
        time_memo = "\n".join(x for x in (time_memo, f"【刚回来】你 {human_gap(time.time() - back['said'])}前说了要走（“{back['line'][:20]}”），现在才回来。"
                                                        "可以自然地带一句刚回来，不要装作没说过要走；这段时间对方发的话在上面。") if x)
    gender_memo = "" if commands.in_test_mode(event.user_id) else await gender_step(event.user_id, text, prev_bot)   # 测试模式：不记性别说法；10/23 起由模型判断，要带上她上一句
    gifts = detect_gifts(text)
    bread_first = any(k == "bread" for k, _ in gifts) and not ltm.bread_given_today(event.user_id)
    gift_memo = "\n".join(gift_hint(k, snip[:20], bread_first, fam) for k, snip in gifts)
    late_memo = ""
    if woke is not None:                           # 10/24 里面有她睡着时收到的话
        late_memo = WAKE_HINT.format(gap=human_gap(time.time() - woke))   # 不接 LATE_HINT：那里让她自己找理由，这里理由就是睡着了
    elif catchup_age is not None:
        late_memo = f"【刚看到】对方这几条消息是你不在的时候发的，最早一条已经是 {human_gap(catchup_age)}前了，你现在才看到。" + LATE_HINT
    # 对方在反问她上一句里、对方自己没提过的词（“诶？蘑菇？哪有蘑菇”）：告诉她这个词是她先说的
    echo_memo = ""
    last_a = next((i for i in range(len(history) - 1, max(-1, len(history) - 5), -1) if history[i]["role"] == "assistant"), None)
    if last_a is not None:          # 只看她最近这条回复；对方的话只看她说这句之前的（之后的可能就是在追问）
        echo_memo = echo_hint(text, history[last_a]["content"],
                              [h["content"] for h in history[max(0, last_a - 10):last_a] if h["role"] == "user"])
    # 表情：先抽签，抽中了才告诉她这轮可以甩一张（有人发她的画像时更容易抽中）
    target = _target_of(event)
    self_image = "[图片：伊蕾娜本人的画像" in text
    use_sticker, sticker_allowed, sticker_emotions = sticker_roll(
        target, fam, is_group, event.group_id if is_group else None, self_image)
    sticker_memo = ""
    if use_sticker:
        only_ok = length_hint == SHORT_VARIANTS[0][1] and random.random() < cfg.sticker_only_prob
        sticker_memo = sticker_hint(sticker_emotions, only_ok)
    # 语音：抽签 / 对方要她发语音时告诉她可以用传声魔法（电脑上的语音服务连着才有）
    voice_auto, voice_memo = voice_roll(fam, is_group, text)
    skip_memo = ""
    if (cfg.skip_by_model and (catchup_age is None or woke is not None) and len(text) <= 12 and not _looks_like_question(text)
            and not she_asked(history) and not gifts):
        skip_memo = ("【可以不回】对方这句像是随口一说（附和、应一声、客套、道别之类）。"
                     "如果你觉得没必要接话，就只输出「[不回]」这三个字符；想回就正常回。")
    extra = "\n\n".join(x for x in (long_memo, life_memo, memo, time_memo, gender_memo, diary_memo, space_memo, late_memo, echo_memo, gift_memo, FAMILIARITY_HINT[fam] + length_hint, sticker_memo, voice_memo, skip_memo) if x)
    recall_msg = [{"role": "system", "content": extra}]

    messages = api_messages(
        [{"role": "system", "content": system_prompt()}]
        + history
        + new_entries[:-1]
        + recall_msg          # 长期记忆和回忆只放在本轮，不写进聊天记忆
        + new_entries[-1:]
    )
    turn.__dict__.update(text=text, name=name, history=history, new_entries=new_entries, seq_at_trigger=seq_at_trigger,
                         memo=memo, mode=mode, fam=fam, bread_first=bread_first, use_sticker=use_sticker,
                         sticker_allowed=sticker_allowed, voice_auto=voice_auto, voice_memo=voice_memo, messages=messages,
                         target=target)
    return True


async def _ask(turn: SimpleNamespace, more: list[dict]) -> tuple[str, str | None]:
    """调一次模型（more：重说时接在后面的“她刚才那句 + 哪里不对”），返回清理过的（回复, 想甩的表情）"""
    resp = await client.chat.completions.create(
        model=cfg.deepseek_model,
        messages=turn.messages + more,
        temperature=cfg.llm_temperature,
        max_tokens=max_tokens_for(turn.mode), stop=CHAT_STOP,
        extra_body={"thinking": {"type": "enabled" if cfg.llm_thinking else "disabled"}},
    )
    budget.track(resp, "chat", user=turn.event.user_id, group=turn.gid_q)
    choice = resp.choices[0]
    return split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))


def _cut(turn: SimpleNamespace, reply: str, context: str) -> str:
    """外文 / 替人说话截断、半截字（不调模型）"""
    cut = cut_derailed(reply, context, speaker_names(turn.event))
    if cut != reply:
        logger.info(f"回复中间冒出没人提过的外文，从那句起截掉：{reply[:60]!r} → {cut[:40]!r}")
        reply = cut
    if is_fragment(reply):
        logger.info(f"回复只剩半截（{reply!r}），这轮不发")
        reply = ""
    return reply


async def _generate(bot: Bot, turn: SimpleNamespace) -> "tuple[str, str | None] | None":
    """③ 调模型、检查、重说（在会话锁里）。返回（回复, 想甩的表情）；调用出错返回 None（额度已退回）。
    10/23 改（《发前清理清单》5.2，用户定的）：以前每道检查各自重说，重说出来的那一版只再查后面几道（比如记混重说以后不再查出戏）。
    现在：第一遍按顺序查（出戏 → 记混 → 讲往事核对 → 外文截断 / 半截字 → 没人提的蘑菇），第一道出问题的让她重说一次；
    重说出来的那一版整套再查一遍，但不再重说：出戏的句子删掉、记混的句子删掉（删光了说记不清）、截断和半截字照旧，
    讲往事核对不再查（它只能靠重说改），蘑菇只记日志。一轮最多重说一次（以前最多四次）"""
    event, busy, gid_q, uid_q, stamp = turn.event, turn.busy, turn.gid_q, turn.uid_q, turn.stamp
    history, memo, text = turn.history, turn.memo, turn.text
    context = text + "\n" + "\n".join(str(h.get("content", "")) for h in history[-6:])
    fc_on = story._fc is not None and cfg.fact_check
    try:
        reply, emotion = await _ask(turn, [])
        # 第一遍：第一道出问题的检查给出“哪里不对”，让她重说一次
        why = None
        bad = ooc_words(reply, text)
        wrong = [] if bad else (story._fc.check(reply) if fc_on else [])
        problems = None if (bad or wrong) else (await story_check(reply, memo, event.user_id, gid_q) if not busy else None)
        if bad:
            # 出戏了：让她以伊蕾娜的身份重说一次
            logger.info(f"回复出戏（{bad}），重新生成：{reply[:40]}")
            why = f"刚才的回复出戏了（出现了：{'、'.join(bad)}）。伊蕾娜不知道这些东西。请完全以伊蕾娜的身份重新回复这条消息，不要道歉，不要解释。"
        elif wrong:
            # 把某人和一段没有他的经历拼在一起了（比如“在梦回之城遇上艾姆妮西亚”）：告诉她哪里记混了，重说一次
            logger.info(f"回复记混了（{'、'.join(f'{m.place}+{m.person}' for m in wrong)}），重新生成：{reply[:40]}")
            why = story._fc.correction(wrong)
        elif problems:
            # 讲的往事和资料对不上：告诉她哪里讲错了，重说一次
            logger.info(f"讲的往事和资料对不上（{'；'.join(problems)[:80]}），重新生成：{reply[:40]}")
            why = STORY_FIX_PROMPT.format(problems="\n".join(f"- {x}" for x in problems))
        else:
            reply = _cut(turn, reply, context)
            odd = unprompted_hits(reply, context)
            if odd:
                # 没人提蘑菇，她自己突然冒出一句（9/29“少拿我跟蘑菇相提并论”、10-03“你连蘑菇和沙耶都分不清”）：
                # 不按句子删（删完会剩半截“好感”），整条退回让她自己看：和话题有关就留着，无关就重说（10-03）
                logger.info(f"回复里提到了没人说过的“{'、'.join(odd)}”，退回让她再看一眼：{reply[:60]!r}")
                why = UNPROMPTED_HINT.format(words="、".join(w for w in dict.fromkeys(odd) if not any(w != x and w in x for x in odd)))
        if why is None:
            return reply, emotion
        reply, emotion = await _ask(turn, [{"role": "assistant", "content": reply}, {"role": "system", "content": why}])
        # 第二遍：重说出来的整套再查一遍，不再重说
        if ooc_words(reply, text):
            logger.info(f"重说以后还是出戏，删掉出戏的句子：{reply[:40]}")
            reply = drop_ooc_sentences(reply, text)   # 删光了就不发
        still = story._fc.check(reply) if (fc_on and reply) else []
        if still:                                 # 还是混：删掉那几句；删光了就说记不清
            logger.info(f"重说以后还是记混（{'、'.join(f'{m.place}+{m.person}' for m in still)}），删掉这几句")
            for m in still:
                reply = reply.replace(m.sentence, "")
            reply = reply.strip() or "那段我记不太清了。"
        reply = _cut(turn, reply, context)
        odd = unprompted_hits(reply, context)
        if odd:
            logger.info(f"重说以后还是提到了没人说过的“{'、'.join(odd)}”，不再重说：{reply[:60]!r}")
    except Exception as e:  # noqa: BLE001
        # 出错时不在聊天里发任何东西；余额不足 / Key 失效私信管理员
        kind = classify_error(e)
        logger.error(f"DeepSeek 调用失败（{kind or '其他错误'}），本条不回复：{e}")
        _refund(stamp, gid_q, uid_q)
        if kind:
            await alert_admins(bot, kind)
        return None
    return reply, emotion


async def _finalize(turn: SimpleNamespace, reply: str, emotion: str | None) -> bool:
    """④ 定稿（在会话锁里）：[不回]、表情、语音，写进聊天记录。不发了返回 False"""
    event, key, is_group, target, text, name = turn.event, turn.key, turn.is_group, turn.target, turn.text, turn.name
    gid_q, uid_q, stamp, fam, history, new_entries = turn.gid_q, turn.uid_q, turn.stamp, turn.fam, turn.history, turn.new_entries
    use_sticker, sticker_allowed, voice_auto, voice_memo = turn.use_sticker, turn.sticker_allowed, turn.voice_auto, turn.voice_memo
    # 她觉得没必要接话：不发，只把对方的话记下来
    if _SKIP_RE.search(reply):
        rest = _SKIP_RE.sub("", reply).strip()
        if not rest:
            history.extend(new_entries)
            save_history(key)
            if not commands.in_test_mode(event.user_id):
                ltm.bump_talk(event.user_id, name)
            logger.info(f"她觉得没必要接话 user={event.user_id}：{text[:20]}")
            _refund(stamp, gid_q, uid_q)
            return False
        reply, emotion = rest, None       # 写了“[不回]”又写了话：以话为准

    # 表情：只有这轮抽中了才发；没抽中她却写了标记，就当没写
    sticker = None
    if emotion:
        if use_sticker:
            sticker = stickers.pick(emotion, set(_recent_stickers[target]), sticker_allowed)
            logger.info(f"表情：想发“{emotion}”" + (f" → {sticker['no']} 号（{'、'.join(sticker['tags'])}）" if sticker else "，库里没有合适的"))
        else:
            logger.info(f"表情：这轮没抽中，忽略她写的“{emotion}”")
    # 语音：她写了「[传声]」（只有对方要求、给了提示时才算数）/ 抽中了随机语音 / 只回一个语气词
    reply, wants_voice = split_voice_mark(reply)
    voice_payload, voice_label, voice_cost = None, "", 0.0
    voice_with_text = False        # VOICE_LANG=ja：念的是日语，语音后面再发中文文字
    if reply and tts_ready() and _voice_count[voice_day()] < cfg.voice_daily_max:
        vt, why = None, ""
        if wants_voice and voice_memo:
            vt, why = voice_text(reply, cfg.voice_text_max_chars), "对方要求"
        elif voice_auto and voice_chat.VOICE_REQUEST_RE.search(text):
            # 10/22：对方在要语音、她没写「[传声]」（推掉了），这条又刚好抽中：不念，免得用声音说“不给”
            logger.info("语音：抽中了，但对方在要语音、她没答应，这条只发文字")
        elif voice_auto:
            vt, why = voice_text(reply, cfg.voice_text_max_chars), "抽中"
        clip = voice_chat.interjections.match(reply) if "\n" not in reply else None
        if clip and vt is None and fam != "disliked" and random.random() < cfg.voice_interjection_prob:
            voice_payload, voice_label = clip.read_bytes(), f"原声 {clip.name}"
        elif vt:
            t0 = time.monotonic()
            ja = None
            try:
                if cfg.voice_lang == "ja":
                    ja = await voice_chat.translate_ja(vt, event.user_id, event.group_id if is_group else None)
                    if ja is None:
                        voice_payload, note = None, "没译成日语"
                    else:
                        voice_payload, note = await tts.speak(ja, timeout=cfg.voice_chat_timeout, lang="ja")
                else:
                    voice_payload, note = await tts.speak(vt, timeout=cfg.voice_chat_timeout)
            except Exception as e:  # noqa: BLE001  语音出什么错都不能让这条回复丢掉（10/20 13:04 线上就是这样丢的）
                voice_payload, note = None, f"出错：{e!r}"
            voice_cost = time.monotonic() - t0
            voice_with_text = bool(voice_payload) and ja is not None
            voice_label = f"合成（{why}，{note}" + (f"，日语：{ja[:40]}" if ja else "") + "）" if voice_payload else ""
            if voice_payload is None:
                logger.info(f"语音：没念成，改发文字（{why}）：{note}")
        if voice_payload:
            logger.info(f"语音：这条用语音发 → {voice_label}：{reply[:30]}")
    elif wants_voice:
        logger.info("语音：她写了「[传声]」，但现在发不了语音，改发文字")
    if not reply and not sticker:
        _refund(stamp, gid_q, uid_q)
        return False
    sticker_note = ""
    if sticker:
        sticker_note = f"[发了表情：{sticker['tags'][0]}" + (f"（{sticker['desc']}）" if sticker.get("desc") else "") + "]"
    record = "\n".join(x for x in (("[发了语音] " + reply) if voice_payload else reply, sticker_note) if x)

    history.extend(new_entries)
    history.append({"role": "assistant", "content": record, "ts": time.time()})
    save_history(key)
    tier_now = core.familiarity_of(event.user_id)     # 按说这句话时的关系算
    testing = commands.in_test_mode(event.user_id)    # 10/23 测试模式：照常回、照常进聊天记录，不碰长期记忆和好感
    if not testing:
        ltm.bump_talk(event.user_id, name)
    hit = None if testing else next((w for w in cfg.taboo_words if w in text), None)
    if hit:
        k = tier_value(cfg.taboo_tier_multiplier, tier_now, 1.0)
        if k > 0:
            ltm.taboo_penalty(event.user_id, cfg.taboo_penalty * k, cfg.taboo_daily_max * k,
                              f"说她{hit}（{ltm.TIER_NAMES.get(tier_now, tier_now)}）")

    turn.__dict__.update(reply=reply, record=record, sticker=sticker, sticker_note=sticker_note, tier_now=tier_now,
                         voice_payload=voice_payload, voice_cost=voice_cost, voice_with_text=voice_with_text)
    return True


async def _send(bot: Bot, turn: SimpleNamespace) -> None:
    """⑤ 发送（锁外）：打字延迟、分条发、语音、表情，发不出去的从记忆里拿掉、退回额度；发出去了交给长期记忆、限额告别、搭话"""
    event, key, ik, is_group, target, text, busy = turn.event, turn.key, turn.ik, turn.is_group, turn.target, turn.text, turn.busy
    gid_q, uid_q, stamp, admitted_at, seq_at_trigger = turn.gid_q, turn.uid_q, turn.stamp, turn.admitted_at, turn.seq_at_trigger
    new_entries, bread_first, tier_now = turn.new_entries, turn.bread_first, turn.tier_now
    reply, record, sticker, sticker_note = turn.reply, turn.record, turn.sticker, turn.sticker_note
    voice_payload, voice_cost, voice_with_text = turn.voice_payload, turn.voice_cost, turn.voice_with_text
    bubbles = split_bubbles(reply) if reply and (not voice_payload or voice_with_text) else []
    out_text = reply if voice_payload else "\n".join(bubbles)       # 真正说出去的话（语音就是念的那句）
    # 同一时间只给一个人打字：别人的回复要等这边发完
    async with _hands:
        # 排队期间已经说了“我要上路了”：这条就不发了（不然告别完还在说话），也从记忆里拿掉
        bye_at = max(_farewell_at.get("global", -1e9), _farewell_at.get(target, -1e9))
        if bye_at > admitted_at:
            logger.info(f"已经告别了，这条不发：{target} user={event.user_id}")
            _unrecord(key, record)
            return
        await switch_pause(target)
        if voice_payload:          # 发语音：不按打字算，按“录一段语音”算（合成已经花掉的时间扣掉）
            delay = max(0.5, min(voice_wav_seconds(voice_payload) + random.uniform(1.0, 2.0), cfg.reply_delay_cap) - voice_cost)
        else:
            delay = typing_delay(bubbles[0] if bubbles else "")
        if busy:
            delay += random.uniform(cfg.peak_extra_delay_min, cfg.peak_extra_delay_max)
        await asyncio.sleep(delay)

        sent = 0
        voice_first = False
        if voice_payload and voice_with_text:     # 日语语音先发，后面跟着中文文字
            try:
                await bot.send(event, voice.segment(voice_payload))   # 语音消息带不了引用
                sent += 1
                _voice_count[voice_day()] += 1
                voice_first = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"语音：发送失败，只发文字：{e}")
                keep = "\n".join(x for x in (reply, sticker_note) if x)
                _unrecord(key, record, keep=keep)
                record, voice_payload = keep, None
            if voice_first and bubbles:
                await asyncio.sleep(bubble_gap(bubbles[0]))
                _count_sent(gid_q, uid_q)
        nb = 0                                    # 文字发出去了几条
        for i, bubble in enumerate(bubbles):
            if i > 0:
                await asyncio.sleep(bubble_gap(bubble))            # 后面几条：像在接着打字
                _count_sent(gid_q, uid_q)                          # 多发的每条都算进限额
            msg = bubble
            if i == 0 and not voice_first and _chat_seq[target] > seq_at_trigger:
                # 这条消息之后有别人说话了、或者她自己先说了别的（包括她排队的时候）：第一条引用原消息，免得大家不知道她在回哪句
                msg = MessageSegment.reply(event.message_id) + bubble
            try:
                await bot.send(event, msg)
                sent += 1
                nb += 1
            except Exception as e:  # noqa: BLE001
                # 发不出去（被风控拦了、掉线了）：后面几条也别发了
                logger.warning(f"回复没发出去（第 {i + 1} 条，共 {len(bubbles)} 条）：{target} user={event.user_id}：{e}")
                break
        if bubbles and not sent:
            # 一条都没发出去：从短期记忆里拿掉，也不交给长期记忆
            _unrecord(key, record)
            _refund(stamp, gid_q, uid_q)
            return
        if nb < len(bubbles):
            sticker = None                                           # 文字没发全，表情也不发了
            if not voice_first:                                      # 语音已经发出去了：这句话算说过了，记录不动
                _unrecord(key, record, keep="\n".join(bubbles[:nb]))
        if voice_payload and not voice_with_text:
            try:
                await bot.send(event, voice.segment(voice_payload))   # 语音消息带不了引用
                sent += 1
                _voice_count[voice_day()] += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"语音：发送失败，改发文字：{e}")
                try:
                    await bot.send(event, reply)
                    sent += 1
                    _unrecord(key, record, keep="\n".join(x for x in (reply, sticker_note) if x))
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"回复没发出去：{target} user={event.user_id}：{e2}")
                    _unrecord(key, record)
                    _refund(stamp, gid_q, uid_q)
                    return
        if sticker:
            # 文字后面隔一两秒再甩表情；只发表情时，中间有人插话就引用原消息
            if bubbles or voice_payload:
                await asyncio.sleep(random.uniform(cfg.bubble_gap_min, cfg.bubble_gap_max))
                _count_sent(gid_q, uid_q)
            try:
                seg = stickers.segment(sticker, cfg.sticker_sub_type)
                if not bubbles and not voice_payload and _chat_seq[target] > seq_at_trigger:
                    seg = MessageSegment.reply(event.message_id) + seg
                await bot.send(event, seg)
                _last_sticker[target] = time.monotonic()
                _recent_stickers[target].append(sticker["file"])
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"表情：发送失败（{sticker['no']} 号）：{e}")
                if not bubbles and not voice_payload:
                    _unrecord(key, record)
                    _refund(stamp, gid_q, uid_q)
                    return
                # 文字发出去了、表情没发出去：聊天记录里只留文字，免得她以为自己甩过这张
                _unrecord(key, record, keep=("[发了语音] " + reply) if voice_payload else reply)
        # 真的发出去了，才收下面包、交给长期记忆（攒够一批就在后台整理档案、评估好感）
        testing = commands.in_test_mode(event.user_id)
        if bread_first and not testing:               # 今天第一次送面包：收下（讨厌的人送的不加分）
            ltm.take_bread(event.user_id, 0 if tier_now == "disliked" else cfg.gift_bread_affection)
        # 长期记忆不需要知道她甩了哪张画像；只发了表情的，给一个简短的说法
        said = reply if voice_first else ("\n".join(bubbles[:nb]) if bubbles else (reply if voice_payload else ""))
        if not testing:
            ltm.add_pending(key, new_entries + [{"role": "assistant", "content": said or f"（甩了一张{sticker['tags'][0]}的表情）"}])
        else:
            # 10/23 测试模式：管理员这一轮和她的回复不交给长期记忆（不整理、不打好感分、不进今日见闻）；
            # 群里一起带进来的旁听记录（别人说的话）照常交
            others = [e for e in new_entries if e.get("uid") != event.user_id and "speakers" in e]
            if others:
                ltm.add_pending(key, others)
            logger.info(f"测试模式：{event.user_id} 这一轮不交给长期记忆")
        mark_sent(target)
        note_sent_round(ik)
        # 她这一轮说了要走：先记下“不在了”，再记“这一轮结束了”，排在后面等着的那条就不会再回（10/23）
        leaving = note_leaving(target, out_text)
        _reply_done[ik] = time.monotonic()
        _active_chats[target] = {"at": time.monotonic(), "group_id": event.group_id if is_group else None,
                                 "user_id": event.user_id}
        # 这一小时的限额用完了：补一句告别，让大家知道她接下来一段时间不会回（同一范围一小时只说一次）
        # 全局限额用完：最近在聊的群和私聊都告别一声；只是这个群的限额用完：只在这个群告别
        if cfg.farewell_on_limit and quota_left(gid_q, uid_q) <= 0:
            # 刚才这轮已经道别了（私聊里对方说了再见、她也回了再见；或者她自己说了要走）：这里就不再补一句告别
            said = out_text
            already_bye = bool(_LEAVE_RE.search(said)) or (not is_group and bool(_BYE_RE.search(text) or _BYE_RE.search(said)))
            # 话还没说完（她最后在反问对方、或者对方又发来了还没回的消息）：先不告别，等收个尾
            open_end = not already_bye and (she_asked([{"role": "assistant", "content": said}]) or bool(_inbox.get(ik)))
            w = _wrapup.get(target)
            if open_end and (w is None or (w["user"] == event.user_id and w["left"] > 0
                                           and time.time() - w["at"] <= WRAPUP_MINUTES * 60)):
                if w is None:
                    _wrapup[target] = {"user": event.user_id, "left": WRAPUP_EXTRA, "at": time.time()}
                logger.info(f"额度用完了，但和 {event.user_id} 的话还没说完，先不告别，收个尾再走：{target}")
            else:
                _wrapup.pop(target, None)
                farewell = await say_farewells(bot, event, target, already_bye=already_bye)
        mark_sent(target)
    if out_text and cfg.knowledge_enabled:
        note_topic(key, out_text)                    # 她讲了哪段经历：对方接着追问时还记得
    if leaving:                          # 她自己说了要走：接下来一阵子真的不在
        _nudge_due.pop(event.user_id, None)
    elif not is_group:
        arm_nudge(event.user_id, text)            # 私聊：对方一阵子没回的话，她可能自己再说一句
    if is_group:
        _last_bot_msg[event.group_id] = (time.monotonic(), event.user_id, record)
        # 刚回完这个人：从她最后一条发出算起，这个人接着说的话直接算在跟她说
        _engaged[(event.group_id, event.user_id)] = time.monotonic()
        _replied_at[(event.group_id, event.user_id)] = time.monotonic()
