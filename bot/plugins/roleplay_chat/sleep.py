"""晚上休息（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：这天晚上几点睡、几点起、现在睡没睡。
到点道晚安（goodnight_now）、睡前收尾提示（winddown_hint 在 quota.py）还在别处，拆分后面几步再挪"""
import random
from datetime import datetime, timedelta

from . import peak
from .core import cfg


# 23:30～7:30（SLEEP_HOURS）不回消息、不插话冒泡写信搭话；到点时对正在聊的道声晚安（和限额用完的告别同一套：
# 话没说完先收尾，最多再回 2 次；刚互相道过别的不补）。休息时才来找她的，不回，像睡着了
_goodnight: dict[str, str] = {}       # 会话 -> 哪天晚上已经道过晚安


def night_key(now: datetime | None = None) -> str:
    """这是哪天晚上（中午 12 点到第二天中午 12 点算一晚，凌晨算前一天晚上）"""
    now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    return (now - timedelta(hours=12)).strftime("%Y-%m-%d")


def sleep_window(night: str) -> tuple[datetime, datetime] | None:
    """这天晚上几点睡、几点起：在 SLEEP_HOURS 的基础上各自前后浮动 SLEEP_JITTER_MINUTES 分钟。
    按日期抽签，同一晚重启也不变"""
    ranges = peak.parse_ranges(cfg.sleep_hours)
    if not ranges:
        return None
    a, b = ranges[0]
    noon = datetime.strptime(night, "%Y-%m-%d").replace(hour=12, tzinfo=peak.BEIJING)
    start = noon + timedelta(minutes=(a - 720) % 1440)
    end = start + timedelta(minutes=(b - a) % 1440)
    j = max(0.0, cfg.sleep_jitter_minutes)
    rnd = random.Random(f"{night}|{cfg.sleep_hours}")
    start += timedelta(minutes=rnd.uniform(-j, j))
    end += timedelta(minutes=rnd.uniform(-j, j))
    return start, end


def asleep(now: datetime | None = None) -> bool:
    if not cfg.sleep_enabled:
        return False
    now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    w = sleep_window(night_key(now))
    return bool(w) and w[0] <= now < w[1]
