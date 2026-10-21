"""掉线提醒（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：记下掉线、重新上线后私信管理员、被踢后先安静一阵。
上线 / 断线时要做的事（启动补回、心跳、表情循环）在 catchup.py"""
import asyncio
import json
import os
import time
from datetime import datetime

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent

from . import peak
from .core import BOT_DIR, cfg
from .quota import _hour_window


OFFLINE_FILE = BOT_DIR / "data" / "offline_alert.json"


_quiet_until = 0.0         # 被踢下线又上线后，这个时间（time.time()）之前先不说话


def quiet_left() -> float:
    """刚被踢下线又上线：还要安静几秒（0 = 不用安静）"""
    return max(0.0, _quiet_until - time.time())


def start_quiet() -> None:
    global _quiet_until
    if cfg.relogin_quiet_minutes > 0:
        _quiet_until = time.time() + cfg.relogin_quiet_minutes * 60
        logger.info(f"被踢下线后重新上线：先安静 {cfg.relogin_quiet_minutes:g} 分钟（不回消息、不插话、不冒泡、不写信），之后补回这段时间的私聊")


def _read_offline() -> dict:
    try:
        return json.loads(OFFLINE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_offline(data: dict | None) -> None:
    try:
        if data is None:
            OFFLINE_FILE.unlink(missing_ok=True)
            return
        OFFLINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFLINE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"记录掉线信息失败：{e}")


def note_offline(kicked: bool, reason: str) -> None:
    """记下这次下线：被踢（NapCat 报告账号下线）优先，断开连接只在没有记录时补一条"""
    old = _read_offline()
    if old.get("kicked") and not kicked:
        return
    now = time.monotonic()
    _write_offline({
        "since": old.get("since") or time.time(),      # 先断开、后收到“被下线”通知时，按最早那次算
        "kicked": kicked, "reason": reason, "pid": os.getpid(),
        "replies_hour": sum(1 for t in _hour_window if now - t <= 3600),
    })


def offline_message(rec: dict, self_id: str) -> str:
    since = datetime.fromtimestamp(float(rec["since"]), peak.BEIJING)
    mins = max(1, int((time.time() - float(rec["since"])) / 60))
    gap = f"{mins} 分钟" if mins < 60 else f"{mins // 60} 小时 {mins % 60} 分钟"
    why = (f"NapCat 报告账号被下线（{rec['reason']}）" if rec.get("kicked")
           else "机器人和 NapCat 的连接断了，可能是 NapCat 关了、崩了，或者账号被下线")
    tip = ""
    if rec.get("kicked"):
        tip = "\n如果是被踢下线，建议先少回一阵，详见《风控记录》。"
        if cfg.relogin_quiet_minutes > 0:
            tip += f"\n她会先安静 {cfg.relogin_quiet_minutes:g} 分钟，之后再补回这段时间的私聊。"
    return (f"【QQ 机器人提醒】小号 {self_id} 在 {since:%m月%d日 %H:%M} 下线了。\n原因：{why}\n"
            f"离线约 {gap}，现在已重新上线。下线前一小时回复了 {rec.get('replies_hour', 0)} 条。{tip}")


def should_alert(rec: dict) -> bool:
    """被踢下线：一定提醒。只是断开连接：机器人一直在跑（同一个进程）、断了 1 分钟以上才提醒；
    换了进程多半是你自己重开了 start.bat，不提醒"""
    if not rec or not cfg.offline_alert:
        return False
    if rec.get("kicked"):
        return True
    return rec.get("pid") == os.getpid() and time.time() - float(rec.get("since", time.time())) >= 60


async def send_offline_alert(bot: Bot, delay: float = 15.0) -> bool:
    rec = _read_offline()
    if not rec:
        return False
    ok = should_alert(rec)
    _write_offline(None)
    if not ok:
        return False
    await asyncio.sleep(delay)                          # 刚上线先缓一缓
    text = offline_message(rec, str(bot.self_id))
    sent = False
    for su in bot.config.superusers:
        try:
            await bot.send_private_msg(user_id=int(su), message=text)
            sent = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"掉线提醒没发出去（管理员 {su} 需要和小号是好友）：{e}")
    logger.info(f"已私信管理员掉线情况：{text.splitlines()[0]}")
    return sent


async def _is_offline_event(event: NoticeEvent) -> bool:
    return "offline" in str(getattr(event, "notice_type", "")).lower()
