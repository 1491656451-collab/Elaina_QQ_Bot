"""补回未读私聊，以及上线 / 断线时要做的事（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
不在线时别人发的私聊上线后补回、在线心跳、被踢下线和回来、上线时启动表情循环和掉线提醒。
只改了一处写法：调 send_offline_alert() 写成 online.send_offline_alert()，测试替换它才生效。
它要用到回消息主流程（converse），所以在 __init__.py 最后导入"""
import asyncio
import json
import random
import time
from collections import deque

from nonebot import get_driver, logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment, NoticeEvent, PrivateMessageEvent
from nonebot.rule import Rule

from . import online, stickers_chat
from .core import BOT_DIR, cfg, HISTORY_DIR
from .history import get_history
from .textin import message_to_text
from .online import _is_offline_event, _read_offline, note_offline, quiet_left, start_quiet
from .friends import is_friend_verify
from .commands import _SLASH_STARTS
from .addressed import _allowed, note_arrival
from . import (  # noqa: E402  这些还在 __init__.py 里（回消息主流程等），所以这个文件放在 __init__.py 最后导入
    converse,
)


_offline_alert_task: "asyncio.Task | None" = None


kicked_notice = on_notice(rule=_is_offline_event, priority=1, block=False)


@kicked_notice.handle()
async def _(event: NoticeEvent):
    d = event.model_dump()
    reason = str(d.get("message") or d.get("tag") or "没说原因")[:60]
    note_offline(True, reason)
    logger.warning(f"账号被下线：{reason}")
    # NapCat 有时不断开连接、自己重新登录：心跳要在这里就停，不然离线这段时间会被心跳盖掉，私聊补不回来
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
    if cfg.catchup_enabled:
        try:
            _save_online()          # 心跳停在下线这一刻
        except Exception:  # noqa: BLE001
            pass


# 被踢后 NapCat 有时不断开连接、自己重新登录：又收到消息就说明回来了，这时补发提醒
async def _back_after_kick(event: MessageEvent) -> bool:
    return bool(cfg.offline_alert and _read_offline().get("kicked")) and (_offline_alert_task is None or _offline_alert_task.done())


back_after_kick = on_message(rule=_back_after_kick, priority=0, block=False)


@back_after_kick.handle()
async def _(bot: Bot):
    global _offline_alert_task
    start_quiet()
    _offline_alert_task = asyncio.create_task(online.send_offline_alert(bot, delay=5))
    # 没断开连接就回来了：不会触发“连上”，这里补做一次“补回未读、再开始记心跳”
    if _heartbeat_task is None and (_catchup_task is None or _catchup_task.done()):
        start_catchup(bot)


# 表情库的后台循环（拉收藏、打标签，在 stickers_chat.py）：上线时开、断线时停，句柄放在这里（10/22 拆分第 6 步）
_sticker_task: "asyncio.Task | None" = None


# 在线时每分钟记一次“心跳”；重新连上时，把心跳之后别人发来、她还没回的私聊找出来，一个一个补回
ONLINE_FILE = BOT_DIR / "data" / "online_state.json"


_seen_ids: deque = deque(maxlen=300)      # 在线时收到过的私聊消息 id（补回时跳过）


_heartbeat_task: "asyncio.Task | None" = None


_catchup_task: "asyncio.Task | None" = None


def _load_online() -> dict:
    try:
        return json.loads(ONLINE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_online() -> None:
    ONLINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ONLINE_FILE.write_text(json.dumps({"heartbeat": time.time(), "seen": list(_seen_ids)}), encoding="utf-8")


async def _is_private(event: MessageEvent) -> bool:
    return isinstance(event, PrivateMessageEvent)


seen_private = on_message(rule=Rule(_is_private), priority=1, block=False)


@seen_private.handle()
async def _(event: PrivateMessageEvent):
    note_arrival(f"private_{event.user_id}", event.message_id)
    if quiet_left() > 0:        # 安静期间收到的私聊不算“看过”，安静完了会补回
        return
    _seen_ids.append(int(event.message_id))


def _seg_list(raw) -> Message:
    if isinstance(raw, str):
        return Message(raw)
    return Message([MessageSegment(type=s.get("type", "text"), data=s.get("data") or {}) for s in raw or []])


def catchup_event(bot_id: int, user_id: int, msgs: list[dict]) -> PrivateMessageEvent:
    """把几条未读私聊合成一条消息事件（中间用换行隔开）"""
    merged = Message()
    for i, m in enumerate(msgs):
        if i:
            merged.append(MessageSegment.text("\n"))
        merged.extend(_seg_list(m.get("message")))
    last = msgs[-1]
    sender = last.get("sender") or {}
    return PrivateMessageEvent(
        time=int(last.get("time", time.time())), self_id=bot_id, post_type="message", sub_type="friend",
        user_id=user_id, message_type="private", message_id=int(last.get("message_id", 0)),
        message=merged, original_message=merged, raw_message=str(merged), font=0,
        sender={"user_id": user_id, "nickname": sender.get("nickname") or str(user_id)}, to_me=True,
    )


async def find_unread(bot: Bot, offline_since: float, seen: set[int]) -> list[tuple[int, list[dict]]]:
    """找出 offline_since 之后别人发来、还没回过的私聊。返回 [(QQ, [消息...])]，最早的人排前面"""
    now = time.time()
    oldest_ok = max(offline_since - 60, now - cfg.catchup_max_age_hours * 3600)
    try:
        friends = {int(f["user_id"]) for f in await bot.get_friend_list()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"获取好友列表失败，不补回未读：{e}")
        return []
    # 最近联系人里，最后一条消息在离线之后的私聊（拿不到就退回：所有聊过天的好友）
    try:
        recent = await bot.call_api("get_recent_contact", count=30)
        people = [int(c["peerUin"]) for c in recent or []
                  if int(c.get("chatType", 0)) == 1 and float(c.get("msgTime") or 0) >= oldest_ok]
    except Exception as e:  # noqa: BLE001
        logger.info(f"拿不到最近联系人（{e}），改为检查聊过天的好友")
        people = [int(f.stem.split("_")[1]) for f in HISTORY_DIR.glob("private_*.json") if f.stem.split("_")[1].isdigit()]
    out = []
    for qq in dict.fromkeys(people):
        if qq not in friends or qq == int(bot.self_id):
            continue
        if cfg.private_whitelist and qq not in cfg.private_whitelist:
            continue
        try:
            data = await bot.call_api("get_friend_msg_history", user_id=qq, count=20)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"获取 {qq} 的聊天记录失败：{e}")
            continue
        msgs = sorted((data or {}).get("messages", []) if isinstance(data, dict) else data or [], key=lambda m: m.get("time", 0))
        # 只看她最后一次说话之后、对方发的消息
        mine = [m for m in msgs if int(m.get("user_id", 0)) == int(bot.self_id)]
        after = mine[-1]["time"] if mine else 0
        last_hist = next((h["ts"] for h in reversed(get_history(f"private_{qq}")) if h.get("ts") and h["role"] == "user"), 0)
        unread = [m for m in msgs
                  if int(m.get("user_id", 0)) == qq
                  and m.get("time", 0) > after and m.get("time", 0) >= oldest_ok and m.get("time", 0) > last_hist
                  and int(m.get("message_id", 0)) not in seen]
        # 加好友的验证消息、“已成功添加为好友”这种系统提示不补
        unread = [m for m in unread if not is_friend_verify(qq, message_to_text(_seg_list(m.get("message"))), m.get("time", 0))]
        # 纯指令（/重置 之类）不补
        unread = [m for m in unread if not message_to_text(_seg_list(m.get("message"))).strip()
                  .startswith(tuple(set(_SLASH_STARTS) | {c for c in (bot.config.command_start or ()) if c}))]
        if unread:
            out.append((qq, unread[-8:]))    # 每人最多看最近 8 条
    out.sort(key=lambda x: x[1][0].get("time", 0))
    return out[: cfg.catchup_max_people]


async def catch_up(bot: Bot) -> int:
    """上线后补回未读私聊，返回补回了几个人"""
    state = _load_online()
    since = state.get("heartbeat")
    if not (cfg.catchup_enabled and cfg.enable_private and cfg.deepseek_api_key) or not since:
        return 0
    seen = set(state.get("seen", [])) | set(_seen_ids)
    todo = await find_unread(bot, float(since), seen)
    if not todo:
        return 0
    logger.info(f"上线后发现 {len(todo)} 个人有未读私聊，准备补回")
    done = 0
    await asyncio.sleep(random.uniform(5, 20))                 # 刚“上线”，先缓一缓
    for i, (qq, msgs) in enumerate(todo):
        if i:
            await asyncio.sleep(random.uniform(cfg.catchup_gap_min, cfg.catchup_gap_max))
        ev = catchup_event(int(bot.self_id), qq, msgs)
        if not _allowed(ev):
            continue
        try:
            await converse(bot, ev, catchup_age=time.time() - msgs[0].get("time", time.time()))
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"补回 {qq} 的未读失败：{e}")
        for m in msgs:
            _seen_ids.append(int(m.get("message_id", 0)))
    _save_online()
    return done


async def _heartbeat_loop() -> None:
    while True:
        try:
            _save_online()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"记录在线状态失败：{e}")
        await asyncio.sleep(60)


def start_catchup(bot: Bot) -> None:
    """（安静期过了之后）补回未读私聊，补完再开始记心跳（免得心跳把离线时间盖掉）"""
    global _catchup_task
    if not cfg.catchup_enabled:
        return

    async def run():
        if quiet_left() > 0:
            await asyncio.sleep(quiet_left())
        try:
            n = await catch_up(bot)
            if n:
                logger.info(f"已补回 {n} 个人的未读私聊")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"补回未读出错：{e}")
        global _heartbeat_task
        if _heartbeat_task is None or _heartbeat_task.done():
            _heartbeat_task = asyncio.create_task(_heartbeat_loop())

    _catchup_task = asyncio.create_task(run())


@get_driver().on_bot_connect
async def _on_connect(bot: Bot):
    global _sticker_task, _offline_alert_task
    if _read_offline().get("kicked"):      # 上次是被踢下线的：先安静一会儿
        start_quiet()
    if cfg.offline_alert and (_offline_alert_task is None or _offline_alert_task.done()):
        _offline_alert_task = asyncio.create_task(online.send_offline_alert(bot))
    if cfg.sticker_enabled:
        if _sticker_task:
            _sticker_task.cancel()
        _sticker_task = asyncio.create_task(stickers_chat._sticker_loop(bot))
    start_catchup(bot)


@get_driver().on_bot_disconnect
async def _(bot: Bot):
    global _heartbeat_task, _sticker_task
    if cfg.offline_alert:
        note_offline(False, "连接断开")
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
    if _sticker_task:
        _sticker_task.cancel()
        _sticker_task = None
    if cfg.catchup_enabled:
        try:
            _save_online()
        except Exception:  # noqa: BLE001
            pass
