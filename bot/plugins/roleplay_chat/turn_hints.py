"""每轮拼进提示里、属于聊天的那部分（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
时间感（隔了多久、现在几点）、这轮回长还是回短、没人提过的词；送面包、给钱时的提示（10/22 第 5 步搬来）"""
import random
import re
import time
from datetime import datetime, timedelta

from . import peak
from .core import cfg
from .prompts import LENGTH_HINT, SHORT_VARIANTS


_LONG_HINTS = (
    "讲讲", "讲个", "讲一下", "说说", "聊聊", "故事", "经历", "详细", "具体", "介绍", "解释",
    "为什么", "怎么回事", "发生了什么", "还记得", "后来呢", "然后呢", "展开", "多说点",
    "难过", "伤心", "好累", "烦死", "崩溃", "想哭", "怎么办", "失恋", "压力",
)


def reply_mode(text: str) -> str:
    """long：讲故事/讲经历/解释/倾诉，或者对方自己写了一大段；其余都是日常闲聊 short"""
    if len(text) >= 60 or any(k in text for k in _LONG_HINTS):
        return "long"
    return "short"


def short_hint(user_text: str = "") -> str:
    """随机挑这轮回复的长短；对方认真写了一大段（30 字以上）时，不挑“几个字就好”那档，免得显得敷衍"""
    variants = SHORT_VARIANTS[1:] if len(user_text) >= 30 else SHORT_VARIANTS
    total = sum(w for w, _ in variants)
    r, acc = random.random() * total, 0.0
    for w, h in variants:
        acc += w
        if r < acc:
            return h
    return variants[-1][1]


# 倾诉的说法（和 _LONG_HINTS 里想听故事、要解释的那些分开）
_VENT_HINTS = ("难过", "伤心", "好累", "烦死", "崩溃", "想哭", "失恋", "压力", "考砸", "骂了我", "被骂", "做不好", "好烦", "委屈",
               "没用", "撑不住", "睡不着", "不开心")


_STORY_HINTS = ("讲讲", "讲个", "讲一下", "说说", "聊聊", "故事", "经历", "详细", "具体", "介绍", "解释")


def length_hint_for(mode: str, text: str, fam: str) -> str:
    if mode == "short":
        return short_hint(text)
    if (mode == "long" and fam in ("stranger", "disliked") and any(k in text for k in _VENT_HINTS)
            and not any(k in text for k in _STORY_HINTS)):
        return LENGTH_HINT["vent_disliked" if fam == "disliked" else "vent_stranger"]   # 10/22：讨厌的人单给一句
    return LENGTH_HINT[mode]


def unprompted_hits(reply: str, context: str) -> list[str]:
    """回复里有、但对方这句和最近的聊天都没提到的词（比如突然冒出来的“蘑菇”）"""
    out = []
    for group in cfg.unprompted_words:
        words = [w for w in str(group).split("|") if w]
        if any(w in context for w in words):
            continue                       # 上文提到过（比如对方说“香菇”），她说“蘑菇”也正常
        out += [w for w in words if w in reply]
    return out


def human_gap(sec: float) -> str:
    """把秒数说成人话：40 分钟 / 5 个小时 / 3 天 / 两个多星期 / 一个多月"""
    m, h, d = sec / 60, sec / 3600, sec / 86400
    if h < 1:
        return f"{max(1, int(m))} 分钟"
    if d < 1:
        return f"{int(h)} 个小时"
    if d < 14:
        return f"{int(d)} 天"
    if d < 30:
        return f"{int(d // 7)} 个多星期".replace("2 个", "两个").replace("3 个", "三个").replace("4 个", "四个")
    n = int(d // 30)
    return "一个多月" if n == 1 else f"{n} 个多月"


_WEEKDAY = "一二三四五六日"


BACK_HINT_HOURS = 3          # 她说要走、超过这么久才再开口，就不提“刚回来”了（10-14 隔了 22 小时还说“回来了。”）


def clock_text(now: datetime | None = None) -> str:
    """“上午 9:05”“中午 11:43”“下午 1:45”“晚上 9:30”：只写“11 点 43 分”，模型会当成晚上 11 点（10/18：中午冒泡说“大半夜的”）"""
    now = now or datetime.now(peak.BEIJING)
    h = now.hour
    period = ("凌晨" if h < 5 else "早上" if h < 9 else "上午" if h < 11 else "中午" if h < 13
              else "下午" if h < 17 else "傍晚" if h < 19 else "晚上")
    return f"{period} {h - 12 if h > 12 else h}:{now.minute:02d}"


def _birthday_md() -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", str(cfg.birthday or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def now_hint(now: datetime | None = None) -> str:
    """每轮告诉她今天几号、现在几点（10/18：以前聊天提示里没有，生日当天说“南瓜祭典早过去了”、晚上被说“早上好”就回“天都亮了”）"""
    now = now or datetime.now(peak.BEIJING)
    line = (f"【现在】{now.month} 月 {now.day} 日（星期{_WEEKDAY[now.weekday()]}）{clock_text(now)}。"
            "对方说的时间（早上好、晚安、这么晚了……）和这个对不上时，以这里为准。")
    bd = _birthday_md()
    if bd:
        yesterday = now - timedelta(days=1)
        if (now.month, now.day) == bd:
            line += f"\n今天是你的生日（{bd[0]} 月 {bd[1]} 日）。"
        elif (yesterday.month, yesterday.day) == bd:
            line += f"\n昨天（{bd[0]} 月 {bd[1]} 日）是你的生日。"
    return line


def time_hint(history: list[dict], last_seen: float | None, fam: str, last_letter: float | None = None) -> str:
    """告诉她：上一段聊天是多久以前的事；这个人隔了多久才来找她（按关系远近决定要不要提）"""
    now = time.time()
    lines = []
    last_ts = next((h["ts"] for h in reversed(history) if h.get("ts")), None)
    if last_ts and now - last_ts >= cfg.context_stale_hours * 3600:
        lines.append(f"上面的聊天已经是 {human_gap(now - last_ts)}前的事了，现在是新的一段对话："
                     "别接着上次的话题往下说，除非对方主动提起。")
    if last_seen and now - last_seen >= cfg.gap_notice_hours * 3600 and fam in ("acquaintance", "close"):
        gap, days = human_gap(now - last_seen), (now - last_seen) / 86400
        if fam == "acquaintance":
            if days >= 3:
                lines.append(f"对方已经 {gap}没来找你了。可以随口带一句（比如“好久不见”），不用热情，也不用追问。")
        elif days >= 7:
            lines.append(f"对方已经 {gap}没来找你了，挺久的。你其实有点在意，但嘴上不会承认，"
                         "可以别扭地吐槽一句（比如“哦，还记得我啊”），别撒娇，也别一直抱怨。")
        elif days >= 1:
            lines.append(f"对方已经 {gap}没来找你了。你注意到了，想提的话就嘴硬地提一句（比如“这几天跑哪去了”），不提也行。")
        if last_letter and last_letter > last_seen:
            lines.append("你之前给对方寄过一封信，他一直没回。可以顺口问一句收到没有，别表现得很在意。")
    if not lines:
        return ""
    return "【时间】" + "\n".join(lines)


_MONEY_RULE = ("你爱钱，但取之有道：只收自己出力换来的报酬。看最近的聊天：对方是为你帮的忙、答应接下的委托付钱，而且数目合理"
               "（一枚铜币大约一个面包，一枚银币够住一晚便宜旅馆，金币是大钱，只配大委托），就收下，嫌少可以讨价还价；"
               "你什么都没做、对方无缘无故塞钱、数目大得离谱，就不收，还会起疑（比如“无功不受禄。”“你想让我干什么？”）。"
               "嘴上说给钱不等于钱真的到了你手里，别当成已经收到了一大笔钱，也别因此对对方态度变好。")


def gift_hint(kind: str, snippet: str, bread_first: bool, fam: str) -> str:
    if kind == "bread":
        if not bread_first:
            return (f"【送面包】对方说要送你面包（「{snippet}」），可今天已经送过一次了。一天一个就够了，这次不收"
                    "（比如“今天已经吃过了。”“想用面包收买我？没那么容易。”），也别表现得很高兴。")
        extra = {
            "disliked": "不过你讨厌这个人，收不收看你心情，就算收了也不会因此改观。",
            "stranger": "你和对方不熟：收下归收下，道声谢就行，别一下子变亲热。",
        }.get(fam, "")
        return f"【送面包】对方说要送你面包（「{snippet}」），这是今天第一次。你最爱面包，收下了，嘴上可以嘴硬，但看得出挺高兴。{extra}"
    head = f"【给钱】对方说要给你钱（「{snippet}」）。"
    if kind == "foreign":
        return (head + "对方说的不是铜币、银币、金币，是你没听说过的钱，你不知道它值多少，不会收（可以吐槽“那是哪个国家的钱？”）；"
                "除非对方把它换成铜币、银币、金币，或者讲清楚它到底值多少，而且讲得通。" + _MONEY_RULE)
    if kind == "vague":
        return head + "对方没说给多少、给的是什么钱，可以先问清楚（“多少？什么钱？”），别先高兴起来。" + _MONEY_RULE
    return head + _MONEY_RULE
