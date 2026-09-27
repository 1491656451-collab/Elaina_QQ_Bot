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


# 每小时额度用完时的告别：群里对大家说，私聊对这个人说。每次挑一句没用过的
FAREWELL_GROUP_LINES = [
    "好了，今天就陪你们到这儿，我该上路了。",
    "天快黑了，我得赶在关城门前到下一个镇子。先走了。",
    "扫帚已经等得不耐烦了，我先出发了。",
    "下一站还挺远的，再不走今晚就要露宿了。各位回见。",
    "我去找家旅馆落脚，今天就聊到这儿吧。",
    "嗯……时间差不多了，我要启程了。别太想我。",
    "有人托我送信，我得赶路了，先失陪。",
    "今天说得够多了，我去下一个国家看看。",
    "我去买面包了，顺便赶路，回头再说。",
    "风向正好，适合飞。我走了。",
    "就到这儿吧，旅途还长着呢。",
    "我要去赶路了，你们慢慢聊。",
]
FAREWELL_PRIVATE_LINES = [
    "我得走了，下次再聊吧。",
    "天色不早了，我先赶路，回头再说。",
    "我要去下一个国家了，有缘再见。",
    "先不聊了，我得在天黑前找到旅馆。",
    "扫帚在催我了，下次再说吧。",
    "今天就到这儿，我要上路了。",
    "我去办点事，晚点再找你……看我心情。",
    "要赶路了，你也早点休息。",
    "先这样吧，我要出发了。",
    "我得走了。别以为我是嫌你烦，是真的要赶路。",
]
FAREWELL_LINES = FAREWELL_GROUP_LINES      # 旧名字，别的地方还在用


def farewell_line(private: bool = False, avoid=()) -> str:
    lines = FAREWELL_PRIVATE_LINES if private else FAREWELL_GROUP_LINES
    fresh = [x for x in lines if x not in avoid]
    return random.choice(fresh or lines)


def busy_line() -> str:
    return random.choice(BUSY_LINES)
