"""判断要不要回、是不是在跟她说话（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
触发规则、消息计数（要不要引用原句）、没 @ 也回复的判断、看到自己画像时的反应；她说要走了以后一阵子不回（10/22 第 7 步搬来）。
群里旁听的处理（passive）要叫插话，暂时还在 __init__.py，第 7 步再定"""
import random
import re
import time
from collections import defaultdict, deque

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.rule import Rule

from . import budget, core, sleep, vision
from .core import _arrival_seq, _chat_seq, cfg, client, ltm
from .prompts import JUDGE_PROMPT
from .history import _passive, get_history
from .textin import clean_body, group_body, is_bot_like, message_to_text, middle_at_me, pull_own_lead, sender_label, sender_name, speaker_head, talking_to_others
from .quota import _sleep_wrapup, in_wrapup, quota_left
from .online import quiet_left
from .friends import is_friend_verify
from .commands import is_slash_message


def _allowed(event: MessageEvent) -> bool:
    if isinstance(event, GroupMessageEvent):
        if not cfg.enable_group:
            return False
        return not cfg.group_whitelist or event.group_id in cfg.group_whitelist
    if isinstance(event, PrivateMessageEvent):
        if not cfg.enable_private:
            return False
        if cfg.private_friends_only and event.sub_type != "friend":
            return False
        return not cfg.private_whitelist or event.user_id in cfg.private_whitelist
    return False


allowed = Rule(_allowed)


async def _is_group_not_to_me(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent) and not event.is_tome()


def note_arrival(target: str, message_id: int) -> None:
    _chat_seq[target] += 1
    _arrival_seq[(target, int(message_id))] = _chat_seq[target]
    if len(_arrival_seq) > 2000:                    # 只留最近的
        for k in list(_arrival_seq)[:1000]:
            del _arrival_seq[k]


async def _is_group(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


counter = on_message(rule=Rule(_is_group), priority=1, block=False)


_speakers: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))   # 群号 -> 最近说话的人（时间, QQ）


_replied_at: dict[tuple[int, int], float] = {}                        # (群号, QQ) -> 她最后一次回这个人的时间


def others_spoke_since(gid: int, uid: int, since: float) -> bool:
    return any(t > since and u != uid for t, u in _speakers.get(gid, []))


@counter.handle()
async def _(event: GroupMessageEvent):
    note_arrival(f"group_{event.group_id}", event.message_id)
    _speakers[event.group_id].append((time.monotonic(), event.user_id))
    ltm.note_name(event.group_id, event.user_id, sender_label(event))


_last_bot_msg: dict[int, tuple[float, int, str]] = {}   # 群号 -> (时间, 她回复的对象 QQ, 她说的话)


_last_smart: dict[int, float] = {}


_engaged: dict[tuple[int, int], float] = {}   # (群号, QQ) -> 这个人最近一次在跟她说话的时间


_Q_TAILS = ("吗", "呢", "是吧", "对吧", "不是吧", "什么", "怎么", "为啥", "干嘛", "干啥", "咋样", "咋办")


def _looks_like_question(text: str) -> bool:
    # 9/28：结尾“吧”“嘛”不再一律算问句（“好吧”“行吧”“算了吧”是应一声，不是在问）；只认“是吧”“对吧”这种
    t = text.rstrip(" ~～。.!！…")
    return any(c in text for c in "?？") or t.endswith(_Q_TAILS)


def _as_third_person(text: str) -> str:
    """判断时站在旁观者角度：记录里的“→ 你”写成“→ 伊蕾娜”"""
    return re.sub(r"(→ (?:[^】]*、)?)你(?=[、】])", r"\1伊蕾娜", text)


async def _judge(event: GroupMessageEvent, line: str) -> bool:
    last = _last_bot_msg.get(event.group_id)
    # 最近的群聊：她自己的记忆里最近几条（含她说的话）+ 还没进记忆的旁听
    lines = []
    for h in get_history(f"group_{event.group_id}")[-4:]:
        c = h["content"].replace("（群聊旁听记录）\n", "")
        lines.append(f"【伊蕾娜】{c}" if h["role"] == "assistant" else _as_third_person(c))
    lines += [_as_third_person(ln) for _, _, ln, _ in list(_passive.get(event.group_id, []))[-4:]]
    recent = "\n".join(x[:120] for x in lines[-6:]) or "（无）"
    prompt = JUDGE_PROMPT.format(last=last[2][:100] if last else "（最近没说话）", recent=recent, line=line[:220])
    try:
        r = await core.judge_client.chat.completions.create(       # 10/28 起默认用 pro（core.pick_judge）
            model=core.judge_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2,
            extra_body={"thinking": {"type": "disabled"}},
        )
        budget.track(r, "judge", user=event.user_id, group=event.group_id, model=core.judge_model)
        ok = (r.choices[0].message.content or "").strip().startswith("是")
        if not ok:
            logger.info(f"判断：不是在跟她说话 群{event.group_id} {line[:40]}")
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning(f"判断是否在跟她说话失败，按“否”处理：{e}")
        return False


async def _addressed(bot: Bot, event: MessageEvent) -> bool:
    """@ 她 / 回复她 / 以昵称开头 → 一定回；否则在群里看是否明显在跟她说话"""
    now = time.monotonic()
    # 以“/”开头的消息（给别的机器人的指令、不是管理员的人发的指令、管理员打错的指令……）：不回，也不当成聊天
    if is_slash_message(event, bot.config.command_start):
        logger.info(f"“/”开头的消息，不理 user={event.user_id}：{message_to_text(event.get_message())[:20]}")
        return False
    if is_bot_like(event):                 # 别的机器人（Markdown、卡片消息）：不理，免得两个机器人对着聊
        return False
    if isinstance(event, PrivateMessageEvent) and is_friend_verify(event.user_id, message_to_text(event.get_message()), event.time):
        logger.info(f"加好友的验证消息 / 系统提示，不回 user={event.user_id}：{message_to_text(event.get_message())[:30]}")
        return False
    if quiet_left() > 0:                   # 刚被踢下线又上线：先安静一会儿（私聊之后会补回）
        if event.is_tome():
            logger.info(f"刚重新上线，还要安静 {quiet_left() / 60:.1f} 分钟，先不回 user={event.user_id}")
        return False
    if event.is_tome():
        if isinstance(event, GroupMessageEvent):
            _engaged[(event.group_id, event.user_id)] = now
        return True
    if not (cfg.smart_reply and isinstance(event, GroupMessageEvent)) or not _allowed(event):
        return False
    text = message_to_text(event.get_message())
    if not text or text.startswith(tuple(bot.config.command_start or {"/"})):
        return False
    # 群里没 @ 她的纯图片 / 纯表情：不当成在跟她说话（不回复，只进旁听，显示为 [图片]）。
    # 例外：本机角色识别认出图里画的是她，她可能看一眼、说一句
    if not re.sub(r"\[(图片|语音|视频|表情[^\]]*)\]|\s", "", text):
        return await _self_image_react(event)
    gid = event.group_id
    names = [n for n in list(cfg.smart_names) + list(bot.config.nickname or []) if n]
    plain = event.get_plaintext()          # 只看文字：@别人的名字、回复的原文都不算
    mentioned = any(n.lower() in plain.lower() for n in names)   # 不区分大小写：ELAINA 也算
    mentioned = mentioned or middle_at_me(event)                   # 句子中间 @ 了她也算点了她的名
    # “因为伊蕾娜小姐觉得没必要回这个消息 / 对吧”：只有“对吧”这种、而同一个人紧挨着的上一句点了她的名，也算在问她（9/30 22:55）
    lead: list[str] = []
    if isinstance(event, GroupMessageEvent):
        lead = pull_own_lead(list(_passive.get(event.group_id, [])), event.user_id, plain, time.time())[0]
        mentioned = mentioned or any(n.lower() in x.lower() for x in lead for n in names)
    # @ 了别人、或者回复的是别人的消息：在跟那个人说话，她不接（除非文字里点了她的名）
    other = talking_to_others(event)
    if other and not mentioned:
        logger.info(f"在跟别人说话（{other}），不接：群{gid} {sender_name(event)}：{text[:30]}")
        return False
    # 连续发言：她刚回过的人接着说。叫了她名字，或者这期间没别人插话（一对一在聊），才直接算在跟她说；
    # 这期间有别人说过话时，就算带“你”、是问句，也可能是对别人说的，交给下面判断（9/28 收紧）
    in_followup = now - _engaged.get((gid, event.user_id), -1e9) <= cfg.followup_window
    if in_followup:
        since = _replied_at.get((gid, event.user_id), _engaged.get((gid, event.user_id), now))
        if mentioned or not others_spoke_since(gid, event.user_id, since):
            _engaged[(gid, event.user_id)] = now
            logger.info(f"接着说，直接回：群{gid} {sender_name(event)}：{text[:30]}")
            return True
    if len(text) < 2 or core.in_peak():       # 高峰时段她很忙，不主动接话（也省掉判断的 token）
        return False
    # 10/27：她回完这个人以后，群里没别人说话，这个人隔一两分钟才接着说（打字慢）：不直接放过，交给判断。
    # 10/26 14:16 “这时候就不想着省钱嘛”隔了 100 秒，没“你”、不是问句，以前在下面直接当成不是跟她说的
    quiet_followup = False
    if not in_followup and cfg.followup_judge_window > 0:
        replied = _replied_at.get((gid, event.user_id))
        if replied is not None and now - replied <= cfg.followup_judge_window and not others_spoke_since(gid, event.user_id, replied):
            quiet_followup = True
    last = _last_bot_msg.get(gid)
    recent_talk = bool(last) and now - last[0] <= cfg.smart_window
    continuing = in_followup or quiet_followup or (recent_talk and (event.user_id == last[1] or "你" in plain or _looks_like_question(plain)))
    # 正在跟她聊的人（她刚回过的人，或者接着用“你”跟她说话）：不受“没 @ 也回复”间隔的限制
    in_conversation = in_followup or quiet_followup or (recent_talk and (event.user_id == last[1] or "你" in plain))
    if not in_conversation and now - _last_smart.get(gid, -1e9) < cfg.smart_min_interval:
        if mentioned or continuing:
            logger.info(f"没 @ 的消息，离上次插话不到 {cfg.smart_min_interval:g} 秒，先不接：群{gid} {sender_name(event)}：{text[:30]}")
        return False
    if not (mentioned or continuing):
        return False                     # 大部分群消息在这里就结束了，不花 token
    line = speaker_head(event, you="伊蕾娜") + clean_body("\n".join(lead + [message_to_text(group_body(event))]))
    if sleep.asleep():
        _sleep_wrapup(f"group_{gid}", event.user_id)     # 刚到休息时间、正在跟她聊的人接着说：先收个尾
    if cfg.smart_judge and quota_left(gid, event.user_id) <= 0 and not in_wrapup(f"group_{gid}", event.user_id):
        return False                     # 今天的钱花完了（或这个群、这个人的份额用完了）：反正不会回，也不用判断
    if quiet_followup and not (mentioned or recent_talk):
        logger.info(f"刚回过这个人、期间没别人说话，隔了 {now - _replied_at[(gid, event.user_id)]:.0f} 秒接着说，交给判断：群{gid} {sender_name(event)}：{text[:30]}")
    ok = await _judge(event, line) if cfg.smart_judge else mentioned
    if ok:
        _last_smart[gid] = now
        _engaged[(gid, event.user_id)] = now
        logger.info(f"没 @ 也回复：群{gid} {sender_name(event)}：{text[:30]}")
    return ok


_last_self_react: dict[int, float] = {}


_self_react_seen: dict[str, float] = {}   # 图片 -> 上次为它搭话的时间（同一张表情包一天只搭一次）


async def _self_image_react(event: GroupMessageEvent) -> bool:
    """群里有人发了伊蕾娜的画像（没 @ 她）：本机识别，不花 token；按概率和间隔决定要不要搭话"""
    if not (cfg.vision_enabled and cfg.vision_self_react and vision.tagging) or core.in_peak():
        return False
    gid, now = event.group_id, time.monotonic()
    if now - _last_self_react.get(gid, -1e9) < cfg.vision_self_react_interval:
        return False
    if now - _last_smart.get(gid, -1e9) < cfg.smart_min_interval:
        return False
    images = [seg.data for seg in event.get_message() if seg.type == "image"][: cfg.vision_max_images]
    for data in images:
        key = vision.cache_key(data)
        if now - _self_react_seen.get(key, -1e9) < 86400:
            continue
        if await vision.shows_self(data):
            _self_react_seen[key] = now
            if random.random() >= cfg.vision_self_react_prob:
                logger.info(f"群{gid} {sender_name(event)} 发了伊蕾娜的图，这次没搭话")
                return False
            _last_self_react[gid] = _last_smart[gid] = now
            logger.info(f"群{gid} {sender_name(event)} 发了伊蕾娜的图，看一眼搭句话")
            return True
    return False


# 她自己说了“我要去赶路了”“先走了”：接下来 20～60 分钟真的不在这个群 / 私聊（不然说完要走还接着聊，很怪）
_LEAVE_RE = re.compile(r"(我?(要|得|该|先)(去)?赶路了|赶路去了|先走了|先失陪|我?得走了|我走了|该走了|有缘再见|下次再聊|先不聊了|我先撤|"
                       r"我?(先)?去睡了|我先睡了|先下了|改天再聊)")


_away: dict[str, dict] = {}              # 会话 -> {"until": 回来的时间, "said": 说要走的时间, "line": 那句话}


def away_left(target: str) -> float:
    """她说过要走、还要几秒才回来（0 = 在）"""
    a = _away.get(target)
    return max(0.0, a["until"] - time.time()) if a else 0.0


def note_leaving(target: str, said: str) -> bool:
    """她这轮说的话里有“要走了”：记下来，接下来一阵子不在"""
    if cfg.leave_minutes_max <= 0 or not said or not _LEAVE_RE.search(said):
        return False
    lo, hi = sorted((cfg.leave_minutes_min, cfg.leave_minutes_max))
    now = time.time()
    _away[target] = {"until": now + random.uniform(lo, hi) * 60, "said": now, "line": said.split("\n")[-1]}
    logger.info(f"她说要走了（{said[-20:]}）：{target} 接下来 {(_away[target]['until'] - now) / 60:.0f} 分钟不回")
    return True
