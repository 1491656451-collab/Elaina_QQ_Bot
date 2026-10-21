"""指令的公共部分和聊天自己的指令（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
哪些是管理员指令、“/”开头的消息不理、/重置、/重载人设。各功能的指令在各自的文件里"""
import time

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from .core import HISTORY_DIR
from .prompts import load_persona
from .history import _histories, _passive, _recent_chat, clear_history, session_key
from .textin import edge_at_positions, seg_text
from .memory_chat import _parse_target


# 所有指令都只有管理员能用；别人发指令她当没看见（不回、不当成聊天）
ADMIN_COMMANDS = ("重置", "清空记忆", "reset", "重载人设", "认图", "表情", "表情包", "记忆", "查看记忆",
                  "好感", "好感度", "性别", "忘记", "删除记忆", "改记忆", "写信", "说说", "插话", "冒泡", "搭话", "花费", "花销", "语音")


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
