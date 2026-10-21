"""到点道晚安（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：睡觉时间到了，给最近在聊的会话道声晚安。
几点睡、睡没睡在 sleep.py；告别的发法在 quota.py。它要用到收尾、聊天记录、插话写信里认“道别”的说法，所以单独放一个文件，在它们后面导入"""
import asyncio
import time

from nonebot import get_bots, logger

from . import sleep
from .core import _hands, cfg
from .history import _inbox, get_history
from .sleep import _goodnight, night_key, sleep_window
from .quota import _active_chats, _wrapup, say_farewells
from .addressed import _LEAVE_RE
from .proactive_private import _BYE_RE


def _said_bye_last(t: str) -> bool:
    """这个会话里她最后一句已经在道别了（说了再见、要走了）"""
    a = _active_chats.get(t) or {}
    hist_key = t if t.startswith("private_") or cfg.group_shared_memory else f"{t}_{a.get('user_id')}"
    last = next((h for h in reversed(get_history(hist_key)) if h.get("role") == "assistant"), None)
    return bool(last) and bool(_LEAVE_RE.search(last["content"]) or _BYE_RE.search(last["content"]))


async def goodnight_now() -> list[str]:
    """到了休息时间：给最近在聊、又没在收尾、也没刚道过别的会话道声晚安"""
    bots = list(get_bots().values())
    if not bots or not cfg.farewell_on_limit:
        return []
    now = time.monotonic()
    skip = set()
    for t, a in _active_chats.items():
        if now - a["at"] > cfg.farewell_active_minutes * 60:
            continue
        pending = any(k[0] == (t if t.startswith("private_") or cfg.group_shared_memory else f"{t}_{a.get('user_id')}")
                      and v for k, v in _inbox.items())
        if t in _wrapup or pending:
            skip.add(t)                        # 话还没说完：等对方这条回完再说晚安（见 _sleep_wrapup）
        elif _said_bye_last(t):
            _goodnight[t] = night_key()        # 刚道过别了，不再补
            skip.add(t)
    async with _hands:
        return await say_farewells(bots[0], None, None, skip=skip)


async def _sleep_loop() -> None:
    done_night = None
    told = None
    while True:
        await asyncio.sleep(30)
        try:
            night = night_key()
            if told != night and (w := sleep_window(night)):
                told = night
                logger.info(f"今晚 {w[0]:%H:%M} 睡，明早 {w[1]:%H:%M} 起（SLEEP_HOURS {cfg.sleep_hours}，前后浮动 {cfg.sleep_jitter_minutes:g} 分钟）")
            if sleep.asleep() and done_night != night:
                done_night = night
                await goodnight_now()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"道晚安出错：{e}")
