"""每轮拼进提示里、属于聊天的那部分（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
时间感（隔了多久、现在几点）、这轮回长还是回短、没人提过的词；送面包、给钱时的提示（10/22 第 5 步搬来）"""
import random
import re
import time
from datetime import datetime, timedelta

from . import peak
from .core import cfg
from .memory import _grams, _related
from .prompts import (LENGTH_HINT, LIFE_CHAT_HEAD, LIFE_ELSEWHERE_HEAD, LIFE_LONG_STAY_TEXT, LIFE_PLAN_TEXT, LIFE_PROACTIVE_HEAD,
                      SHORT_VARIANTS)


_LONG_HINTS = (
    "讲讲", "讲个", "讲一下", "说说", "聊聊", "故事", "经历", "详细", "具体", "介绍", "解释",
    "为什么", "怎么回事", "发生了什么", "还记得", "后来呢", "然后呢", "展开", "多说点",
    "难过", "伤心", "好累", "烦死", "崩溃", "想哭", "怎么办", "失恋", "压力",
)


# 11/05（识图 agent《交接-图片描述变长-聊天-1105》）：图片描述放宽到 100 字以后，只发一张聊天截图也会被当成“写了一大段”。
# 算这轮回长还是回短时，只看对方自己打的字：[图片：……]、（引用了图片：……）不算字数，也不拿来找“讲讲”“难过”这类词；
# 发了图就不挑“几个字就好”那档（以前靠描述本身 30 字以上碰巧做到，现在写明）
_IMG_OPEN_RE = re.compile(r"\[图片[：:]|（引用了图片：|\[聊天记录（共")          # 11/06 转发的聊天记录也不是对方自己打的字
_HAS_IMG_RE = re.compile(r"\[图片[\]：:]|（引用了图片：|\[聊天记录")
_PAIRS = {"[": "]", "（": "）"}


def own_text(text: str) -> str:
    """对方自己打的字：去掉图片描述。
    按括号配对找描述的结尾（11/05 识图 agent 提的）：描述里自己带括号（“（画的不是你）”“（一只黑、一只粉）”）时，
    不能遇到第一个“）”“]”就停，不然引用两张图时第二张的描述会整段留下来，被当成对方写了一大段。没配上的就去到最后"""
    text = text or ""
    out, i = [], 0
    while True:
        m = _IMG_OPEN_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        opener = text[m.start()]
        closer = _PAIRS[opener]
        depth, j = 1, m.end()
        while j < len(text) and depth:
            if text[j] == opener:
                depth += 1
            elif text[j] == closer:
                depth -= 1
            j += 1
        i = j
    return "".join(out).strip()


def reply_mode(text: str) -> str:
    """long：讲故事/讲经历/解释/倾诉，或者对方自己写了一大段；其余都是日常闲聊 short"""
    own = own_text(text)
    if len(own) >= 60 or any(k in own for k in _LONG_HINTS):
        return "long"
    return "short"


def short_hint(user_text: str = "") -> str:
    """随机挑这轮回复的长短；对方认真写了一大段（30 字以上）或者发了图时，不挑“几个字就好”那档，免得显得敷衍"""
    serious = len(own_text(user_text)) >= 30 or bool(_HAS_IMG_RE.search(user_text or ""))
    variants = SHORT_VARIANTS[1:] if serious else SHORT_VARIANTS
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
    text = own_text(text)                     # 11/05：倾诉、要听故事只看对方自己打的字，不看图片描述
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


# ---------------------------------------------------------------- 10/23【你的近况】
_KIND_NOTE = {"野外": "（在野外赶路）", "小说地点": "（以前来过的地方）"}


def _life_place(place: dict | None) -> str:
    """status（记忆 agent 给的）：还没说过 → 不写；前几天说的 → 不一定还在；现在 → 待了几天"""
    if not place or not place.get("name"):
        return ""
    where = f"{place['name']}{_KIND_NOTE.get(place.get('kind') or '', '')}"
    if place.get("status") == "前几天说的":
        return f"你{place.get('said') or '前几天'}说过自己在{where}，之后没再提过，现在不一定还在那里。"
    n = int(place.get("days_here") or 1)
    stay = f"，已经待了 {n} 天左右" if n >= 2 else ""
    return f"你说过自己现在在{where}{stay}。"


def _life_plans(plans, statuses: tuple[str, ...]) -> str:
    """她说过哪天动身（self_mem 的 plans，11/05）：只写 statuses 里的那几种状态"""
    out = []
    for p in plans or []:
        tmpl = LIFE_PLAN_TEXT.get(p.get("status") or "")
        if not tmpl or p.get("status") not in statuses or not p.get("text"):
            continue
        to = f"（去{p['to']}）" if p.get("to") else ""
        said = str(p.get("said") or "前几天")
        said = f" {said}" if said[:1].isdigit() else said          # “你 2 天前说过”
        out.append(tmpl.format(said=said, text=p["text"], to=to, due=p.get("due") or ""))
    return "".join(out)


def _life_item(it: dict) -> str:
    mood = f"（当时觉得{it['mood']}）" if it.get("mood") else ""
    return f"{it.get('when') or '最近'}：{it.get('text', '')}{mood}"


# 11/05（《人机感检查-周到型-1105》F）：【你的近况】以前每轮都带，她什么话题都要报一下自己在海边、旅馆的账。
# 现在：对方问到她（在哪、在干嘛、最近怎样、旅行……）才整段带；别的时候只带和这几句话对得上的那一两件，在哪也不带
_ASK_LIFE_RE = re.compile(r"在哪|哪儿|哪里|在干|干嘛|干啥|干什么|做什么|在做|忙什么|在忙|忙不忙|最近|今天|这几天|这两天|昨天|"
                          r"赶路|路上|几天|多远|到哪|下一站|打算去|旅行|旅途|旅馆|住哪|住在|去哪|上路|动身|出发|启程|还在|过得|怎么样|吃了|吃什么|天气|下雨|冷不冷|热不热")
LIFE_MATCH_GRAMS = 2      # 不是在问她的时候：近况里的一件事和这几句话至少有几个两字词对得上才带


def life_focus(d: dict | None, text: str, recent: str = "") -> dict | None:
    """挑这一轮要带的近况：对方在问她 → 原样；没问 → 只留和这几句话（含她上一句）对得上的事，不带在哪"""
    if not d or _ASK_LIFE_RE.search(text or ""):
        return d
    q = _grams(f"{text} {recent}")
    keep = [it for it in list(d.get("today") or []) + list(d.get("related") or [])
            if _related(q, it.get("text", "")) >= LIFE_MATCH_GRAMS]
    # 11/06（1491656451 问“这海这么大？走了几天了都”，没带在哪，她答“我还没走到海边呢”）：
    # 对方这句话里提到了她所在的那种地方（海、山、镇……），在哪照样带上（只看对方这句，不看前文，免得又一直带）
    place = d.get("place") if _place_touched(d.get("place"), text) else None
    if not keep and not place:
        return None
    return {"place": place, "today": [], "related": keep}       # 打算（plans）不带：没问到她就别提要走的事


_GEO_CHARS = "海湖河溪岛山林谷原漠镇城村港"


def _place_touched(place: dict | None, text: str) -> bool:
    """在哪的名字里的地形字（海、山、镇……）在这几句话里出现了"""
    name = str((place or {}).get("name") or "")
    return any(c in name and c in text for c in _GEO_CHARS)


def life_hint(d: dict | None) -> str:
    """每轮回消息带的【你的近况】：self_mem.life_for_chat 的结果拼成一段；什么都没有返回空"""
    if not d:
        return ""
    parts = [_life_place(d.get("place"))]
    items = list(d.get("today") or []) + list(d.get("related") or [])
    if items:
        parts.append("你说过的：" + "；".join(_life_item(it) for it in items) + "。")
    parts.append(_life_plans(d.get("plans"), ("还没到日子", "到日子了", "过了日子还没动")))   # 问到她时才走到这里
    body = "".join(p for p in parts if p)
    return f"{LIFE_CHAT_HEAD}\n{body}" if body else ""


def _long_stay(place: dict | None, plans) -> str:
    """11/09 C：现在的地方待了 life_long_stay_days 天以上、也没说过要走（plans 空）→ 让她自己掂量走不走；0 = 不加"""
    days = int(cfg.life_long_stay_days or 0)
    if not days or not place or place.get("status") != "现在" or plans:
        return ""
    n = int(place.get("days_here") or 1)
    return LIFE_LONG_STAY_TEXT if n >= days else ""          # 待了几天上一句已经写了，这里不重复


def life_hint_proactive(d: dict | None) -> str:
    """冒泡、写信、搭话带的【你的近况】：self_mem.life_for_proactive 的结果"""
    if not d:
        return ""
    parts = [_life_place(d.get("place")), _long_stay(d.get("place"), d.get("plans")),
             _life_plans(d.get("plans"), ("到日子了", "过了日子还没动"))]   # 还没到日子的不带，免得天天念叨要走
    if d.get("fresh"):
        parts.append("最近说过的：" + "；".join(_life_item(it) for it in d["fresh"]) + "。")
    if d.get("told_here"):
        parts.append("这里已经听你讲过（这次说点别的，别换个说法再讲同一件事）：" + "；".join(f"{it.get('text', '')}（{it.get('when') or '最近'}）" for it in d["told_here"]) + "。")
    if d.get("told_elsewhere"):
        parts.append(LIFE_ELSEWHERE_HEAD + "；".join(f"{it.get('text', '')}（{it.get('when') or '最近'}）" for it in d["told_elsewhere"]) + "。")
    body = "\n".join(p for p in parts if p)
    return f"{LIFE_PROACTIVE_HEAD}\n{body}" if body else ""
