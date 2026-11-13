"""晚上休息（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：这天晚上几点睡、几点起、现在睡没睡。
到点道晚安（goodnight_now）、睡前收尾提示（winddown_hint 在 quota.py）还在别处，拆分后面几步再挪"""
import json
import random
from datetime import datetime, timedelta

from nonebot import logger

from . import peak
from .core import BOT_DIR, cfg


# 23:30～7:30（SLEEP_HOURS）不回消息、不插话冒泡写信搭话；到点时对正在聊的道声晚安（和限额用完的告别同一套：
# 话没说完先收尾，最多再回 2 次；刚互相道过别的不补）。休息时才来找她的，不回，像睡着了
_goodnight: dict[str, str] = {}       # 会话 -> 哪天晚上已经道过晚安


def night_key(now: datetime | None = None) -> str:
    """这是哪天晚上（中午 12 点到第二天中午 12 点算一晚，凌晨算前一天晚上）"""
    now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    return (now - timedelta(hours=12)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 11/13 她自己定的作息（《方案-她自己定作息-1113》）
# 每天傍晚问她一次（sleep_plan.py），定好的按“哪天晚上”存在 data/sleep_plan.json；SLEEP_BY_MODEL 开着、这晚定过，就按她定的来。
# 时间都存成“离这天中午 12 点多少分钟”（22:30 → 630，次日 07:40 → 1180），跨午夜好算
PLAN_KEEP_NIGHTS = 14
_plans: dict | None = None


def plan_file():
    return BOT_DIR / "data" / "sleep_plan.json"


def plans() -> dict:
    global _plans
    if _plans is None:
        try:
            d = json.loads(plan_file().read_text(encoding="utf-8"))
            _plans = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _plans = {}
    return _plans


def save_plan(night: str, plan: dict) -> None:
    d = plans()
    d[night] = plan
    for k in sorted(d)[:-PLAN_KEEP_NIGHTS]:
        d.pop(k, None)
    f = plan_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def _off(hhmm: int) -> int:
    """一天里的第几分钟 → 离中午 12 点多少分钟（0～1439）"""
    return (hhmm - 720) % 1440


def _range_off(text: str) -> tuple[int, int] | None:
    r = peak.parse_ranges(text or "")
    return (_off(r[0][0]), _off(r[0][1])) if r else None


def clamp_plan(sleep_min: int, wake_min: int) -> tuple[int, int, list[str]]:
    """她说的几点睡、几点起（一天里的第几分钟）→ 收进 SLEEP_START_RANGE / SLEEP_END_RANGE、睡 SLEEP_MIN_HOURS～SLEEP_MAX_HOURS 小时；
    返回（入睡、起床离中午多少分钟，收过哪些）"""
    notes = []
    a, b = _off(sleep_min), _off(wake_min)
    sr, er = _range_off(cfg.sleep_start_range), _range_off(cfg.sleep_end_range)
    if sr and not sr[0] <= a <= sr[1]:
        a2 = min(max(a, sr[0]), sr[1])
        notes.append(f"入睡 {_hhmm(a)} → {_hhmm(a2)}")
        a = a2
    if er and not er[0] <= b <= er[1]:
        b2 = min(max(b, er[0]), er[1])
        notes.append(f"起床 {_hhmm(b)} → {_hhmm(b2)}")
        b = b2
    lo, hi = int(cfg.sleep_min_hours * 60), int(cfg.sleep_max_hours * 60)
    if b - a < lo or b - a > hi:
        b2 = a + (lo if b - a < lo else hi)
        if er:
            b2 = min(max(b2, er[0]), er[1])
        if b2 - a < lo:                          # 起床已经到最晚了还不够睡：入睡往前挪
            a = max(b2 - lo, sr[0] if sr else b2 - lo)
        notes.append(f"睡 {(b - a) / 60:.1f} 小时 → 起床 {_hhmm(b2)}")
        b = b2
    return a, b, notes


def _hhmm(off: int) -> str:
    m = (off + 720) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def planned(night: str) -> tuple[datetime, datetime] | None:
    """她给这晚定的作息；没开 SLEEP_BY_MODEL、没定过返回 None"""
    if not cfg.sleep_by_model:
        return None
    p = plans().get(night)
    if not isinstance(p, dict) or "start" not in p or "end" not in p:
        return None
    try:
        noon = datetime.strptime(night, "%Y-%m-%d").replace(hour=12, tzinfo=peak.BEIJING)
        return noon + timedelta(minutes=int(p["start"])), noon + timedelta(minutes=int(p["end"]))
    except (TypeError, ValueError) as e:
        logger.warning(f"作息：{night} 的计划读不出来，照老办法：{e}")
        return None


def sleep_window(night: str) -> tuple[datetime, datetime] | None:
    """这天晚上几点睡、几点起：她自己定过（SLEEP_BY_MODEL）就按她的；不然在 SLEEP_HOURS 的基础上各自前后浮动
    SLEEP_JITTER_MINUTES 分钟，按日期抽签，同一晚重启也不变"""
    p = planned(night)
    if p:
        return p
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
