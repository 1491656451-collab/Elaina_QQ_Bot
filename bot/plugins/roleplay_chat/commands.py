"""指令的公共部分和聊天自己的指令（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
哪些是管理员指令、“/”开头的消息不理、/重置、/重载人设。各功能的指令在各自的文件里。
10/23：管理员在群里用指令时，回的纯文字说明改成私聊发给他（见 admin_note_to_private）"""
import time

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.consts import CMD_KEY, PREFIX_KEY
from nonebot.internal.matcher import current_matcher
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from .core import HISTORY_DIR
from .prompts import load_persona
from .history import _histories, _passive, _recent_chat, clear_history, session_key
from .textin import edge_at_positions, seg_text
from .memory_chat import _parse_target


# 所有指令都只有管理员能用；别人发指令她当没看见（不回、不当成聊天）
ADMIN_COMMANDS = ("测试", "重置", "清空记忆", "reset", "重载人设", "认图", "表情", "表情包", "记忆", "查看记忆",
                  "好感", "好感度", "性别", "忘记", "删除记忆", "改记忆", "写信", "说说", "插话", "冒泡", "搭话", "花费", "花销", "语音")


# 10/23（《测试指令会不会留下错误记忆》4.1，用户定的）：管理员在群里用指令时，指令回的“（……）”说明群友都看得见，很出戏，
# 档案、花费这些也不该给群友看。这里统一拦：正在处理指令（命令匹配上了）、事件是群消息、发指令的是管理员、
# 要发的是纯文字时，改成私聊发给他；带语音、图片、表情的照常发在群里。
# 只拦 bot.send（指令的 finish / send 都走它）：她真正说出去的话不走这里——/插话、/冒泡用 send_group_msg，/写信、/搭话本来就是私聊，
# 她正常聊天时也用 bot.send，但那时匹配上的不是指令。私聊发不出去（不是好友等）就照旧发在群里。
# 注：一开始挂在“每次调接口”的钩子上，会给所有接口调用多加一次等待，补回未读时几个请求的先后被打乱（测试 test_catch_up_unread），改成只包 bot.send
async def admin_note_to_private(bot: Bot, event, message):
    """该改成私聊的就私聊发给管理员并返回接口结果；不该改的返回 None"""
    m = current_matcher.get(None)
    if not isinstance(event, GroupMessageEvent) or m is None:
        return None
    if not (m.state.get(PREFIX_KEY) or {}).get(CMD_KEY):
        return None
    if str(event.user_id) not in {str(x) for x in (getattr(bot.config, "superusers", None) or ())}:
        return None
    msg = message if isinstance(message, Message) else Message(message)
    if not msg or any(seg.type != "text" for seg in msg):
        return None
    try:
        res = await bot.call_api("send_private_msg", user_id=event.user_id, message=msg)
    except Exception as e:  # noqa: BLE001
        logger.info(f"群里指令的说明私聊给管理员没发出去，照旧发在群里：{e}")
        return None
    logger.info(f"群{event.group_id} 里指令的说明改成私聊发给 {event.user_id}：{str(msg)[:40]}")
    return res if res is not None else {}


# 10/23 测试模式（《测试指令会不会留下错误记忆》第 5 节第 1 条，用户定的）：/测试 开 以后，管理员本人（私聊、群聊都算）
# 和她的聊天照常回、照常进聊天记录（短期记忆），但不交给长期记忆整理（不打好感分、不进今日见闻）、不加减好感（面包、雷点）、
# 不记性别说法、不更新“上次聊天的时间”。只管发指令的这个人，不影响别人。关掉时不清聊天记录（要清自己发 /重置）。
# 不存盘：机器人重启就自动关掉；开着超过 TEST_MODE_HOURS 小时也自动关，免得忘了关、之后的正常聊天都没记住
TEST_MODE_HOURS = 6
_test_mode: dict[int, float] = {}        # QQ -> 打开的时间


def in_test_mode(qq: int) -> bool:
    at = _test_mode.get(qq)
    if at is None:
        return False
    if time.time() - at > TEST_MODE_HOURS * 3600:
        _test_mode.pop(qq, None)
        logger.info(f"测试模式：{qq} 开着超过 {TEST_MODE_HOURS} 小时，自动关掉了")
        return False
    return True


test_cmd = on_command("测试", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@test_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    op = arg.extract_plain_text().strip()
    qq = event.user_id
    if op == "开":
        _test_mode[qq] = time.time()
        logger.info(f"测试模式：{qq} 打开")
        await test_cmd.finish(f"（测试模式开了：你和她的聊天照常回、照常进聊天记录，但不交给长期记忆、不加减好感、不进今日见闻。"
                              f"{TEST_MODE_HOURS} 小时后或者机器人重启会自动关；测完发 /测试 关）")
    if op == "关":
        was = _test_mode.pop(qq, None) is not None
        logger.info(f"测试模式：{qq} 关掉")
        await test_cmd.finish("（测试模式关了。测试时的聊天记录还在，想清掉发 /重置）" if was else "（测试模式本来就没开）")
    left = TEST_MODE_HOURS * 3600 - (time.time() - _test_mode[qq]) if in_test_mode(qq) else 0
    await test_cmd.finish((f"（测试模式开着，还有 {left / 60:.0f} 分钟自动关）" if left else "（测试模式没开）")
                          + "\n用法：/测试 开、/测试 关")


_orig_bot_send = Bot.send


async def _bot_send(self, event, message, **kwargs):
    res = await admin_note_to_private(self, event, message)
    if res is not None:
        return res
    return await _orig_bot_send(self, event, message, **kwargs)


Bot.send = _bot_send


_SLASH_STARTS = ("/", "／")


def is_slash_message(event: MessageEvent, command_start=None) -> bool:
    """去掉开头的 @ 和空格后以“/”（或全角“／”）开头的消息：多半是给别的机器人的指令（/签到、/help……），
    她一律不看：不回、不进旁听、不记。管理员指令由上面的指令处理，不受影响（9/30）"""
    starts = tuple(set(_SLASH_STARTS) | {c for c in (command_start or ()) if c})
    msg = event.get_message()
    edge = edge_at_positions(msg)
    text = "".join(seg_text(seg) for i, seg in enumerate(msg) if not (seg.type == "at" and i in edge)).strip()
    return text.startswith(starts)


def is_admin_command(text: str, command_start) -> bool:
    t = text.strip()
    for start in command_start or {"/"}:
        if start and t.startswith(start):
            name = t[len(start):].lstrip()
            # 只认“正好是指令”或“指令后面跟空格”：“/表情真可爱”不算指令
            return any(name == c or name.startswith(c + " ") for c in ADMIN_COMMANDS)
    return False


reset_cmd = on_command("重置", aliases={"清空记忆", "reset"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


_BULK_RESET = {"所有私聊": "private_", "全部私聊": "private_", "私聊": "private_",
               "所有群聊": "group_", "全部群聊": "group_", "群聊": "group_"}


_bulk_pending: dict[tuple[int, str], float] = {}     # (管理员QQ, 前缀) -> 第一次发的时间（等确认）


def _session_keys(prefix: str) -> list[str]:
    keys = {f.stem for f in HISTORY_DIR.glob(f"{prefix}*.json")}
    keys |= {k for k, v in _histories.items() if k.startswith(prefix) and v}
    return sorted(keys)


def clear_all(prefix: str) -> int:
    """清空所有私聊 / 所有群聊的短期记忆（连同还没整理进长期记忆的旧消息、旁听暂存）；返回清了几个"""
    keys = _session_keys(prefix)
    for k in keys:
        clear_history(k)
    if prefix == "group_":
        _passive.clear()
        _recent_chat.clear()
    return len(keys)


@reset_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    """/重置：清空当前这个群 / 这个私聊的短期记忆；/重置 QQ号（或 @某人）：清空和这个人的私聊记忆；
    /重置 所有私聊、/重置 所有群聊：全部清空（30 秒内再发一次确认）"""
    word = arg.extract_plain_text().strip()
    prefix = _BULK_RESET.get(word)
    if prefix:
        what = "私聊" if prefix == "private_" else "群聊"
        pk, now = (event.user_id, prefix), time.monotonic()
        if now - _bulk_pending.get(pk, -1e9) > 30:
            n = len(_session_keys(prefix))
            _bulk_pending[pk] = now
            await reset_cmd.finish(f"（将清空所有{what}的短期记忆，共 {n} 个{what}，清了找不回来。30 秒内再发一次“/重置 {word}”确认）")
        _bulk_pending.pop(pk, None)
        n = clear_all(prefix)
        logger.info(f"管理员 {event.user_id} 清空了所有{what}的短期记忆（{n} 个）")
        await reset_cmd.finish(f"（已清空所有{what}的短期记忆，共 {n} 个）")
    target = _parse_target(event, arg)
    if target and target[0] == "user":
        clear_history(f"private_{target[1]}")
        await reset_cmd.finish(f"（和 {target[1]} 的私聊记忆已清空）")
    clear_history(session_key(event))
    if isinstance(event, GroupMessageEvent):
        _passive.pop(event.group_id, None)
    await reset_cmd.finish("（记忆已清空）")


reload_cmd = on_command("重载人设", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@reload_cmd.handle()
async def _():
    p = load_persona()
    await reload_cmd.finish(f"（人设已重载，{len(p)} 字）")
