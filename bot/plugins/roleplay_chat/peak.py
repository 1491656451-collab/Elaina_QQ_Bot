"""
DeepSeek 高峰时段（北京时间 周一至周五 9:00-12:00、14:00-18:00，不含法定节假日）

高峰时段 API 价格更高，这段时间让伊蕾娜“很忙”：少回、短回，能不调用模型就不调用。
"""
from __future__ import annotations

import random
from collections import deque
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
    "在排队，等会儿。",
    "正跟人讨价还价呢，晚点回你。",
    "等等，我在找路。",
    "手上有活，待会儿说。",
    "……在忙。",
    "在跟委托人说话，晚点。",
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


# 关系分档：告别、晚安、“今天累了”按关系挑台词（群里对大家说，不分档）
#   far：讨厌的人、陌生人（客气、有距离）｜friend：普通朋友｜near：熟人（随意、偶尔嘴硬）｜close：很熟（嘴硬里带点在意）
TIER_OF = {"disliked": "far", "stranger": "far", "friend": "friend", "acquaintance": "near", "close": "close"}

# 限额用完时的告别。每句都要带上“先走了”“得走了”“先失陪”“下次再聊”这类说法（_LEAVE_RE 认得出来，才知道她已经道过别）
FAREWELL_GROUP_LINES = [
    "好了，今天就陪你们到这儿。我该走了。",
    "天快黑了，得赶在关城门前到下一个镇子。我先走了。",
    "扫帚已经在催了，我先失陪。",
    "下一站还挺远，再不走今晚就要露宿了。我先走了，各位回见。",
    "旅馆只剩最后一间房，我得走了。",
    "嗯……时间差不多了，我要赶路了。别太想我。",
    "有封信要替人送，先失陪。",
    "今天说得够多了。我走了，下一个国家见。",
    "风向正好，适合飞。我走了。",
    "就到这儿吧，我先撤了。你们慢慢聊。",
    "委托人还在等我，先失陪了。",
    "再聊下去天都要黑了。我该走了。",
    "……哎呀，都这个时候了。我先走了。",
    "今天就到这儿，下次再聊。",
]
FAREWELL_PRIVATE_TIERS = {
    "far": [
        "抱歉，我得走了。下次再聊吧。",
        "我还要赶路，先失陪了。",
        "今天就到这儿吧，我先走了。",
        "天色不早了，我得走了。有缘再见。",
        "不好意思，还有委托要办，先失陪。",
    ],
    "friend": [
        "我得走了，下次再聊吧。",
        "天色不早了，我要赶路了，回头再说。",
        "扫帚在催了，先不聊了。",
        "先这样吧，我得走了。你也别聊太晚。",
        "今天就到这儿，我先撤了。",
        "旅馆快关门了，我先走了，改天再聊。",
    ],
    "near": [
        "我先走了，晚点再找你……看我心情。",
        "我得走了。别以为我是嫌你烦，是真的要赶路。",
        "再聊下去我今晚就得睡野外了。我先走了。",
        "我该走了。下次有好玩的事，记得留着讲给我听。",
        "扫帚已经在门口转了三圈了，我先走了。",
        "要赶路了。……别那副表情，下次再聊。",
    ],
    "close": [
        "我得走了。……下次早点来找我，别让我等。",
        "先走了。路上看到好吃的店，会记着告诉你的。",
        "今天就到这儿吧。我走了，你也早点休息，别熬夜。",
        "我该走了。……才不是舍不得，是真的要赶路。",
        "下次再聊。你要是想我了——算了，你肯定会的。",
        "要赶路了。你照顾好自己，别让我操心。……我是说，别给我添麻烦。",
    ],
}
FAREWELL_PRIVATE_LINES = [x for v in FAREWELL_PRIVATE_TIERS.values() for x in v]   # 旧名字：不分档时用
FAREWELL_LINES = FAREWELL_GROUP_LINES      # 旧名字，别的地方还在用


# 晚上休息前道晚安（9/29）：每句都带“晚安”或“睡了”（_BYE_RE 认得出来）
SLEEP_GROUP_LINES = [
    "不早了，我找家旅馆歇下了。各位晚安。",
    "困了……今天就到这儿，我先睡了。",
    "明天一早还要赶路，我先去睡了。你们也别聊太晚。",
    "蜡烛快烧完了，我睡了。晚安。",
    "今天飞了一整天，腰都酸了。我去睡了，明天见。",
    "嗯……眼睛睁不开了。晚安。",
    "旅馆的床终于空出来了，我先睡了。各位晚安。",
    "楼下酒馆还在吵……算了，我塞着耳朵睡了。晚安。",
    "日记写完了，我也该睡了。晚安。",
    "再熬下去明天就要顶着黑眼圈赶路了。我先睡了。",
    "今天就到这里。晚安，别梦见我。",
    "哈啊……我去睡了。你们也早点休息。",
    "晚安。明天有好玩的事再告诉我。",
]
SLEEP_PRIVATE_TIERS = {
    "far": [
        "不早了，我要休息了。晚安。",
        "抱歉，我该睡了。有事明天再说吧。",
        "时间不早了，我先睡了。晚安。",
        "我这边已经很晚了，先去睡了。",
    ],
    "friend": [
        "困了，明天再聊吧。晚安。",
        "我先睡了，明天还要赶路。你也早点睡。",
        "蜡烛快烧完了……我睡了，明天见。",
        "嗯……好困。晚安，别熬太晚。",
        "今天就聊到这儿吧，我要睡了。晚安。",
    ],
    "near": [
        "我要去睡美容觉了。晚安。",
        "再聊下去我明天就要顶着黑眼圈赶路了。我去睡了，晚安。",
        "我在旅馆躺下了，有话明天再说。晚安。",
        "哈啊……不行了，我先睡了。你也别熬夜，晚安。",
        "日记都写完了，我睡了。晚安，别梦到奇怪的东西。",
    ],
    "close": [
        "……困了。我先睡了，你也早点睡，明天别让我看到你还醒着。",
        "晚安。明天醒了再来找我，我不会跑掉的。……大概。",
        "我要睡了。今天……聊得还不错。晚安。",
        "熬夜对皮肤不好，这是美少女的忠告。我去睡了，晚安。",
        "晚安。做个好梦——最好是有我出现的那种。",
        "我先睡了。你也是，别光顾着盯着手里那块发光的东西。",
    ],
}
SLEEP_PRIVATE_LINES = [x for v in SLEEP_PRIVATE_TIERS.values() for x in v]   # 旧名字：不分档时用

# 今天的钱花完了之后才来私聊的人：回一句“今天累了”（不调用模型）
TIRED_TIERS = {
    "far": [
        "今天有点累了，明天再说吧。",
        "抱歉，今天跑了太多地方，有事明天再说。",
        "我已经在旅馆歇下了，明天吧。",
    ],
    "friend": [
        "今天有点累了，明天再说吧。",
        "今天跑了太多地方，累了……明天再聊。",
        "今天就到这儿吧，我要休息了。明天再找我。",
    ],
    "near": [
        "今天累坏了……明天再聊。",
        "嗯……今天不想说话了，明天吧。",
        "我已经躺下了。有事明天再说，别吵我。",
        "今天的话都说完了，明天请早。",
    ],
    "close": [
        "今天飞了一整天，扫帚都累了，何况我。明天吧。",
        "……累了。明天再听你说，好不好。",
        "我已经躺下了。有事明天再说，今天就放过我吧。",
    ],
}

def in_ranges(ranges: str, now: datetime | None = None) -> bool:
    """现在在不在这些时间段里（北京时间）；支持跨午夜，比如 23:30-07:30"""
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    minute = now.hour * 60 + now.minute
    for a, b in parse_ranges(ranges):
        if (a <= minute < b) if a <= b else (minute >= a or minute < b):
            return True
    return False


_recent_used: deque = deque(maxlen=12)   # 最近说过的几句（跨会话、跨天），尽量不重复


def _pick(lines: list[str], avoid=()) -> str:
    fresh = [x for x in lines if x not in avoid and x not in _recent_used]
    fresh = fresh or [x for x in lines if x not in avoid] or lines
    line = random.choice(fresh)
    _recent_used.append(line)
    return line


def _tiered(tiers: dict, fam: str | None) -> list[str]:
    return tiers.get(TIER_OF.get(fam or "", "friend"), tiers["friend"])


def sleep_line(private: bool = False, avoid=(), fam: str | None = None) -> str:
    return _pick(_tiered(SLEEP_PRIVATE_TIERS, fam) if private else SLEEP_GROUP_LINES, avoid)


def farewell_line(private: bool = False, avoid=(), fam: str | None = None) -> str:
    return _pick(_tiered(FAREWELL_PRIVATE_TIERS, fam) if private else FAREWELL_GROUP_LINES, avoid)


def tired_line(fam: str | None = None) -> str:
    return _pick(_tiered(TIRED_TIERS, fam))


def busy_line() -> str:
    return random.choice(BUSY_LINES)
