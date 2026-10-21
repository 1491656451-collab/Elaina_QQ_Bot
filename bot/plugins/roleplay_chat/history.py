"""聊天记录（短期记忆）的读写（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：每个群 / 每个私聊一份，落盘到 data/history；
群里最近几句聊天（插话、旁听用，10/22 第 6 步搬来）；
还没回复的消息（_inbox）、没发出去时把她那句拿掉（_unrecord）（10/22 第 7 步搬来）"""
import asyncio
import json
from collections import defaultdict, deque
from pathlib import Path

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from .core import HISTORY_DIR, cfg, ltm


_histories: dict[str, list[dict]] = {}


_passive: dict[int, deque] = defaultdict(lambda: deque(maxlen=max(cfg.passive_buffer, 1)))


_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def session_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        if cfg.group_shared_memory:
            return f"group_{event.group_id}"
        return f"group_{event.group_id}_{event.user_id}"
    return f"private_{event.user_id}"


def _hist_path(key: str) -> Path:
    return HISTORY_DIR / f"{key}.json"


def get_history(key: str) -> list[dict]:
    if key not in _histories:
        p = _hist_path(key)
        try:
            _histories[key] = json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _histories[key] = []
    return _histories[key]


def save_history(key: str) -> None:
    hist = _histories.get(key, [])
    limit = cfg.history_max_turns_group if key.startswith("group_") else cfg.history_max_turns
    if len(hist) > limit:
        del hist[: len(hist) - limit]
    _hist_path(key).write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")


def clear_history(key: str) -> None:
    _histories[key] = []
    _hist_path(key).unlink(missing_ok=True)
    ltm.drop_pending(key)                 # 没整理的旧消息也丢掉（重置多半是因为聊歪了）


def api_messages(msgs: list[dict]) -> list[dict]:
    """历史里还存了 uid、昵称等字段，发给模型前只留 role 和 content"""
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


_recent_chat: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))   # 群号 -> 最近的群聊（时间, QQ, 昵称, 内容）


_inbox: dict[tuple[str, int], list[str]] = defaultdict(list)   # (会话, QQ) -> 还没回复的消息


def _unrecord(key: str, record: str, keep: str = "") -> None:
    """这轮回复没发出去（或只发出去一部分）：把她那句从短期记忆里拿掉，或者改成真正发出去的部分；对方说的话留着。
    （不拿会话锁：这时拿着 _hands，别人可能拿着会话锁在等 _hands；这里没有 await，不会被打断）"""
    h = get_history(key)
    for i in range(len(h) - 1, -1, -1):
        if h[i].get("role") == "assistant" and h[i].get("content") == record:
            if keep:
                h[i]["content"] = keep
            else:
                h.pop(i)
            save_history(key)
            break
