"""
DeepSeek 高峰时段（北京时间 周一至周五 9:00-12:00、14:00-18:00，不含法定节假日）

高峰时段 API 价格更高，这段时间让伊蕾娜“很忙”：少回、短回，能不调用模型就不调用。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))   # 用固定时区，Windows 上不需要额外装 tzdata


def _span(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


# 2026 年法定节假日放假日期（国务院办公厅国办发明电〔2025〕7号）
# 以后每年底国务院公布新一年安排后，在 .env 的 PEAK_HOLIDAYS 里补上即可
HOLIDAYS_2026 = (
    _span("2026-01-01", "2026-01-03")     # 元旦
    + _span("2026-02-15", "2026-02-23")   # 春节
    + _span("2026-04-04", "2026-04-06")   # 清明
    + _span("2026-05-01", "2026-05-05")   # 劳动节
    + _span("2026-06-19", "2026-06-21")   # 端午
    + _span("2026-09-25", "2026-09-27")   # 中秋
    + _span("2026-10-01", "2026-10-07")   # 国庆
)

BUSY_LINES = [
    "在忙，晚点。",
    "正在赶路，稍后再说。",
    "嗯……等我一下，手上有事。",
    "现在不方便，回头聊。",
    "忙着呢。",
    "我在飞，看不清消息。晚点说。",
    "在接委托，别吵。",
    "晚点再说吧，现在腾不出手。",
]


def parse_ranges(text: str) -> list[tuple[int, int]]:
    """'09:00-12:00,14:00-18:00' -> [(540, 720), (840, 1080)]（分钟）"""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        ha, ma = a.split(":")
        hb, mb = b.split(":")
        out.append((int(ha) * 60 + int(ma), int(hb) * 60 + int(mb)))
    return out


def is_peak(ranges: str, holidays: list[str], now: datetime | None = None) -> bool:
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    if now.weekday() >= 5:                    # 周六周日（调休上班日也不算高峰，按周一至周五算）
        return False
    if now.date().isoformat() in set(holidays):
        return False
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in parse_ranges(ranges))


FAREWELL_LINES = [
    "好了，我该上路了。",
    "我要去下一个国家了，有缘再见。",
    "天色不早了，我得赶路了。",
    "扫帚已经等不及了，我先走一步。",
    "今天就聊到这儿吧，我要启程了。",
    "下一站还挺远的，我先出发了。",
]


def farewell_line() -> str:
    return random.choice(FAREWELL_LINES)


def busy_line() -> str:
    return random.choice(BUSY_LINES)
