"""
长期记忆

- 每个人一份档案（按 QQ 号，群聊和私聊共用）：
    impression  她对这个人的总体印象（一句话）
    facts       一条条记忆：内容、类型、重要度、在哪儿知道的（私聊 / 哪个群 / 空间）、哪天记下、最近哪天又聊到
    told        她自己对这个人说过、要记得的事（讲过哪段旅途、答应过什么）
- 每个群一份“往事”：这个群一起聊过、做过的值得记住的事，带日期和在场的人
- 工作方式：每轮对话都先加进“待整理”，攒够一批（默认 8 条）就在后台调用一次模型，
  模型只写“增、改、删”，程序合并进档案，并按对话内容调整好感度。不影响回复速度。
- 遗忘由程序算：能记多少跟着关系走，满了先忘又旧又不重要的；小事、近况过一阵子自己淡掉。
- 回复时：按场合（私聊知道的事不在群里、空间里用）和这句话挑几条，作为一小段提示带给模型。

文件都在 data/memory/ 下：
  users/<QQ号>.json      个人档案
  groups/<群号>.json     群往事 + 昵称→QQ 对照
  pending/<会话>.json    还没整理的旧消息
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import shutil
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from nonebot import logger

from . import budget


_DUP_MARK_RE = re.compile(r"#\d{1,2}$")     # 群里重名的记号（“小明#2”）
_PID = itertools.count(time.time_ns())      # 待整理消息的编号（启动时从当前时间起，重启后也不会重复）


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # 文件坏了（比如写到一半断电）：改名留底，不要直接当成空档案再存回去
        bad = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            path.replace(bad)
            logger.warning(f"记忆文件读不出来，已改名留底：{bad.name}（{e}）")
        except OSError:
            logger.warning(f"记忆文件读不出来：{path}（{e}）")
        return default


def _write(path: Path, data) -> None:
    """先写临时文件再替换：写到一半被打断，原文件也还是完整的"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ 记忆条目的小工具
KINDS = ("身份", "喜好", "习惯", "近况", "计划", "约定", "梗", "经历")
FACT_CAP_BY_TIER = {"disliked": 8, "stranger": 8, "friend": 12, "acquaintance": 20, "close": 30}
# 每轮最多带几条：（重要的几条，和这句话相关的几条）
CONTEXT_LIMITS = {"disliked": (1, 2), "stranger": (1, 2), "friend": (2, 3), "acquaintance": (3, 5), "close": (3, 7)}
FORGET_DAYS = {1: 30, 2: 90}                # 这么多天没再聊到就忘（重要度 3 不自动忘）
RECENT_DAYS = 14                            # “近况”过这么多天就不算近况了
PLAN_GRACE_DAYS = 30                        # 计划到期后再留这么多天
MAX_TOLD = 8
# 【可以问问】：对方隔了一阵才来时，提醒她一件可以问后续的事（计划到时间了、之前说过的近况）
ASK_TIERS = ("acquaintance", "close")      # 只给熟人、很熟（不熟的人被问“你上次说的考试”会觉得被盯着）
ASK_GAP_HOURS = 6                          # 对方隔了这么久才来找她（写信本来就隔了一天以上）
ASK_RECENT_MIN_DAYS = 3                    # 近况要过了这么多天才问“后来怎么样了”
SCOPE_NAMES = {"private": "私聊", "qzone": "空间", "public": "群里", "legacy": "未分类"}

_PUNCT_RE = re.compile(r"[\s，。、！？!?,.；;：:“”\"'‘’（）()【】\[\]~～…—\-]+")
_CJK_RE = re.compile(r"[一-鿿]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")
_MD_RE = re.compile(r"^\s*(\d{1,2})月(\d{1,2})日[：:，, ]*")
# 太常见、比不出相关不相关的两字组合
_STOP_GRAMS = {
    "伊蕾", "蕾娜", "喜欢", "什么", "一个", "这个", "那个", "没有", "自己", "知道", "觉得", "就是", "还是", "不是",
    "可以", "我们", "你们", "他们", "问她", "她说", "被她", "说过", "问过", "今天", "时候", "东西", "一下", "怎么",
    "有点", "真的", "但是", "因为", "所以", "然后", "已经", "一直", "经常", "常常", "觉得", "为什", "么样",
}


def _today_str() -> str:
    return date.today().isoformat()


def _days_since(d: str | None) -> float:
    if not d:
        return 0.0
    try:
        return (date.today() - date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return 0.0


def _norm(s: str) -> str:
    return _PUNCT_RE.sub("", str(s)).lower()


_QUOTE_RE = re.compile(r"(“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"|‘[^’]*’)")
_HER_RE = re.compile(r"伊蕾娜|她(?!们)|我(?!们)")


def _to_you(t: str) -> str:
    """给她看的记忆里，指伊蕾娜自己的“伊蕾娜 / 她 / 我”都换成“你”（引号里的原话不动）。
    条目默认说的是对方本人，“她”在这里几乎都指伊蕾娜；对方是女生时不换就会把“请她吃可颂”读反"""
    if not t:
        return t
    return "".join(seg if i % 2 else _HER_RE.sub("你", seg).replace("我们", "你们")
                   for i, seg in enumerate(_QUOTE_RE.split(str(t))))


def _grams(s: str) -> set[str]:
    out: set[str] = set()
    for run in _CJK_RE.findall(str(s)):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    out.update(w.lower() for w in _WORD_RE.findall(str(s)))
    return out - _STOP_GRAMS


def _clean_tags(v, text: str = "") -> list[str]:
    """模型给的关键词：最多 4 个、每个不超过 6 个字，去掉重复的和正文里本来就有的"""
    out = []
    for t in _as_list(v):
        t = _clean_text(t, 6)
        if t and t not in out and t not in str(text):
            out.append(t)
    return out[:4]


# 伊蕾娜自己爱提的话题：档案里这类条目一多，她每轮都会扯到上面（10/03 群鲨鱼：20 条里 7 条和面包有关）。
# 给她看时，对方这几句没提到的话题最多带 1 条，提到了最多 2 条
HER_TOPICS = {
    "面包": ("面包", "可颂", "吐司", "甜点", "奶油", "烘焙", "法棍"),
    "钱": ("钱", "金币", "报酬", "工钱", "付账"),
    "蘑菇": ("蘑菇", "香菇", "菌子"),
}


_ARROW_YOU_RE = re.compile(r"(→ (?:[^】]*、)?)你(?=[、】（])")


def _her_topics(text: str) -> set[str]:
    t = str(text or "")
    return {k for k, words in HER_TOPICS.items() if any(w in t for w in words)}


def _cap_topics(items: list, text_of, talked: set[str]) -> list:
    """按 items 的先后（重要的在前）留：同一个她爱提的话题，没聊到的留 1 条，聊到的留 2 条"""
    used: dict[str, int] = defaultdict(int)
    out = []
    for it in items:
        tops = _her_topics(text_of(it))
        if any(used[t] >= (2 if t in talked else 1) for t in tops):
            continue
        for t in tops:
            used[t] += 1
        out.append(it)
    return out


def _fact_words(f: dict) -> str:
    """挑相关记忆时拿来比的字：正文 + 关键词（“期末”也能对上“下周要考试”）"""
    return f"{f.get('text', '')} {' '.join(str(t) for t in f.get('tags') or [])}"


def _related(query_grams: set[str], text: str) -> int:
    return len(query_grams & _grams(text)) if query_grams else 0


def _parse_day(v) -> str | None:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _md_to_date(md_month: int, md_day: int) -> str | None:
    today = date.today()
    try:
        d = date(today.year, md_month, md_day)
    except ValueError:
        return None
    if d > today + timedelta(days=1):            # “12月30日”在 1 月读到：是去年的
        d = date(today.year - 1, md_month, md_day)
    return d.isoformat()


def _md(d: str | None) -> str:
    try:
        x = date.fromisoformat(str(d)[:10])
        return f"{x.month}月{x.day}日"
    except (TypeError, ValueError):
        return ""


def _ago(d: str | None) -> str:
    n = int(_days_since(d))
    if n <= 0:
        return "今天"
    if n == 1:
        return "昨天"
    if n < 7:
        return f"{n} 天前"
    if n < 30:
        return f"{n // 7} 周前"
    return f"{n // 30} 个多月前" if n < 365 else "很久以前"


def scope_of(key: str) -> str:
    """这批聊天来自哪里：私聊 private｜群 group:群号｜空间评论 qzone（公开的）"""
    if key.startswith("group_"):
        return f"group:{key.split('_')[1]}"
    if key.startswith("qzone_"):
        return "qzone"
    return "private"


def scope_ok(scope: str, place: str, gid: int | None) -> bool:
    """在 place 说话时，能不能用在 scope 知道的事。
    私聊：都能用｜群：只用这个群、空间里知道的，以及迁移时标成“群里说过”的｜空间（公开）：私聊知道的不用"""
    scope = scope or "legacy"
    if place == "private":
        return True
    if scope in ("private", "legacy"):
        return False
    if place == "qzone":
        return True
    return scope in ("qzone", "public") or scope == f"group:{gid}"


def _place_of(scope: str) -> tuple[str, int | None]:
    """整理的这批聊天在哪儿（scope_of 的结果）→ scope_ok 要的 (place, gid)"""
    if str(scope).startswith("group:"):
        return "group", _int(str(scope).split(":", 1)[1])
    return ("qzone", None) if scope == "qzone" else ("private", None)


_WRAP_QUOTES = (("“", "”"), ('"', '"'), ("'", "'"), ("「", "」"))


def _clean_text(v, n: int = 40) -> str:
    """去掉首尾空白；整句被一对引号包着才去掉引号（句子里、句尾的引号要留着，不然“这还差不多”会少半个）"""
    t = str(v or "").strip()
    for a, b in _WRAP_QUOTES:
        inner = t[len(a):-len(b)] if len(t) >= 2 else ""
        if t.startswith(a) and t.endswith(b) and len(t) >= 2 and a not in inner and b not in inner:
            t = t[len(a):-len(b)].strip()
            break
    return t[:n].strip()


def _split_date(text: str) -> tuple[str, str | None]:
    """“9月29日问她……” → （“问她……”, 那天的日期）：日期由程序记，不留在正文里"""
    m = _MD_RE.match(text)
    if not m:
        return text, None
    rest = text[m.end():].strip()
    return (rest or text), _md_to_date(int(m.group(1)), int(m.group(2)))


def _similar(a: str, b: str) -> bool:
    """两条说的基本是一回事（字面重合很多）"""
    ga, gb = _grams(a) | set(_norm(a)), _grams(b) | set(_norm(b))
    if not ga or not gb:
        return False
    return len(ga & gb) / min(len(ga), len(gb)) >= 0.7


def _weight(v, default: int = 2) -> int:
    try:
        return max(1, min(3, int(v)))
    except (TypeError, ValueError):
        return default


def _as_list(v) -> list:
    """模型该给列表却给了一个字符串 / 数字：当成只有一项（不要一个字一个字拆开）"""
    if isinstance(v, list):
        return v
    if v is None or v == "" or isinstance(v, bool):
        return []
    return [v] if isinstance(v, (str, int, float, dict)) else []


def _int(v, default=None):
    try:
        return int(str(v).strip().lstrip("#"))
    except (TypeError, ValueError):
        return default


def _item_text(x, n: int = 40) -> str:
    """列表里的一项：字符串，或者 {"text": ...}"""
    return _clean_text(x.get("text") if isinstance(x, dict) else x, n)


def _keep_score(f: dict) -> float:
    """满了先忘谁：重要度 × 10 − 多少天没再聊到（群往事按发生的日期）"""
    return _weight(f.get("weight")) * 10 - _days_since(f.get("seen") or f.get("since") or f.get("date"))


def _expired(f: dict) -> bool:
    if f.get("scope") == "legacy":               # 还没迁移的旧条目：不知道重要不重要，先不忘
        return False
    age = _days_since(f.get("seen") or f.get("since"))
    kind, w = f.get("kind"), _weight(f.get("weight"))
    if kind == "近况" and age > RECENT_DAYS:
        return True
    if kind == "计划" and f.get("due") and _days_since(f["due"]) > PLAN_GRACE_DAYS:
        return True
    return w in FORGET_DAYS and age > FORGET_DAYS[w]


def _trim_facts(facts: list[dict], cap: int) -> list[dict]:
    """个人档案：还没迁移的旧条目先不算进上限、也不淘汰（迁移时再一起算），其余按 _trim"""
    legacy = [f for f in facts if f.get("scope") == "legacy"]
    if not legacy:
        return _trim(facts, cap)
    kept = _trim([f for f in facts if f.get("scope") != "legacy"], cap)
    return [f for f in facts if f.get("scope") == "legacy" or any(f is k for k in kept)]


def _trim(items: list[dict], cap: int, protect_top: bool = True) -> list[dict]:
    """超出上限：重要度 3 的不挤掉（除非多得离谱），其余按保留分从低到高删；剩下的保持原来的顺序"""
    if len(items) <= cap:
        return items
    idx = list(range(len(items)))
    top = [i for i in idx if protect_top and _weight(items[i].get("weight")) >= 3]
    rest = sorted((i for i in idx if i not in top), key=lambda i: (-_keep_score(items[i]), -i))   # 一样的分：留新记下的
    room = max(0, cap - len(top))
    keep = set(rest[:room])
    if len(top) > cap + 5:                       # 重要的也多得离谱了：留最近聊到的
        top = sorted(top, key=lambda i: (-_keep_score(items[i]), -i))[:cap + 5]
    keep.update(top)
    return [items[i] for i in idx if i in keep]


SUMMARIZE_PROMPT = """你是“伊蕾娜”（一个 QQ 角色扮演机器人）的记忆整理员。用户消息里会给你今天的日期、已有的人物档案、群往事和她最近的一段聊天记录（“伊蕾娜（她自己）：”开头的是她自己说的话，其余是别人说的）。
请像一个真人回想刚才的聊天那样，更新她对每个人的记忆，并评估好感变化。已有档案里每条都有编号 #id，**只写有变化的部分，不要把整份档案重写一遍**。

一、记什么
1. 记这个人本身：身份（学生、上夜班……）、喜好、习惯（老在半夜来找她）、近况（最近很累）、计划（下周考试）、和伊蕾娜之间的约定、梗（给她起的外号、反复玩的段子）、重要的经历或互动（第一次给她画画像）。
2. 不记聊天流水账：不要写“问伊蕾娜……，被伊蕾娜回……”，也不要在句尾带“被她回……”“被吐槽……”；一次性的随口一问、寒暄客套不记。她怎么回的一般不用记，除非成了他们之间的梗或约定。例：“想用面包收买伊蕾娜讲故事，被回今天已吃过、不收”→“爱拿面包收买伊蕾娜”。
   和已有某条说的是同一回事，就用 update 改那条或者 touch，不要再 add 一条相近的。
   同一个话题（比如都和请伊蕾娜吃面包有关）已经有条目的，新进展用 update 合进那条，不要另加一条；档案里同一话题已经有好几条的，顺手合并成一两条（update 其中一条写合并后的内容，其余 drop）。
   分清是谁说的、谁做的：“伊蕾娜（她自己）：”开头的是伊蕾娜说的。她自己的经历、喜恶、抱怨、许下的事，不是对方的事，不要写进对方的条目，主语也不要换成对方。她答应对方的事才写进 told。
   例：伊蕾娜说“这家旅馆的枕头太软了”“下次还你一个硬一点的，当上次面包的回礼”→ 对方的条目里什么也不加（嫌枕头软的是她）；told 写“说过要送他一个硬枕头当面包的回礼”。
3. 每条不超过 30 字，写这个人（“是学生”“喜欢刚出炉的可颂”），不写日期，日期由程序记。不要把对方的昵称、群名片当成一条记（程序已经知道昵称；昵称里的字眼也不代表他真是那样的人）。
4. 类型 kind 只能是：身份、喜好、习惯、近况、计划、约定、梗、经历。
5. 重要度 weight：1 = 顺带一提的小事；2 = 值得记住；3 = 约定、身份、希望被怎么称呼、对她很重要的事。
   关键词 tags：每条写 2～4 个近义或相关的词，以后对方换个说法也能想起来（例如“下周要考试”写 ["期末", "学校", "复习"]；“养了只橘猫”写 ["猫", "宠物", "喵"]）。每个词不超过 6 个字，不要重复正文里已有的词。
6. 计划类看得出大概时间的，写 due（YYYY-MM-DD，按今天的日期推算，比如“下周考试”）；看不出就不写。
7. 不要记：密码、手机号、身份证号、住址、银行卡等隐私；健康、疾病、政治、宗教等敏感信息；伊蕾娜自己讲的小说故事的内容。
   对方说想轻生、不想活了、想伤害自己：不记原话和细节，最多记一条 kind=近况、weight=1 的“那阵子心情很低落”。
8. 只根据聊天记录，不猜测、不编造。人和 QQ 号的对应以档案列表为准，列表里没有的人不要写。

二、怎么改（每个人）
- add：新记下的，每条写 text、kind、weight（计划可加 due）。和已有某条意思一样的不要再加，写进 touch。
- update：新信息和已有某条对不上、或者有了新进展（“要考试”→“考完了，考得不错”），写那一条的 id 和新的 text（可以顺便改 kind、weight），不要另加一条。
  约定、计划兑现了、取消了、变了，一定要改：例如“约好请伊蕾娜吃面包”，这段里他已经递了面包 → update 成“兑现过请伊蕾娜吃面包的约定”（kind 改成经历）；她又说还欠一顿，就写成“请伊蕾娜吃过一次面包，她说还欠一顿”。别让已经兑现的旧约定一直挂着。
- drop：已经不对、或者对方要她忘掉的旧条目 id。不要因为条数多就删，淘汰旧的由程序来做。
- touch：这段聊天里又聊到了、内容没变的旧条目 id。
- 已有条目后面没有“词：”的，这次聊到了，顺手用 update 补上 tags。
- 档案里标着“未分类”的旧条目：这次聊到了的，顺手用 update 补上 kind 和 weight；写成了“被伊蕾娜回……”这种流水账的，改写成关于这个人的话，或者 drop。
- told：伊蕾娜自己在这段里对这个人说过、以后要记得的事，只有三种：① 讲过哪段旅途经历（只写是哪段，例如“讲过雪之国的事”）；② 答应过他什么、和他约过什么；③ 对他明确表过的态度（例如“说过别叫伊蕾娜宝宝”）。她随口的回答、吐槽、拒绝、调侃、纠正、推荐都**不算**（例如“回他对动物没什么偏好”“调侃他话多”“纠正过自己的发色”“说过自己不是占卜摊”“推荐过各地面包店”“不肯透露画像是什么时候的”都不要写）。她向他讨东西、催他兑现、反复提的要求也不算（例如“又催他兑现刚出炉的可颂”）；和“她跟他说过”里已有的是同一件事，也不要再写。每条不超过 25 字，没有就不写，大多数时候都没有。
- told_update：“她跟他说过”里已经兑现、取消、说反了的，不删，改成现在的状态：old 照抄那条原文，text 写新的（例如“答应过请他吃面包”→“答应请他吃的面包已经请过了”；说反了的写清到底谁请谁）。
- impression：伊蕾娜对这个人的总体印象，一句话，不超过 40 字，用她的口吻（例如“嘴甜又黏人，偶尔嘴硬但会认错”）。群里也会用到，所以只写性格和相处方式，不写私事（倾诉过的烦恼、情绪、告白），也不提伊蕾娜自己爱的东西（面包、钱这些，写了她每轮都会扯到上面）。还没有印象、或者印象变了才写；没变就不写这一项。
- 条目、told 里提到伊蕾娜时写“伊蕾娜”，别用“她”“我”代替（对方是女生时会分不清谁请谁）。
- 印象和记忆条目里，别把伊蕾娜自己的喜好（面包、钱、讨厌蘑菇这些）写成对方的特点，除非对方自己反复提起；写了她每次看到都会想扯到面包上。
- 这段记录里没有这个人的新内容：add、update、drop 都留空，照样给 affection。

三、群往事 group_events（私聊、空间评论时 add 留空）
- 这个群里大家一起聊过、玩过、值得记住的事，每条不超过 40 字，不写日期（程序会记）。
- who：在场参与的人的 QQ 号（必须是档案列表里的人）。
- 和已有往事重复的不要加；已经不对的写进 drop（写 #id）。

四、好感变化 affection：站在伊蕾娜（自恋、爱钱、爱面包、讨厌蘑菇、嘴毒但重情、讨厌被冒犯）的角度，评价这段记录里每个人给她的感受，给一个整数。
   先看档案里标的【关系】——同样的话，关系不同感受完全不同。
   再分清是“玩笑”还是“恶意”：
     - 玩笑：调侃、互损、斗嘴、起外号、拿她的自恋 / 爱钱 / 贪吃 / 身材开玩笑、故意逗她生气。越熟越是日常，普通朋友扣得少，熟人、很熟之间基本不扣。
     - 恶意：辱骂、人身攻击、性骚扰、明知她在意还反复戳痛处、带着恶意的嘲讽和贬低。恶意不看关系，一律按陌生人的标准扣；很熟的人这样做，她反而更受伤，可以扣得更重。
   ● 陌生人（基准）：
     - +1～+5：聊得投机、有趣、尊重她、真诚关心她、夸得她心里舒服、陪她聊她感兴趣的事
     - 0：普通寒暄、没什么感觉
     - -1～-5：无聊纠缠、刷屏、硬要她做不想做的事、一上来就告白/叫老婆/强加关系、提蘑菇之类让她烦的事
     - -6～-15：辱骂、人身攻击、性骚扰、恶意冒犯
   ● 普通朋友：聊过一些、印象不坏。轻度调侃、开玩笑算正常，0～-1；告白、强加关系还是有点冒犯，-1～-3；恶意照样按基准扣
   ● 熟人：调侃、互损、开玩笑算正常打闹，基本不扣（0，过火了最多 -1）；告白、撒娇不算冒犯，看她心情 0～+2；恶意照样按基准扣
   ● 很熟：互损、开玩笑一律不扣（0），这是他们之间的相处方式；真心关心、陪伴、记得她的喜好可以多加 +2～+5；恶意按基准扣，可以更重
   ● 讨厌：她本来就烦这个人，冒犯按基准再重一些；想加分很难，只有特别真诚、明显改过的表现才给 +1～+2
   ● 倾诉不是冒犯：对方难过、倾诉烦恼、说丧气话，甚至说不想活了、要离开这个世界，这些话本身不扣分；真诚地说出来、没有别的恶意，就当作是信任她，可以 0～+2。
   ● 卖惨算纠缠：拿难过当手段——逼她回应、逼她答应告白、一遍遍说“我要走了”来博关注——按“纠缠”扣（-1～-5）；扣的是纠缠本身，不是因为他说了难过的话。
   ● 身材梗不在这里算分：拿她的身材开玩笑（平胸、飞机场、洗衣板等）已经由系统当场按关系扣过，这里不因此再扣；只有同时还有别的恶意（辱骂、骚扰等）才按那部分扣。
   ● 送东西不在这里算分：说送面包、给钱（包括「[给面包]」「[给钱]」「转账」这类写法）已经由系统单独算过，这里不因此加分；付给她合理的报酬不加不减；拿钱引诱、无缘无故撒钱、用她不认识的钱糊弄她，不加分。
   同时写一句不超过 20 字的理由 reason，并标出类别 affection_kind：“正常”（没扣分，或者只是玩笑过火）、“纠缠”（烦人但没有恶意：纠缠告白、刷屏、问个没完）、“恶意”（辱骂、人身攻击、性骚扰、恶意冒犯）。
   同一件烦人的事，档案里已经记着“又来了”，这次也只按这段记录本身的程度扣，不要因为“又来了”越扣越重。

五、性别 gender_guess：**只在这段里有明确证据时**才写“男”或“女”，在 gender_evidence 里写出证据（不超过 20 字），在 gender_evidence_kind 里写证据的种类：
   - “自称”：对方自己说的（“我一个女生”“本人男”）。自己说的也可能是骗她的，照实记下就行，程序会结合别的证据判断；
   - “别人称呼”：别的群友认真地用“他 / 她”“哥 / 姐”称呼他（开玩笑的称呼不算，比如管男生叫“老婆”）；
   - “昵称”：昵称一看就知道；
   - “拆穿”：别人说他在骗人（“他是男的你别信”），gender_guess 写别人说的那个性别；
   - “承认骗人”：他自己承认之前说的性别是假的，gender_guess 写他现在承认的那个。
   说话方式、聊的话题（游戏、化妆、粗鲁、撒娇、叫她老婆）都**不算**证据。没有明确证据就写“不确定”，大多数时候都是“不确定”；不要因为档案里以前记过就照抄。

六、今日见闻 today_moments：伊蕾娜晚上会写旅行日记（会公开给很多人看）。从这段记录里挑 0～2 件她会想写进日记的事：有趣的话题、有人关心她、有人惹她烦、好笑或让她在意的事。寒暄、没内容的闲聊不算，没有就返回空列表。每件写：
   - qq：主要相关的那个人（必须是档案列表里的人）
   - event：只写话题层面，不超过 25 字，不写名字、QQ 号，不写具体的私事细节（例如写“聊了工作上的烦心事”，不写“被老板骂了”）
   - mood：她的感受，一两个词（开心、得意、无语、烦、在意、好笑……）
   - keywords：2～4 个关键词，用来给日记挑配图（例如 面包、下雨、读书、猫）

只输出 JSON，格式（没有的项可以省略或留空）：
{"people": [{"qq": 123456,
   "add": [{"text": "……", "kind": "喜好", "weight": 2, "tags": ["……", "……"]}],
   "update": [{"id": 3, "text": "……"}], "drop": [5], "touch": [7],
   "told": ["……"], "told_update": [{"old": "……", "text": "……"}], "impression": "……",
   "affection": 2, "affection_kind": "正常", "reason": "……", "gender_guess": "不确定", "gender_evidence": "", "gender_evidence_kind": ""}],
 "group_events": {"add": [{"text": "……", "who": [123456]}], "drop": []},
 "today_moments": [{"qq": 123456, "event": "……", "mood": "……", "keywords": ["……"]}]}"""

SUMMARIZE_INPUT = """今天是 {today}。这段聊天来自：{where}。

【已有的人物档案】
{profiles}

【已有的群往事】
{events}

【聊天记录】
{transcript}"""


MIGRATE_PROMPT = """你是“伊蕾娜”（一个 QQ 角色扮演机器人）的记忆整理员。她的记忆格式升级了：以前每个人只有一串句子，现在每条要标上类型和重要度。用户消息里是一个人的旧档案，请整理成新格式。只输出 JSON。

1. 把“问伊蕾娜……，被伊蕾娜回……”这种聊天流水账，改写成关于这个人的话，或者直接去掉；句尾也不要留“被她回……”“被吐槽……”。一次性的随口一问、寒暄去掉；意思重复的合并。
   例：“想用面包收买伊蕾娜讲故事，被回今天已吃过、不收”→“爱拿面包收买伊蕾娜”；“问伊蕾娜喜不喜欢草泥马，被回对动物没什么偏好”→去掉。
2. told 只放三种：伊蕾娜讲过的旅途经历（只写是哪段）、答应过他的事、对他明确表过的态度（例如“说过别叫伊蕾娜宝宝”）。她随口的回答、吐槽、拒绝、调侃、纠正、推荐不算（例如“纠正过自己的发色”“说过自己不是占卜摊”“推荐过各地面包店”“说过不熟群鲨鱼”都不要），不要放进来。
   不要把对方的昵称、群名片当成一条记（程序已经知道昵称；昵称里的字眼也不代表他真是那样的人）。每条不超过 25 字，每条标 private（像是私下说的写 true）。没有就留空，大多数人都没有。
3. 每条 facts 写：text（不超过 30 字，不写日期）、kind（只能是：身份、喜好、习惯、近况、计划、约定、梗、经历）、weight（1 = 小事，2 = 值得记住，3 = 约定、身份、称呼、对她很重要的事）、private（像私事、只适合私下说的写 true：情绪、烦恼、家里的事、倾诉过的心事、告白；喜好、外号、公开玩的梗写 false）、date（原句里写了“某月某日”的，换成 YYYY-MM-DD，年份按今天推算；没写就不写）。
4. 对方说过想轻生、不想活了之类的原话和细节，一律去掉，最多留一条 kind=近况、weight=1、private=true 的“那阵子心情很低落”。
5. 另写一句 impression：伊蕾娜对这个人的总体印象，不超过 40 字，用她的口吻。群里也会用到，所以只写性格和相处方式，不写私事（倾诉过的烦恼、情绪、告白）。
6. 只根据旧档案，不编造。

格式：{"impression": "……", "facts": [{"text": "……", "kind": "喜好", "weight": 2, "private": false}], "told": [{"text": "……", "private": false}]}"""

MIGRATE_INPUT = """今天是 {today}。
这个人：{name}（她和他的关系：{tier}）
旧档案（{n} 条）：
{facts}"""


class LongTermMemory:
    def __init__(self, root: Path, client, model: str, *, batch: int = 8,
                 max_facts: int = 12, max_events: int = 8, enabled: bool = True, defer=None):
        self.root = root
        self.client = client
        self.model = model
        self.batch = batch
        self.group_batch = batch                 # 群里攒几条整理一次（群的短期记忆只有 10 条，要比私聊整理得勤）
        self.max_facts = max_facts              # 没按关系设上限时的兜底
        self.max_events = max_events
        self.enabled = enabled
        self.defer = defer or (lambda: False)   # 返回 True 时先不整理（比如 API 高峰时段）
        self.facts_by_tier = dict(FACT_CAP_BY_TIER)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)   # QQ -> 整理这个人档案时的锁
        self._tasks: set[asyncio.Task] = set()
        self._running: set[str] = set()         # 正在整理的会话，避免重复开任务
        self.group_gap = 0.0                     # 同一个群两次整理至少隔多少秒（攒得太多时不等）
        self._started: dict[str, float] = {}    # 会话 -> 上次开始整理的时间
        self._names: dict[int, dict[str, int]] = {}   # 群号 -> {昵称: QQ}
        self._fails: dict[str, int] = {}          # 会话 -> 连续整理失败几次
        self._retry_at: dict[str, float] = {}     # 会话 -> 失败后，这个时间之前先不再试
        self._migrating = False
        self._migrate_fail: dict[int, float] = {}  # QQ -> 迁移失败后，这个时间之前先不再试
        # 整理出“今日见闻”后交给谁保存（QQ 空间日记用）：moment_sink(会话, 见闻列表, {QQ: 昵称})
        self.moment_sink = None
        try:
            self.backup_once()                     # 9/30 换了档案格式：开机先把旧格式的原样备份一份
        except Exception as e:  # noqa: BLE001
            logger.warning(f"长期记忆：备份旧档案出错：{e}")

    migrate_enabled: bool = True               # 旧格式档案在后台慢慢迁移（每次补漏时迁几个人）
    migrate_per_round: int = 3

    # -------------------------------------------------------------- 存取
    def _user_path(self, qq: int) -> Path:
        return self.root / "users" / f"{qq}.json"

    def _group_path(self, gid: int) -> Path:
        return self.root / "groups" / f"{gid}.json"

    def _group_ids(self) -> list[int]:
        d = self.root / "groups"
        return sorted(g for g in (_int(p.stem) for p in d.glob("*.json")) if g is not None) if d.is_dir() else []

    def _pending_path(self, key: str) -> Path:
        return self.root / "pending" / f"{key}.json"

    @staticmethod
    def _upgrade_facts(prof: dict) -> None:
        """旧档案的 facts 是一串句子：先换成条目，标成“未分类”（只在私聊用），等后台迁移补上类型、场合"""
        facts = [f for f in (prof.get("facts") or []) if isinstance(f, (str, dict))]
        if not any(isinstance(f, str) or _int(f.get("id")) is None for f in facts):
            return
        try:
            since = datetime.fromtimestamp(float(prof["updated"])).date().isoformat() if prof.get("updated") else _today_str()
        except (TypeError, ValueError, OSError):
            since = _today_str()
        nid = max([_int(prof.get("next_id"), 1)] + [(_int(f.get("id"), 0) or 0) + 1 for f in facts if isinstance(f, dict)])
        out = []
        for f in facts:
            if isinstance(f, dict):
                if _int(f.get("id")) is None:
                    f["id"], nid = nid, nid + 1
                out.append(f)
                continue
            text = str(f).strip()
            if not text:
                continue
            m = _MD_RE.match(text)
            day = _md_to_date(int(m.group(1)), int(m.group(2))) if m else None
            out.append({"id": nid, "text": text, "kind": "", "weight": 2, "scope": "legacy",
                        "since": day or since, "seen": day or since})
            nid += 1
        prof["facts"], prof["next_id"] = out, nid

    def get_user(self, qq: int) -> dict:
        prof = _read(self._user_path(qq), None)
        if prof is None:
            start = float(self.affection_cfg.get("start", 0))
            return {"qq": qq, "name": "", "facts": [], "score": start, "score_v": self.SCORE_VERSION,
                    "gender_guess_v": self.GENDER_GUESS_V}
        if prof.get("score_v") != self.SCORE_VERSION:      # 旧档案：分数按新范围换算一次（存盘时带上版本号）
            if "score" in prof:
                prof["score"] = self.migrate_score(prof["score"])
            prof["score_v"] = self.SCORE_VERSION
        if prof.get("gender_guess_v") != self.GENDER_GUESS_V:   # 旧规则猜的性别不算数，按新规则重新猜
            prof.pop("gender_guess", None)
            prof.pop("gender_votes", None)
            prof.pop("gender_log", None)
            prof.pop("gender_doubt", None)
            prof["gender_guess_v"] = self.GENDER_GUESS_V
        self._upgrade_facts(prof)
        return prof

    def save_user(self, prof: dict) -> None:
        prof["updated"] = int(time.time())
        prof.setdefault("gender_guess_v", self.GENDER_GUESS_V)
        if prof.get("name"):                      # 群里重名时聊天记录里写成“小明#2”，档案里只存原名
            prof["name"] = _DUP_MARK_RE.sub("", str(prof["name"])).strip()
        self._upgrade_facts(prof)
        _write(self._user_path(int(prof["qq"])), prof)

    @staticmethod
    def fact_texts(prof: dict) -> list[str]:
        return [f["text"] if isinstance(f, dict) else str(f) for f in prof.get("facts") or []]

    def forget_user(self, qq: int) -> bool:
        """删掉长期记忆里的印象（熟悉程度、好感保留）"""
        prof = self.get_user(qq)
        existed = bool(prof.get("facts") or prof.get("impression") or prof.get("told"))
        prof["facts"], prof["told"] = [], []
        prof.pop("impression", None)
        prof["mem_gen"] = int(prof.get("mem_gen", 0)) + 1     # 正在进行的整理、迁移看到这个，就不把旧的写回来
        self.save_user(prof)
        return existed

    def edit_impression(self, qq: int, text: str) -> tuple[str, str] | None:
        """/改记忆 @某人 印象 新内容：改她对这个人的印象（写“无”就清掉）。返回 (改前, 改后)"""
        new = _clean_text(text, 40)
        if not new:
            return None
        prof = self.get_user(qq)
        old = prof.get("impression") or "（没有）"
        if new in ("无", "没有", "清空"):
            prof.pop("impression", None)
            new = "（没有）"
        else:
            prof["impression"] = new
        prof["mem_gen"] = int(prof.get("mem_gen", 0)) + 1
        self.save_user(prof)
        return old, new

    def edit_fact(self, qq: int, n: int, text: str) -> tuple[str, str] | None:
        """把第 n 条（按 /记忆 里的序号，“她说过”接着往下编）改成 text；重要度、在哪儿知道的不变。
        text 开头写了类型再空一格（“经历 兑现过……”）就连类型一起换，不写类型不变。
        返回 (改前, 改后)，改了类型的写成“（约定→经历）……”；没有这一条返回 None"""
        kind = None
        m = re.match(r"\s*(" + "|".join(KINDS) + r")\s*[\s:：]\s*(.+)$", str(text or ""), re.S)
        if m:
            kind, text = m.group(1), m.group(2)
        new = _clean_text(text, 40)
        prof = self.get_user(qq)
        facts = prof.get("facts") or []
        told = [t for t in prof.get("told") or [] if isinstance(t, dict)]
        if not new:
            return None
        if 1 <= n <= len(facts) and isinstance(facts[n - 1], dict):
            f = facts[n - 1]
            old = f.get("text", "")
            f["text"], f["seen"] = new, _today_str()
            f.pop("offered", None)
            f["tags"] = _clean_tags(f.get("tags"), new)
            if kind and kind != f.get("kind"):
                old = f"（{f.get('kind') or '未分类'}→{kind}）{old}"
                f["kind"] = kind
                if kind != "计划":
                    f.pop("due", None)
        elif len(facts) < n <= len(facts) + len(told):
            t = told[n - len(facts) - 1]
            old = t.get("text", "")
            t["text"], t["date"] = new, _today_str()
            prof["told"] = told
        else:
            return None
        prof["mem_gen"] = int(prof.get("mem_gen", 0)) + 1   # 正在整理的那次不会把旧的写回来
        self.save_user(prof)
        return old, new

    def forget_fact(self, qq: int, n: int) -> str | None:
        """删掉第 n 条（按 /记忆 里显示的序号）；返回删掉的内容"""
        prof = self.get_user(qq)
        facts = prof.get("facts") or []
        told = prof.get("told") or []
        if 1 <= n <= len(facts):
            gone = facts.pop(n - 1)
        elif len(facts) < n <= len(facts) + len(told):   # “她说过”接着条目往下编号
            gone = told.pop(n - len(facts) - 1)
            prof["told"] = told
        else:
            return None
        prof["mem_gen"] = int(prof.get("mem_gen", 0)) + 1
        self.save_user(prof)
        return gone.get("text", "") if isinstance(gone, dict) else str(gone)

    @staticmethod
    def _upgrade_events(g: dict) -> None:
        evs = [e for e in (g.get("events") or []) if isinstance(e, (str, dict))]
        if not any(isinstance(e, str) or _int(e.get("id")) is None for e in evs):
            return
        nid = max([_int(g.get("next_id"), 1)] + [(_int(e.get("id"), 0) or 0) + 1 for e in evs if isinstance(e, dict)])
        out = []
        for e in evs:
            if isinstance(e, dict):
                if _int(e.get("id")) is None:
                    e["id"], nid = nid, nid + 1
                out.append(e)
                continue
            text = str(e).strip()
            m = _MD_RE.match(text)
            day = _md_to_date(int(m.group(1)), int(m.group(2))) if m else None
            if m:
                text = text[m.end():].strip()
            if text:
                out.append({"id": nid, "text": text, "date": day or "", "who": [], "weight": 2})
                nid += 1
        g["events"], g["next_id"] = out, nid

    def get_group(self, gid: int) -> dict:
        g = _read(self._group_path(gid), {"gid": gid, "events": [], "names": {}})
        self._upgrade_events(g)
        return g

    def save_group(self, g: dict) -> None:
        self._upgrade_events(g)
        _write(self._group_path(int(g["gid"])), g)

    @staticmethod
    def event_texts(g: dict) -> list[str]:
        return [e["text"] if isinstance(e, dict) else str(e) for e in g.get("events") or []]

    def forget_group(self, gid: int) -> bool:
        g = self.get_group(gid)
        had = bool(g.get("events"))
        g["events"] = []
        self.save_group(g)
        return had

    def note_name(self, gid: int, qq: int, name: str) -> None:
        """记下群里 昵称→QQ 的对应，整理记忆时用来认人。每人只留最近 3 个昵称；有变化就马上存盘（整理一直失败也不丢）"""
        if not name:
            return
        m = self._names.setdefault(gid, self.get_group(gid).get("names", {}))
        if m.get(name) == qq:
            return
        m.pop(name, None)
        m[name] = qq                              # 放到最后 = 最新
        mine = [n for n, u in m.items() if u == qq]
        for n in mine[:-3]:
            del m[n]
        try:
            g = self.get_group(gid)
            g["names"] = m
            self.save_group(g)
        except OSError as e:
            logger.warning(f"群昵称对照表存盘失败：{e}")

    def _name_in_group(self, gid: int | None, qq: int) -> str:
        if gid:
            names = self._names.get(gid) or self.get_group(gid).get("names", {})
            mine = [n for n, u in names.items() if int(u) == int(qq)]
            if mine:
                return _DUP_MARK_RE.sub("", mine[-1]).strip()
        return self.get_user(qq).get("name") or str(qq)

    def fact_cap(self, tier: str) -> int:
        return int(self.facts_by_tier.get(tier) or self.max_facts)

    # -------------------------------------------------------------- 写入：攒旧消息
    def add_pending(self, key: str, entries: list[dict]) -> None:
        if not self.enabled or not entries:
            return
        path = self._pending_path(key)
        pending = _read(path, [])
        for e in entries:                         # 每条一个编号，整理完按编号删（不按位置删，免得删错）
            pending.append({**e, "_pid": next(_PID)})
        cap = max(self.batch, self._batch_for(key)) * 5
        if len(pending) > cap:                    # 防止整理一直失败时无限变大
            logger.warning(f"长期记忆：{key} 攒了 {len(pending)} 条还没整理，最早的 {len(pending) - cap} 条丢掉")
            pending = pending[-cap:]
        _write(path, pending)
        if len(pending) >= self._batch_for(key):
            self._start(key)

    def _batch_for(self, key: str) -> int:
        return max(2, int(self.group_batch or self.batch)) if key.startswith("group_") else self.batch

    def _start(self, key: str) -> bool:
        """在后台整理这个会话（高峰时段、正在整理、刚失败过还没到重试时间，就先不整理）"""
        if self.defer() or key in self._running or time.time() < self._retry_at.get(key, 0):
            return False
        if key.startswith("group_") and time.time() - self._started.get(key, 0) < self.group_gap:
            if len(_read(self._pending_path(key), [])) < self._batch_for(key) * 3:   # 攒得太多就不等了，免得被丢掉
                return False
        self._started[key] = time.time()
        self._running.add(key)
        task = asyncio.create_task(self.summarize(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _t, k=key: self._running.discard(k))
        return True

    def retry_pending(self) -> int:
        """把攒够一批、还没整理的会话补做一次（开机时和定时任务用）；顺便在后台迁移几份旧格式档案。返回开始整理了几个"""
        n = 0
        for key in self.pending_keys():
            if len(_read(self._pending_path(key), [])) >= self._batch_for(key) and self._start(key):
                n += 1
        if self.migrate_enabled and not self._migrating and not self.defer():
            task = asyncio.create_task(self.migrate_some(self.migrate_per_round))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return n

    def _failed(self, key: str) -> None:
        """整理失败：5 分钟、15 分钟、1 小时后再试（期间来新消息也不马上重试，免得每条消息都白花一次钱）"""
        n = self._fails[key] = self._fails.get(key, 0) + 1
        wait = (300, 900, 3600)[min(n, 3) - 1]
        self._retry_at[key] = time.time() + wait
        logger.warning(f"长期记忆整理失败（{key} 连续第 {n} 次），{wait // 60} 分钟后再试")

    def pending_keys(self, since: float = 0.0) -> list[str]:
        """还有没整理的消息的会话（只看 since 之后改动过的）"""
        d = self.root / "pending"
        if not d.exists():
            return []
        return [f.stem for f in d.glob("*.json") if f.stat().st_mtime >= since]

    def drop_pending(self, key: str) -> None:
        self._pending_path(key).unlink(missing_ok=True)

    # -------------------------------------------------------------- 整理
    def _people_in(self, key: str, entries: list[dict]) -> dict[int, str]:
        people: dict[int, str] = {}
        for e in entries:
            if e.get("uid"):
                people[int(e["uid"])] = e.get("name", "")
            for name, uid in (e.get("speakers") or {}).items():
                people[int(uid)] = name
        if key.startswith("private_"):
            qq = int(key.split("_")[1])
            people.setdefault(qq, "")
        return people

    @staticmethod
    def _transcript(key: str, entries: list[dict]) -> str:
        lines = []
        for e in entries:
            if e["role"] == "assistant":
                lines.append(f"伊蕾娜（她自己）：{e['content']}")
            elif key.startswith("private_"):
                lines.append(f"对方：{e['content']}")
            else:                                 # 群聊记录里的“→ 你”是对她说的；整理的模型看了会以为是它自己，写成“→ 伊蕾娜”
                lines.append(_ARROW_YOU_RE.sub(r"\1伊蕾娜", str(e["content"])))
        return "\n".join(lines)

    @staticmethod
    def _fact_line(f: dict) -> str:
        kind = f.get("kind") or "未分类"
        tags = [f"{kind}·{_weight(f.get('weight'))}" if f.get("kind") else "未分类", _md(f.get("seen") or f.get("since"))]
        if f.get("due"):
            tags.append(f"到 {_md(f['due'])}")
        if f.get("tags"):
            tags.append("词：" + "/".join(f["tags"]))
        return f"  #{f.get('id')} [{'｜'.join(t for t in tags if t)}] {f.get('text', '')}"

    def _profile_block(self, qq: int, name: str, prof: dict, place: str = "private", gid: int | None = None) -> str:
        """给整理的模型看的档案：群里、空间里整理时，私聊才知道的事不给它看（免得它改写成群里的往事、写进公开的日记）"""
        tier = self.familiarity(qq, self.close_friends)
        head = f"- QQ {qq}（昵称：{name or prof.get('name') or '未知'}｜关系：{self.TIER_NAMES[tier]}｜最多记 {self.fact_cap(tier)} 条）"
        lines = [head, f"  印象：{prof.get('impression') or '（还没有）'}"]
        facts = [f for f in prof.get("facts") or [] if not _expired(f) and scope_ok(f.get("scope"), place, gid)]
        lines += [self._fact_line(f) for f in facts] or ["  （还没有记下什么）"]
        told = [t for t in prof.get("told") or [] if isinstance(t, dict) and scope_ok(t.get("scope"), place, gid)]
        if told:
            lines.append("  她跟他说过：" + "；".join(f"{t.get('text', '')}（{_md(t.get('date'))}）" for t in told[-MAX_TOLD:]))
        return "\n".join(lines)

    def _events_block(self, gid: int | None, group: dict | None) -> str:
        if not group:
            return "（私聊 / 空间评论，没有群往事）"
        rows = []
        for e in group.get("events") or []:
            who = "、".join(self._name_in_group(gid, q) for q in _as_list(e.get("who")) if _int(q) is not None)
            rows.append(f"#{e.get('id')} {_md(e.get('date')) or '日期不详'}{f'（在场：{who}）' if who else ''}：{e.get('text', '')}")
        return "\n".join(rows) or "（还没有）"

    async def summarize(self, key: str, force: bool = False) -> None:
        """force=True：不等攒够一批，有多少整理多少（晚上写日记前的“日结”用）"""
        async with self._locks[key]:
            path = self._pending_path(key)
            entries = _read(path, [])
            if len(entries) < (2 if force else self._batch_for(key)):
                return
            people = self._people_in(key, entries)
            async with contextlib.AsyncExitStack() as stack:
                for qq in sorted(people):           # 同一个人的档案同一时间只有一处在整理（按 QQ 排序加锁，不会互相卡死）
                    await stack.enter_async_context(self._user_locks[qq])
                await self._summarize(key, path, entries, people)

    async def _summarize(self, key: str, path: Path, entries: list[dict], people: dict[int, str]) -> None:
        gid = int(key.split("_")[1]) if key.startswith("group_") else None
        profiles = {qq: self.get_user(qq) for qq in people}
        group = self.get_group(gid) if gid else None
        where = f"群聊（群 {gid}）" if gid else ("QQ 空间评论（公开的）" if key.startswith("qzone_") else "私聊")
        place = "group" if gid else ("qzone" if key.startswith("qzone_") else "private")
        gens = {qq: profiles[qq].get("mem_gen", 0) for qq in people}   # 整理期间管理员 /忘记 过的，印象、说过的话不再写回去
        user_msg = SUMMARIZE_INPUT.format(
            today=datetime.now().strftime("%Y-%m-%d（%Y年%m月%d日）"),
            where=where,
            profiles="\n".join(self._profile_block(qq, people[qq], profiles[qq], place, gid) for qq in people) or "（无）",
            events=self._events_block(gid, group),
            transcript=self._transcript(key, entries),
        )
        if not budget.can_background():
            logger.info(f"长期记忆：今天的钱花完了，{key} 先不整理，留到明天（待整理的消息都留着）")
            return
        try:
            api = self.summary_client or self.client     # 整理单独用更长的超时，出错不自动重试（每次重试都计费）
            resp = await api.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": SUMMARIZE_PROMPT},    # 固定的规则放最前面：每次一字不差，能命中缓存
                          {"role": "user", "content": user_msg}],
                temperature=0.3,
                max_tokens=self.summary_max_tokens,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "memory", user=None, group=None)
            usage = getattr(resp, "usage", None)
            used = getattr(usage, "completion_tokens", None)
            if resp.choices[0].finish_reason == "length":
                logger.warning(f"长期记忆整理写到一半被截断了（写了 {used} token，MEMORY_SUMMARY_MAX_TOKENS={self.summary_max_tokens} 不够），这次作废：{key}")
                self._failed(key)
                return
            data = json.loads(resp.choices[0].message.content or "")
            if not isinstance(data, dict) or not isinstance(data.get("people"), list):
                raise ValueError(f"返回的内容不对（{str(resp.choices[0].message.content)[:60]!r}）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"长期记忆整理失败：{key}：{e}")
            self._failed(key)
            return
        self._fails.pop(key, None)
        self._retry_at.pop(key, None)

        try:
            # 先在内存里算好所有人的新档案，最后统一写入；每批记一个编号，同一批重试时不重复加减好感
            scope = scope_of(key)
            batch_id = entries[0].get("_pid") if entries else None
            changed, updated = [], []
            for p in _as_list(data.get("people")):
                if not isinstance(p, dict):
                    continue
                try:
                    qq = int(p.get("qq"))
                except (TypeError, ValueError):
                    continue
                if qq not in people:        # 不在这段记录里的人不许改
                    continue
                prof = self.get_user(qq)    # 重新读一遍：整理期间她可能又和这个人聊过（计数、好感已变）
                if people[qq]:
                    prof["name"] = people[qq]
                tier = self.familiarity(qq, self.close_friends)
                forgot = prof.get("mem_gen", 0) != gens.get(qq, 0)
                self._merge_facts(prof, p, scope, qq, tier)
                imp = _clean_text(p.get("impression"), 40)
                if not forgot:
                    self._merge_told(prof, p, scope)
                    if imp:
                        prof["impression"] = imp
                prof["facts"] = _trim_facts([f for f in prof.get("facts") or [] if not _expired(f)], self.fact_cap(tier))
                guess = {"男": "male", "女": "female"}.get(str(p.get("gender_guess", "")).strip())
                ev = _clean_text(p.get("gender_evidence"), 30)
                if guess and ev and not (batch_id is not None and batch_id in (prof.get("batches") or [])):
                    note = self.gender_evidence(prof, guess, str(p.get("gender_evidence_kind", "")).strip(), ev)
                    if note:                     # 发现被骗了：记一笔，以后可以拿来挖苦
                        prof.setdefault("facts", []).append(self._new_fact(prof, note, "梗", 2, scope))
                try:
                    delta = int(p.get("affection", 0))
                except (TypeError, ValueError):
                    delta = 0
                delta = max(-15, min(5, delta))
                if tier == "disliked" and delta > 0:     # 讨厌的人想挽回，加分减半（至少 +1）
                    delta = max(1, delta // 2)
                done_batches = prof.get("batches") or []
                if batch_id is not None and batch_id in done_batches:
                    delta = 0                                # 这一批上次已经算过好感（上次写到一半出错了）
                if delta < 0:
                    delta = self._nag_capped(prof, qq, delta, str(p.get("affection_kind", "")).strip())
                cap = self.affection_cfg.get("summary_daily_cap", 0)
                if delta > 0 and cap:                    # 每人每天靠整理最多加这么多（聊得再多也不会一天就熟起来）
                    today = self._today()
                    if prof.get("sum_gain_day") != today:
                        prof["sum_gain_day"], prof["sum_gain_today"] = today, 0
                    room = max(0, cap - int(prof.get("sum_gain_today", 0)))
                    if delta > room:
                        logger.info(f"长期记忆：{qq} 今天靠聊天已经涨了 {prof.get('sum_gain_today', 0)} 分，这次 +{delta} 只算 +{room}")
                        delta = room
                    prof["sum_gain_today"] = int(prof.get("sum_gain_today", 0)) + delta
                if delta:
                    self._apply_affection(prof, delta, str(p.get("reason", ""))[:30])
                if batch_id is not None:
                    prof["batches"] = (done_batches + [batch_id])[-5:]
                updated.append(prof)
                changed.append(qq)
            if group is not None:
                self._merge_events(group, data.get("group_events"), gid, people)
                group["names"] = self._names.get(gid, group.get("names", {}))
            for prof in updated:                    # 都算好了再一起写：中途出错就一个都不写
                self.save_user(prof)
            if group is not None:
                self.save_group(group)
            if self.moment_sink and data.get("today_moments"):
                try:
                    self.moment_sink(key, [x for x in _as_list(data.get("today_moments")) if isinstance(x, dict)],
                                     {q: _DUP_MARK_RE.sub("", n).strip() for q, n in people.items()})
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"今日见闻保存失败：{e}")
        except Exception as e:  # noqa: BLE001   模型给的格式怪、合并时出错：这批一个都不写，按失败处理（隔一阵再试，不会每条消息都重花一次钱）
            logger.warning(f"长期记忆：{key} 整理结果合并出错，这次作废：{type(e).__name__}: {e}")
            self._failed(key)
            return
        # 只删掉这次整理过的那几条；整理期间新攒的留着下次用
        done = {e["_pid"] for e in entries if e.get("_pid") is not None}
        now_list = _read(path, [])
        remain = [e for e in now_list if e.get("_pid") not in done] if done else now_list[len(entries):]
        if remain:
            _write(path, remain)
        else:
            path.unlink(missing_ok=True)
        logger.info(f"长期记忆已整理：{key}，更新了 {len(changed)} 人的档案")

    # -------------------------------------------------------------- 合并：模型只给“增改删”，程序来改档案
    def _new_fact(self, prof: dict, text: str, kind, weight, scope: str, due=None, since: str | None = None, tags=None) -> dict:
        nid = int(prof.get("next_id") or 1)
        prof["next_id"] = nid + 1
        today = _today_str()
        f = {"id": nid, "text": text, "kind": kind if kind in KINDS else "经历", "weight": _weight(weight),
             "scope": scope, "since": since or today, "seen": today}
        d = _parse_day(due)
        if d and f["kind"] == "计划":
            f["due"] = d
        t = _clean_tags(tags, text)
        if t:
            f["tags"] = t
        return f

    def _merge_facts(self, prof: dict, p: dict, scope: str, qq: int, tier: str = "stranger") -> None:
        facts: list[dict] = [f for f in prof.get("facts") or [] if isinstance(f, dict)]
        place, gid = self._place(scope), self._gid(scope)
        visible = [f for f in facts if scope_ok(f.get("scope"), place, gid)]    # 这次给模型看过的（群里整理时不给它看私聊的事）
        by_id = {_int(f.get("id")): f for f in visible if _int(f.get("id")) is not None}
        today = _today_str()
        ops = any(k in p for k in ("add", "update", "drop", "touch"))
        if not ops and isinstance(p.get("facts"), list):
            # 模型没按“增改删”写、而是给了整份档案：按旧办法合并（一下子少了一半以上就当成漏写，保留原来的）
            new = [t for t in (_item_text(x) for x in p["facts"]) if t]
            if len(visible) >= 4 and len(new) < len(visible) / 2:
                logger.warning(f"长期记忆：{qq} 的档案从 {len(visible)} 条变成 {len(new)} 条，不像正常整理，保留原来的")
                return
            old = {_norm(f.get("text", "")): f for f in visible}
            known = {_norm(f.get("text", "")) for f in facts}
            out = []
            for t in new[: self.fact_cap(tier)]:    # 整份给的：排在前面的算重要的
                f = old.get(_norm(t))
                if not f and _norm(t) in known:     # 和没给它看的某条（私聊知道的）一样：那条原样留着，不再加一条
                    continue
                if f:
                    f["seen"] = today
                    out.append(f)
                else:
                    out.append(self._new_fact(prof, t, "经历", 2, scope))
            hidden = [f for f in facts if not any(f is v for v in visible)]
            prof["facts"] = out + hidden            # 这次没给它看的（私聊知道的事）原样留着
            return

        def ids(v) -> list[int]:
            return [i for i in (_int(x.get("id") if isinstance(x, dict) else x) for x in _as_list(v)) if i in by_id]

        drops = set(ids(p.get("drop")))
        if len(visible) >= 4 and len(drops) > len(visible) / 2:
            logger.warning(f"长期记忆：{qq} 这次要删 {len(drops)}/{len(visible)} 条，不像正常整理，这次不删")
            drops = set()
        for i in ids(p.get("touch")):
            by_id[i]["seen"] = today
        for u in _as_list(p.get("update")):
            if not isinstance(u, dict):
                continue
            got = ids([u.get("id")])
            if not got or got[0] in drops:
                continue
            f = by_id[got[0]]
            text = _clean_text(u.get("text"))
            if text:
                f["text"] = text
            if u.get("kind") in KINDS:
                f["kind"] = u["kind"]
            t = _clean_tags(u.get("tags"), f.get("text", ""))
            if t:
                f["tags"] = t
            if "weight" in u:
                f["weight"] = _weight(u["weight"], f.get("weight", 2))
            d = _parse_day(u.get("due"))
            if d:
                f["due"] = d
            if f.get("scope") == "legacy":
                # 旧条目不知道是在哪儿知道的：私聊里又聊到了就算私聊知道的；群里、空间里只补类型，场合留给迁移判断
                f["kind"] = f.get("kind") or "经历"
                if scope == "private":
                    f["scope"] = "private"
            f["seen"] = today
        existing = {_norm(f.get("text", "")): f for f in facts}
        for a in _as_list(p.get("add")):
            if not isinstance(a, dict):
                a = {"text": a}
            text, _day = _split_date(_clean_text(a.get("text")))
            if not text:
                continue
            same = existing.get(_norm(text)) or next((f for f in facts if _similar(text, f.get("text", ""))), None)
            if same:                              # 和已有的一样、或者说的基本是一回事：算又聊到了
                same["seen"] = today
                continue
            f = self._new_fact(prof, text, a.get("kind"), a.get("weight", 2), scope, a.get("due"), tags=a.get("tags"))
            facts.append(f)
            existing[_norm(text)] = f
        prof["facts"] = [f for f in facts if _int(f.get("id")) not in drops]

    @staticmethod
    def _place(scope: str) -> str:
        return "group" if scope.startswith("group:") else ("qzone" if scope == "qzone" else "private")

    @staticmethod
    def _gid(scope: str) -> int | None:
        return _int(scope[6:]) if scope.startswith("group:") else None

    @staticmethod
    def _merge_told(prof: dict, p: dict, scope: str) -> None:
        told = [t for t in prof.get("told") or [] if isinstance(t, dict)]
        for u in _as_list(p.get("told_update")):   # 兑现了、取消了、说反了：不删，改成现在的状态（忘不忘交给上限）
            if not isinstance(u, dict):
                continue
            old, new = _norm(_item_text(u.get("old"), 40)), _item_text(u.get("text"), 30)
            if not old or not new:
                continue
            for t in told:                        # 只改这段能看到的（群里整理改不到私聊里说的）
                cur = _norm(t.get("text", ""))
                if scope_ok(t.get("scope"), *_place_of(scope)) and (cur == old or _similar(cur, old)):
                    t["text"], t["date"] = new, _today_str()
                    break
            prof["told"] = told
        seen = {_norm(t.get("text", "")) for t in told}
        for t in _as_list(p.get("told")):
            text = _item_text(t, 30)
            if text and _norm(text) not in seen and not any(_similar(_norm(text), s) for s in seen):   # 同一件事不记两遍
                told.append({"text": text, "date": _today_str(), "scope": scope})
                seen.add(_norm(text))
        if told:
            prof["told"] = told[-MAX_TOLD:]

    def _merge_events(self, group: dict, ge, gid: int, people: dict[int, str]) -> None:
        events: list[dict] = [e for e in group.get("events") or [] if isinstance(e, dict)]
        today = _today_str()
        if isinstance(ge, list):
            # 旧写法：给了整份往事
            new = [t for t in (_item_text(x, 50) for x in ge) if t]
            if len(events) >= 4 and len(new) < len(events) / 2:
                logger.warning(f"长期记忆：群 {gid} 的往事从 {len(events)} 条变成 {len(new)} 条，不像正常整理，保留原来的")
                return
            old = {_norm(e.get("text", "")): e for e in events}
            out = []
            nid = _int(group.get("next_id"), 1) or 1
            for t in new:
                m = _MD_RE.match(t)
                day = _md_to_date(int(m.group(1)), int(m.group(2))) if m else None
                body = t[m.end():].strip() if m else t
                e = old.get(_norm(body))
                if not e:
                    e = {"id": nid, "text": body, "date": day or today, "who": [], "weight": 2}
                    nid += 1
                out.append(e)
            group["events"], group["next_id"] = out[-self.max_events:], nid
            return
        if not isinstance(ge, dict):
            return                                  # 没给群往事：原样保留
        by_id = {_int(e.get("id")): e for e in events if _int(e.get("id")) is not None}
        drops = {i for i in (_int(x) for x in _as_list(ge.get("drop"))) if i in by_id}
        if len(events) >= 4 and len(drops) > len(events) / 2:
            logger.warning(f"长期记忆：群 {gid} 这次要删 {len(drops)}/{len(events)} 条往事，不像正常整理，这次不删")
            drops = set()
        seen = {_norm(e.get("text", "")) for e in events}
        nid = max([_int(group.get("next_id"), 1) or 1] + [i + 1 for i in by_id])
        for a in _as_list(ge.get("add")):
            if not isinstance(a, dict):
                a = {"text": a}
            text = _clean_text(a.get("text"), 50)
            if not text or _norm(text) in seen:
                continue
            who = []
            for q in _as_list(a.get("who")):
                q = _int(q)
                if q in people and q not in who:
                    who.append(q)
            events.append({"id": nid, "text": text, "date": today, "who": who, "weight": _weight(a.get("weight", 2))})
            seen.add(_norm(text))
            nid += 1
        events = [e for e in events if _int(e.get("id")) not in drops]
        group["events"] = _trim(events, self.max_events, protect_top=False)
        group["next_id"] = nid

    # -------------------------------------------------------------- 旧格式档案迁移（一次性，后台慢慢做）
    def legacy_users(self) -> list[int]:
        out = []
        for qq in self.user_ids():
            prof = self.get_user(qq)
            if any(f.get("scope") == "legacy" for f in prof.get("facts") or []):
                out.append(qq)
        return out

    def backup_once(self) -> None:
        """升级前把旧格式的档案原样备份一份（只备份一次；开机时就做，赶在任何一次存盘之前）"""
        dest = self.root / "backup-v1"
        if dest.exists() or not (self.root / "users").exists():
            return
        if not any(isinstance(x, str) for f in (self.root / "users").glob("*.json")
                   for x in (_read(f, {}) or {}).get("facts") or []):
            return                                # 已经没有旧格式的了：不用备份
        tmp = self.root / "backup-v1.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(self.root / "users", tmp / "users")
            if (self.root / "groups").exists():
                shutil.copytree(self.root / "groups", tmp / "groups")
            tmp.replace(dest)                     # 整个复制完才改名：复制到一半断了，下次还会重新备份
            logger.info(f"长期记忆：升级前已把旧档案备份到 {dest}")
        except OSError as e:
            logger.warning(f"长期记忆：备份旧档案失败：{e}")

    _backup_once = backup_once

    def _groups_of(self, qq: int) -> list[int]:
        out = []
        d = self.root / "groups"
        for f in d.glob("*.json") if d.exists() else []:
            if not f.stem.isdigit():
                continue
            names = (self._names.get(int(f.stem)) or _read(f, {}).get("names") or {})
            if any(int(u) == int(qq) for u in names.values()):
                out.append(int(f.stem))
        return out

    MIGRATE_MAX_TRIES = 3

    async def migrate_some(self, limit: int = 3) -> int:
        """把几份旧格式档案交给模型整理成新格式（每次最多调 limit 次模型）。高峰时段、钱花完时不做"""
        if self._migrating:
            return 0
        self._migrating = True
        n = tried = 0
        try:
            for qq in self.legacy_users():
                if tried >= limit or self.defer() or not budget.can_background():
                    break
                if time.time() < self._migrate_fail.get(qq, 0):
                    continue
                tried += 1
                async with self._user_locks[qq]:
                    if await self.migrate_user(qq):
                        n += 1
                    else:
                        self._migrate_fail[qq] = time.time() + 3600
        finally:
            self._migrating = False
        if tried:
            logger.info(f"长期记忆：这轮迁移了 {n}/{tried} 份旧档案，还剩 {len(self.legacy_users())} 份")
        return n

    def _give_up_migrate(self, qq: int, prof: dict) -> None:
        """模型试了几次都没整理好：不再花钱，旧条目按“私聊知道的小事”留着（只在私聊用，一个月没聊到就淡掉）"""
        for f in prof.get("facts") or []:
            if f.get("scope") == "legacy":
                f.update(scope="private", kind=f.get("kind") or "经历", weight=1)
        prof.pop("migrate_tries", None)
        self.save_user(prof)
        logger.warning(f"长期记忆：{qq} 的旧档案迁移试了 {self.MIGRATE_MAX_TRIES} 次都不行，旧条目改成只在私聊用的小事")

    async def migrate_user(self, qq: int) -> bool:
        prof = self.get_user(qq)
        legacy = [f for f in prof.get("facts") or [] if f.get("scope") == "legacy"]
        if not legacy:
            return True
        self.backup_once()
        tier = self.familiarity(qq, self.close_friends)
        user_msg = MIGRATE_INPUT.format(
            today=datetime.now().strftime("%Y-%m-%d"), name=prof.get("name") or qq, tier=self.TIER_NAMES[tier],
            n=len(legacy), facts="\n".join(f"- {f.get('text', '')}" for f in legacy))
        gen = prof.get("mem_gen", 0)
        legacy_ids = {_int(f.get("id")) for f in legacy}

        def failed(why: str) -> bool:
            p2 = self.get_user(qq)
            p2["migrate_tries"] = int(p2.get("migrate_tries", 0)) + 1
            logger.warning(f"长期记忆：迁移 {qq} 的旧档案失败（第 {p2['migrate_tries']} 次）：{why}")
            if p2["migrate_tries"] >= self.MIGRATE_MAX_TRIES:
                self._give_up_migrate(qq, p2)
                return True
            self.save_user(p2)
            return False

        try:
            api = self.summary_client or self.client
            resp = await api.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": MIGRATE_PROMPT}, {"role": "user", "content": user_msg}],
                temperature=0.2, max_tokens=self.summary_max_tokens,
                response_format={"type": "json_object"}, extra_body={"thinking": {"type": "disabled"}})
            budget.track(resp, "memory", user=None, group=None)
            if resp.choices[0].finish_reason == "length":
                raise ValueError("写到一半被截断了")
            data = json.loads(resp.choices[0].message.content or "")
            items = data.get("facts") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"返回的内容不对（{str(resp.choices[0].message.content)[:60]!r}）")
        except Exception as e:  # noqa: BLE001
            return failed(str(e))
        if not items and len(legacy) >= 3:
            return failed(f"模型一条也没留（原来 {len(legacy)} 条）")
        prof = self.get_user(qq)                 # 重新读：调用期间可能又聊过、或者被管理员 /忘记 了
        now_legacy = {_int(f.get("id")) for f in prof.get("facts") or [] if f.get("scope") == "legacy"}
        if prof.get("mem_gen", 0) != gen or now_legacy != legacy_ids:
            logger.info(f"长期记忆：迁移 {qq} 期间档案变了（被删过或已经整理过），这次的结果不用")
            return True
        try:
            groups = self._groups_of(qq)
            public = f"group:{groups[0]}" if len(groups) == 1 else ("public" if groups else "private")
            keep = [f for f in prof.get("facts") or [] if f.get("scope") != "legacy"]
            new = []
            for it in _as_list(items):
                if not isinstance(it, dict):
                    it = {"text": it}
                text, day = _split_date(_clean_text(it.get("text")))
                if not text:
                    continue
                since = _parse_day(it.get("date")) or day
                private = it.get("private") is True or str(it.get("private")).lower() == "true"
                f = self._new_fact(prof, text, it.get("kind"), it.get("weight", 2), "private" if private else public, since=since)
                if since:
                    f["seen"] = since
                new.append(f)
            told = []
            for t in _as_list(data.get("told")):
                text = _item_text(t, 30)
                # 说过的话默认当成私下说的；模型明确标了不是私事的才算公开
                pub = isinstance(t, dict) and (t.get("private") is False or str(t.get("private")).lower() == "false")
                if text:
                    told.append({"text": text, "date": _today_str(), "scope": public if pub else "private"})
            if told:
                prof["told"] = ([t for t in prof.get("told") or [] if isinstance(t, dict)] + told)[-MAX_TOLD:]
            imp = _clean_text(data.get("impression"), 40)
            if imp and not prof.get("impression"):
                prof["impression"] = imp
            prof["facts"] = _trim(new + keep, self.fact_cap(tier))
            prof.pop("migrate_tries", None)
        except Exception as e:  # noqa: BLE001
            return failed(f"{type(e).__name__}: {e}")
        self.save_user(prof)
        logger.info(f"长期记忆：{qq} 的旧档案已迁移（{len(legacy)} 条 → {len(new)} 条）")
        return True

    # -------------------------------------------------------------- 好感度
    # 分数 -50～150（讨厌 <0｜陌生人 0～39｜普通朋友 40～89｜熟人 90～129｜很熟 130+）。来源：① 长期记忆整理时按对话内容加减分；
    # ② 送面包、踩雷；③ 管理员手动调整（光聊天不加分：9/30 删掉了 AFFECTION_CHAT_GAIN）。新人从 20 起步；
    # 很久不聊会慢慢向 20 回落，但回落不改变档位（9/30 起）：很熟最多落到 130，讨厌的人最多回升到 -1。
    close_friends: tuple = ()
    summary_max_tokens: int = 4000           # 整理时模型最多写多少（只写增改删，一般几百 token）
    summary_client = None                    # 整理专用的客户端（超时更长、不自动重试）；没设就用聊天那个
    gender_cap: bool = True                  # 只有确认是女生（或有证据地猜了两次都是女生）才能到“很熟”
    affection_cfg = {
        "decay_after_days": 7, "decay_per_day": 2,
        "min": -50, "max": 150,
        "start": 20, "dislike": 0, "friend": 40, "acquaintance": 90, "close": 130,
        "summary_daily_cap": 10, "nag_daily_cap": 10,
    }
    SCORE_VERSION = 2          # 9/29 换了分数范围：旧档案（-100～100）读的时候换算过来

    @staticmethod
    def migrate_score(old: float) -> float:
        """旧分数（-100～100：讨厌 <-20｜陌生人 -20～29｜熟人 30～69｜很熟 70+，新人 0）换成新分数，档位不变：
        讨厌 -50～0｜陌生人 0～40（新人 20）｜熟人 90～130｜很熟 130～150（新加的“普通朋友”40～90 以后慢慢聊出来）"""
        old = max(-100.0, min(100.0, float(old)))
        if old < -20:
            return round((old + 20) * 50 / 80, 1)
        if old < 0:
            return round(old + 20, 1)
        if old < 30:
            return round(20 + old * 20 / 30, 1)
        if old < 70:
            return round(90 + (old - 30), 1)
        return round(130 + (old - 70) * 20 / 30, 1)

    def _nag_capped(self, prof: dict, qq: int, delta: int, kind: str) -> int:
        """整理时的扣分：“纠缠”（烦人但没恶意）每人每天合计最多扣 nag_daily_cap 分；“恶意”不限。
        没标类别的：-6 及以下按恶意、其余按纠缠；标了“正常”却扣分的，按纠缠算"""
        if kind != "恶意" and not (kind not in ("正常", "纠缠") and delta <= -6):
            cap = float(self.affection_cfg.get("nag_daily_cap", 0) or 0)
            if cap:
                today = self._today()
                if prof.get("nag_day") != today:
                    prof["nag_day"], prof["nag_today"] = today, 0
                room = max(0.0, cap - float(prof.get("nag_today", 0)))
                if -delta > room:
                    logger.info(f"长期记忆：{qq} 今天因为纠缠已经扣了 {prof.get('nag_today', 0):g} 分，这次 {delta} 只算 -{room:g}")
                    delta = -int(room)
                prof["nag_today"] = float(prof.get("nag_today", 0)) - delta
        return delta

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _tier_bounds(self, score: float) -> tuple[float, float]:
        """回落时分数能到的范围：不跨出现在的档位（9/30 用户定：回落不改变熟悉程度，其他加减分照常）。
        高于起步分的只往下落到本档下限（很熟 130、熟人 90、普通朋友 40、陌生人落到起步分）；
        低于起步分的只往上回到本档上限（陌生人回到起步分，讨厌的人最多回到讨厌线下 1 分）"""
        c = self.affection_cfg
        start = float(c.get("start", 0))
        lines = sorted(float(c[k]) for k in ("dislike", "friend", "acquaintance", "close") if k in c)
        floor = max([start] + [b for b in lines if b <= score])
        above = [b for b in lines if b > score]
        ceil = min([start] + ([above[0] - 1] if above else []))
        return floor, ceil

    def effective_score(self, prof: dict) -> float:
        """计算衰减后的好感：超过 decay_after_days 天没说话，每天向新人的起始分靠拢 decay_per_day，
        但不跨出现在的档位（见 _tier_bounds）"""
        c = self.affection_cfg
        if "score" not in prof:                 # 旧版数据：按说话次数给个初始值
            prof["score"] = self.migrate_score(min(int(prof.get("talks", 0)), 25))
        score = float(prof["score"])
        if self.gender_cap and not self.counts_as_female(prof):
            score = min(score, c["close"] - 1)   # 先按性别封顶，再算回落：男生存的分再高，也按熟人那档算下限
        last = prof.get("last_talk")
        if last:
            # 从“最后说话 + 宽限期”开始回落；已经结算进 score 的那段（decay_settled 之前）不再重复算（9/30 修：
            # 以前别的加减分把回落后的分写回去以后，下次又从 last_talk 起整段再落一遍）
            begin = max(float(last) + c["decay_after_days"] * 86400, float(prof.get("decay_settled") or 0))
            idle_days = (time.time() - begin) / 86400
            if idle_days > 0:
                dec = idle_days * c["decay_per_day"]
                start = float(c.get("start", 0))
                floor, ceil = self._tier_bounds(score)
                if score > start:
                    score = max(min(floor, score), score - dec)
                elif score < start:
                    score = min(max(ceil, score), score + dec)
        if self.gender_cap and not self.counts_as_female(prof):
            score = min(score, c["close"] - 1)   # 男生、看不出性别的人：最高到熟人
        score = max(float(c.get("min", -50)), min(float(c.get("max", 150)), score))
        return round(score, 1)

    def _apply_affection(self, prof: dict, delta: float, reason: str = "") -> None:
        c = self.affection_cfg
        prof["score"] = max(float(c.get("min", -50)), min(float(c.get("max", 150)), self.effective_score(prof) + delta))
        prof["decay_settled"] = time.time()      # 到现在为止的回落已经算进 score 了
        prof["last_talk"] = prof.get("last_talk") or time.time()
        log = prof.setdefault("affection_log", [])
        log.append(f"{self._today()} {'+' if delta > 0 else ''}{delta:g} {reason}".strip())
        del log[:-8]

    def bump_talk(self, qq: int, name: str = "") -> int:
        """记一次正常聊天：计数 +1，记下时间（光聊天不加好感；不受 memory_enabled 影响）"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)      # 先把衰减结算掉（旧数据也在这里按次数换算初始分）
        prof["talks"] = int(prof.get("talks", 0)) + 1
        if name:
            prof["name"] = name
        prof.pop("gain_day", None); prof.pop("gain_today", None)    # 9/30 删掉“光聊天加分”后留下的旧字段
        prof["score"] = score
        prof["last_talk"] = prof["last_msg"] = prof["decay_settled"] = time.time()
        self.save_user(prof)
        return prof["talks"]

    def last_seen(self, qq: int) -> float | None:
        """这个人上一次找她说话的时间（群聊私聊都算）；从没说过话返回 None"""
        prof = self.get_user(qq)
        return prof.get("last_msg") or prof.get("last_talk")

    def user_ids(self) -> list[int]:
        d = self.root / "users"
        return [int(f.stem) for f in d.glob("*.json") if f.stem.isdigit()] if d.exists() else []

    def mark_letter(self, qq: int) -> None:
        prof = self.get_user(qq)
        prof["last_letter"] = time.time()
        self.save_user(prof)

    GENDER_NAMES = {"male": "男", "female": "女"}

    def bread_given_today(self, qq: int) -> bool:
        return self.get_user(qq).get("bread_day") == self._today()

    def take_bread(self, qq: int, delta: float) -> None:
        """收下今天的面包：记一笔，按需加分（每人每天只一次）"""
        prof = self.get_user(qq)
        if prof.get("bread_day") == self._today():
            return
        prof["bread_day"] = self._today()
        if delta:
            self._apply_affection(prof, delta, "送了面包")
        self.save_user(prof)

    def set_gender(self, qq: int, gender: str | None) -> None:
        """管理员设定性别（None = 清除）：最权威，之后的证据都不改它。好感封顶会跟着变"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)          # 先按旧性别结算一次
        if gender:
            prof["gender"], prof["gender_src"] = gender, "admin"
            prof.pop("gender_doubt", None)
            prof.pop("gender_lied", None)
        else:
            prof.pop("gender", None)
            prof.pop("gender_src", None)
        prof.pop("gender_pending", None)
        prof["score"] = score
        prof["decay_settled"] = time.time()          # 回落已经结算进 score
        self.save_user(prof)

    GENDER_GUESS_V = 2        # 9/30 改了猜性别的规则：旧的猜测（没证据也猜、多半猜成男生）读到时清掉

    GENDER_KINDS = ("自称", "别人称呼", "昵称", "拆穿", "承认骗人")

    def gender_evidence(self, prof: dict, g: str, kind: str, ev: str = "", day: str | None = None) -> str | None:
        """记一条性别证据，重新判断。返回“发现被骗了”的一句记录（没有就返回 None）。
        - 管理员 /性别 设的最权威，证据不改它；
        - 只有“自称”的时候最多算半信半疑：要有别的来源印证（别人称呼、昵称），或者不同的日子自称过两次，才算“猜得稳”；
        - 出现相反的证据就退回看不出（带着怀疑）；相反的证据也攒够了，就改判——以前确认过的（聊天里质疑后又确认的）也能推翻。"""
        if g not in ("male", "female"):
            return None
        kind = kind if kind in self.GENDER_KINDS else "自称"
        log = [e for e in prof.get("gender_log") or [] if isinstance(e, dict)]
        log.append({"g": g, "kind": kind, "ev": _clean_text(ev, 30), "day": day or _today_str()})
        prof["gender_log"] = log[-6:]
        return self._judge_gender(prof)

    def _judge_gender(self, prof: dict) -> str | None:
        if prof.get("gender_src") == "admin":
            return None
        log = prof.get("gender_log") or []
        if not log:
            return None
        before = prof.get("gender") or prof.get("gender_guess") or (prof.get("gender_doubt") or {}).get("before")
        last = log[-1]["g"]
        run = []                                   # 最近一段说法一致的证据
        for e in reversed(log):
            if e.get("g") != last:
                break
            run.append(e)
        conflict = len(run) < len(log)
        kinds = {e.get("kind") for e in run}
        self_days = {e.get("day") for e in run if e.get("kind") == "自称"}
        stable = "承认骗人" in kinds or (len(run) >= 2 and (kinds - {"自称"} or len(self_days) >= 2))
        note = None
        if stable:
            prof["gender_guess"] = last
            prof.pop("gender_doubt", None)
            if prof.get("gender") and prof["gender"] != last:
                prof.pop("gender", None)           # 以前“确认”过的被推翻了
                prof.pop("gender_src", None)
            if before and before != last and any(e.get("g") == before and e.get("kind") == "自称" for e in log):
                note = f"说过自己是{'女生' if before == 'female' else '男生'}，后来发现多半是骗伊蕾娜的"
                prof["gender_lied"] = before
        else:
            if prof.get("gender_guess") != last:
                prof.pop("gender_guess", None)
            opposite = next((e for e in reversed(log) if e.get("g") != last), None)
            if conflict or (prof.get("gender") and prof["gender"] != last):
                prof["gender_doubt"] = {"now": last, "ev": run[0].get("ev", ""), "before": (opposite or {}).get("g") or prof.get("gender"),
                                        "before_kind": (opposite or {}).get("kind") or ("自称" if prof.get("gender") else "")}
            else:
                prof.pop("gender_doubt", None)
        return note

    def claim_gender(self, qq: int, gender: str) -> None:
        """聊天里对方自称性别、被她质疑后又确认了一次：只算一次“自称”，不再直接定死（9/30 起）"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)
        prof.pop("gender_pending", None)
        self.gender_evidence(prof, gender, "自称", "被质疑后又确认了一次")
        prof["score"] = score
        prof["decay_settled"] = time.time()
        self.save_user(prof)

    @staticmethod
    def last_claim(prof: dict) -> str | None:
        """对方最近一次自称的性别"""
        return next((e.get("g") for e in reversed(prof.get("gender_log") or []) if e.get("kind") == "自称"), None)

    def counts_as_female(self, prof: dict) -> bool:
        """能不能到“很熟”：确认是女生（管理员设的，或者 9/30 以前聊天里确认的，没被推翻）；
        或者有证据地判断是女生，而且不只是她自己说的（9/30 用户定的 B 方案，加上“对方可能骗人”的怀疑）"""
        if prof.get("gender"):
            return prof["gender"] == "female"
        return prof.get("gender_guess") == "female"

    def gender_hint(self, prof: dict, who) -> str:
        """给她看的一句性别提示：确定的、猜得稳的、半信半疑的、有点怀疑被骗的"""
        w = lambda g: "女生" if g == "female" else "男生"   # noqa: E731
        g = prof.get("gender")
        doubt = prof.get("gender_doubt") or {}
        if g and prof.get("gender_src") == "admin":
            return f"「{who}」是{w(g)}。"
        if doubt:
            said = w(doubt.get("before")) if doubt.get("before") else "另一种说法"
            how = {"自称": f"说过自己是{said}", "别人称呼": f"被人当成{said}", "昵称": f"昵称看着像{said}"}.get(
                doubt.get("before_kind", ""), f"看起来像{said}")
            ev = f"（{doubt['ev']}）" if doubt.get("ev") else ""
            return (f"「{who}」以前{how}，但现在有对不上的地方{ev}。你有点怀疑对方在骗你："
                    "心里有数就行，想挖苦可以轻轻点一句，别追着问。")
        if g:
            return f"「{who}」说过自己是{w(g)}，你姑且信了。"
        if prof.get("gender_guess"):
            if self._lied(prof):
                return (f"「{who}」以前骗过你说自己是{w(self._lied(prof))}，后来被你识破了，你觉得应该是{w(prof['gender_guess'])}。"
                        "这笔账你记着：聊到相关的，可以嘴硬地挖苦一句，但别翻来覆去说，也别真生气。")
            return f"你觉得「{who}」应该是{w(prof['gender_guess'])}：别说破，也别问。"
        claim = self.last_claim(prof)
        if claim:
            return f"「{who}」自称是{w(claim)}，但没有别的证据，你半信半疑：别当真，也别追问。"
        return ""

    @staticmethod
    def _lied(prof: dict) -> str | None:
        """骗过她的性别（被识破、现在猜的是另一种）；没有就 None"""
        lied, guess = prof.get("gender_lied"), prof.get("gender_guess")
        if not lied and guess and any("多半是骗" in str(f.get("text", "")) for f in prof.get("facts") or [] if isinstance(f, dict)):
            lied = "female" if guess == "male" else "male"      # 旧档案里只有那条记忆，没存标记
        return lied if lied and guess and lied != guess else None

    def gender_text(self, prof: dict) -> str:
        g = prof.get("gender")
        if g:
            return f"{self.GENDER_NAMES[g]}（{'管理员设定' if prof.get('gender_src') == 'admin' else '本人确认'}）"
        guess = prof.get("gender_guess")
        if guess:
            return f"未确认（有证据地判断是{self.GENDER_NAMES[guess]}生）"
        if prof.get("gender_doubt"):
            return "未确认（说法对不上，有点怀疑在骗人）"
        claim = self.last_claim(prof)
        return f"未确认（自称{self.GENDER_NAMES[claim]}生，半信半疑）" if claim else "未确认"

    def taboo_penalty(self, qq: int, penalty: float, daily_max: float, reason: str) -> float:
        """踩雷立刻扣分，每人每天有上限"""
        prof = self.get_user(qq)
        today = self._today()
        if prof.get("taboo_day") != today:
            prof["taboo_day"], prof["taboo_today"] = today, 0.0
        cut = min(penalty, max(0.0, daily_max - prof["taboo_today"]))
        if cut > 0:
            prof["taboo_today"] += cut
            self._apply_affection(prof, -cut, reason)
            self.save_user(prof)
        return prof.get("score", float(self.affection_cfg.get("start", 0)))

    def adjust(self, qq: int, delta: float | None = None, set_to: float | None = None, reason: str = "管理员调整") -> float:
        prof = self.get_user(qq)
        if set_to is not None:
            delta = set_to - self.effective_score(prof)
        self._apply_affection(prof, float(delta or 0), reason)
        prof["last_talk"] = time.time()
        self.save_user(prof)
        return prof["score"]

    def familiarity(self, qq: int, close_friends=()) -> str:
        if qq in close_friends:
            return "close"
        c = self.affection_cfg
        score = self.effective_score(self.get_user(qq))
        if score < c["dislike"]:
            return "disliked"
        if score >= c["close"]:
            return "close"
        if score >= c["acquaintance"]:
            return "acquaintance"
        if score >= c.get("friend", c["acquaintance"]):
            return "friend"
        return "stranger"

    # -------------------------------------------------------------- 读取：给模型的提示
    def context_for(self, qq: int, name: str, gid: int | None, *, place: str | None = None,
                    text: str = "", recent: str = "") -> str:
        """挑几条记忆给她：
        - place：在哪儿说话。private 私聊（都能用）｜group 群里（私聊知道的不用）｜qzone 空间评论（公开的，私聊知道的不用）
        - text / recent：这句话和最近几句，用来挑相关的；不给就按重要、最近挑
        - 越熟带得越多；重要的（约定、身份、称呼）先带"""
        if not self.enabled:
            return ""
        place = place or ("group" if gid else "private")
        prof = self.get_user(qq)
        who = name or prof.get("name") or qq
        tier = self.familiarity(qq, self.close_friends)
        n_core, n_rel = CONTEXT_LIMITS.get(tier, (2, 3))
        q = _grams(f"{text} {recent}")
        parts = []
        gh = self.gender_hint(prof, who)
        if gh:
            parts.append(gh)
        if prof.get("impression"):
            parts.append(f"你对「{who}」的印象：{_to_you(prof['impression'])}")

        facts = [f for f in prof.get("facts") or [] if not _expired(f) and scope_ok(f.get("scope"), place, gid)]
        core = sorted((f for f in facts if _weight(f.get("weight")) >= 3),
                      key=lambda f: -_keep_score(f))[:n_core]
        rest = [f for f in facts if f not in core]
        if q:
            raw = f"{text} {recent}"
            scored = [(s, f) for f in rest
                      if (s := _related(q, _fact_words(f)) + sum(1 for t in f.get("tags") or [] if len(str(t)) >= 1 and str(t) in raw)) > 0]
            picked = [f for _, f in sorted(scored, key=lambda x: (-x[0], -_keep_score(x[1])))[:n_rel]]
            if not picked:                     # 这句话和哪条都不沾边：只想起最要紧的一两件
                picked = sorted(rest, key=lambda f: -_keep_score(f))[:max(1, n_rel // 2)]
        else:
            picked = sorted(rest, key=lambda f: -_keep_score(f))[:n_rel]
        ask = self._ask_item(prof, tier, place, gid)
        talked = _her_topics(f"{text} {recent}")
        keep = _cap_topics(core + [f for f in picked if f not in core], _fact_words, talked)
        chosen = [f for f in prof.get("facts") or [] if any(f is k for k in keep) and f is not ask]   # 按档案里的顺序
        if gh and self._lied(prof):              # 骗过她的事，性别那句已经说了，不重复
            chosen = [f for f in chosen if "多半是骗" not in str(f.get("text", ""))]
        if chosen:
            parts.append(f"关于「{who}」你记得：" + "；".join(self._fact_for_her(f) for f in chosen))

        told = [t for t in prof.get("told") or [] if scope_ok(t.get("scope"), place, gid)]
        if q:
            told = [t for t in told if _related(q, t.get("text", "")) > 0]
        told = [t for t in told if _her_topics(t.get("text", "")) <= talked]   # 她爱提的话题，对方没提就不带（免得催了又催）
        told = _cap_topics(told[::-1], lambda t: t.get("text", ""), talked)[::-1][-2:]
        if told:
            parts.append(f"你跟「{who}」说过的：" + "；".join(f"{_to_you(t['text'])}（{_ago(t.get('date'))}）" for t in told)
                         + "。别当成第一次说。")

        if ask:
            due = f"（大概是{_md(ask['due'])}的事）" if ask.get("due") else ""
            parts.append(f"【可以问问】「{who}」{_ago(ask.get('since'))}说过：{_to_you(ask.get('text', ''))}{due}。"
                         "隔了一阵了，聊得上的话可以顺口问一句后来怎么样；接不上就不提。只问这一件。")
            ask["offered"] = _today_str()          # 提过一次就不再提这件（问没问、对方怎么答，下次整理时会记进去）
            try:
                self.save_user(prof)
            except OSError as e:
                logger.warning(f"长期记忆：记下“可以问问”失败：{e}")

        if gid and place == "group":
            events = [e for e in self.get_group(gid).get("events") or [] if not self._event_expired(e)]
            mine = [e for e in events if int(qq) in [_int(x) for x in _as_list(e.get("who"))]][-3:]
            others = [e for e in events if e not in mine][-2:]
            rows = [e for e in events if e in mine or e in others]
            if rows:
                parts.append("这个群的往事：" + "；".join(self._event_for_her(gid, e) for e in rows))
        if not parts:
            return ""
        return (
            "【长期记忆】以下是你从以前的聊天里记住的事，自然地运用，别逐条复述，"
            "也别说“我记录里写着”。记错了就以对方现在说的为准。\n" + "\n".join(parts)
        )

    def _ask_item(self, prof: dict, tier: str, place: str, gid: int | None) -> dict | None:
        """挑一件可以问后续的事：计划到时间了的优先，其次是几天前说过的近况（重要度 2 以上）。
        只给熟人、很熟；空间评论（公开）不给；对方隔了 ASK_GAP_HOURS 以上才来才给；每件只提一次"""
        if tier not in ASK_TIERS or place == "qzone":
            return None
        seen = prof.get("last_msg") or prof.get("last_talk")
        try:
            if seen and time.time() - float(seen) < ASK_GAP_HOURS * 3600:
                return None
        except (TypeError, ValueError):
            return None
        today = date.today().isoformat()
        plans, recents = [], []
        for f in prof.get("facts") or []:
            if not isinstance(f, dict) or f.get("offered") or _expired(f) or not scope_ok(f.get("scope"), place, gid):
                continue
            if f.get("kind") == "计划" and f.get("due") and str(f["due"]) <= today:
                plans.append(f)
            elif f.get("kind") == "近况" and _weight(f.get("weight")) >= 2 and _days_since(f.get("since")) >= ASK_RECENT_MIN_DAYS:
                recents.append(f)
        if plans:
            return max(plans, key=lambda f: str(f.get("due")))
        if recents:
            return max(recents, key=lambda f: (_weight(f.get("weight")), -_days_since(f.get("since"))))
        return None

    @staticmethod
    def _fact_for_her(f: dict) -> str:
        t = _to_you(f.get("text", ""))
        if f.get("kind") in ("近况", "计划"):
            t += f"（{_ago(f.get('since'))}说的）"
            if f.get("due"):
                later = str(f["due"]) > date.today().isoformat()
                t = t[:-1] + f"，大概在{_md(f['due'])}" + ("，还没到，别问结果" if later else "") + "）"
        return t

    @staticmethod
    def _event_expired(e: dict) -> bool:
        w = _weight(e.get("weight"))
        return bool(w in FORGET_DAYS and e.get("date") and _days_since(e["date"]) > FORGET_DAYS[w])

    def _event_for_her(self, gid: int, e: dict) -> str:
        who = "、".join(self._name_in_group(gid, q) for q in _as_list(e.get("who")) if _int(q) is not None)
        head = _md(e.get("date"))
        head += f"（在场：{who}）" if who else ""
        return f"{head}：{e.get('text', '')}" if head else e.get("text", "")

    # -------------------------------------------------------------- 给管理员看
    TIER_NAMES = {"disliked": "讨厌", "stranger": "陌生人", "friend": "普通朋友", "acquaintance": "熟人", "close": "很熟"}

    def describe_affection(self, qq: int, close_friends=()) -> str:
        prof = self.get_user(qq)
        tier = self.TIER_NAMES[self.familiarity(qq, close_friends)]
        log = prof.get("affection_log", [])[-5:]
        lines = "\n".join(f"· {x}" for x in log) or "· （还没有变化记录）"
        head = f"（{prof.get('name') or qq}｜好感 {self.effective_score(prof):g}｜{tier}｜说过 {int(prof.get('talks', 0))} 次话）\n性别：{self.gender_text(prof)}"
        if prof.get("last_letter"):
            days = (time.time() - prof["last_letter"]) / 86400
            head += f"\n上次给他写信：{'今天' if days < 1 else f'{int(days)} 天前'}"
        return f"{head}\n最近变化：\n{lines}"

    @staticmethod
    def _scope_name(scope: str | None) -> str:
        scope = scope or "legacy"
        if scope.startswith("group:"):
            return f"群{scope[6:]}"
        return SCOPE_NAMES.get(scope, scope)

    def describe_user(self, qq: int) -> str:
        prof = self.get_user(qq)
        talks = int(prof.get("talks", 0))
        tier = self.familiarity(qq, self.close_friends)
        head = f"说过 {talks} 次话｜好感 {self.effective_score(prof):g}"
        facts = prof.get("facts") or []
        if not facts and not prof.get("impression"):
            return f"（关于 {qq} 还没有长期记忆｜{head}）"
        out = [f"（{prof.get('name') or qq}｜QQ {qq}｜{head}｜记了 {len(facts)}/{self.fact_cap(tier)} 条）",
               f"性别：{self.gender_text(prof)}"]
        if prof.get("impression"):
            out.append(f"印象：{prof['impression']}")
        for i, f in enumerate(facts):
            tags = [f.get("kind") or "未分类", "★" * _weight(f.get("weight")), self._scope_name(f.get("scope")), _ago(f.get("seen") or f.get("since"))]
            if f.get("tags"):
                tags.append("词：" + "/".join(map(str, f["tags"])))
            out.append(f"{i + 1}. {f.get('text', '')}（{'｜'.join(tags)}）")
        told = [t for t in prof.get("told") or [] if isinstance(t, dict)]
        if told:
            out.append("她说过（也能用 /忘记 第N条 删）：")
            out += [f"{len(facts) + i + 1}. {t.get('text', '')}（{self._scope_name(t.get('scope'))}｜{_ago(t.get('date'))}）"
                    for i, t in enumerate(told)]
        rows = []                              # 群往事里有他的（只是看看；删要用 /忘记 本群）
        for gid in self._group_ids():
            for e in self.get_group(gid).get("events") or []:
                if isinstance(e, dict) and int(qq) in [_int(x) for x in _as_list(e.get("who"))] and not self._event_expired(e):
                    rows.append((str(e.get("date") or ""), f"群{gid} {_md(e.get('date'))}：{e.get('text', '')}"))
        if rows:
            out.append("群往事里有他的（最近 5 条，只读）：")
            out += ["· " + r for _, r in sorted(rows)[-5:]]
        return "\n".join(out)

    def describe_group(self, gid: int) -> str:
        events = self.get_group(gid).get("events", [])
        if not events:
            return f"（群 {gid} 还没有往事记录）"
        return f"（群 {gid} 的往事）\n" + "\n".join(f"{i + 1}. {self._event_for_her(gid, e)}" for i, e in enumerate(events))
