"""QQ 空间日记：每天一条说说（一张自己的图 + 文案），并回复说说下的评论

- 今日见闻：长期记忆整理时顺便产出（memory.py 的 today_moments），存到 data/qzone/daily/日期.json。
  群聊只算白名单群（本来就只在白名单群里聊）；私聊只算有长期档案的人。
- 20:00 日结：白天还没攒够一批、没整理的聊天先整理一次，保证晚上的聊天也能写进日记。
- 20:30～22:30 随机一个时刻：按好感挑见闻 → 按见闻挑图 → 写文案 → 出戏/隐私检查 → 发说说（所有人可见）。
- 评论：每 30 分钟查一次最近 7 天的说说，只回直接对她说的评论，按聊天规则回（好感、雷点、出戏检查、限额）。
- 别人空间里的 @：查评论时顺便读“与我相关”（NapCat 不推送空间提醒，只能定时读），别人在说说里 @ 她就去评论一句，
  在评论里 @ 她、回她的话就回复那一条；别人之间聊天不插嘴。和自己说说下的评论共用每天的回复上限。
- 刷好友动态：查评论时顺便刷一次好友动态，好友几小时内发的原创说说按好感抽几率评论一句（每天单独限几条，每轮最多 1 条）。
- 管理员指令：/说说 预览｜发预览｜立即发｜删除最新｜今日｜图库｜查评论｜好友动态｜日结｜暂停｜恢复

这个模块在 roleplay_chat/__init__.py 的最后导入，直接用聊天插件里的人设、记忆、好感、限流这些东西。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timedelta

from nonebot import get_bots, get_driver, logger, on_command, on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment, NoticeEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import (
    BOT_DIR, FAMILIARITY_HINT, alert_admins, api_messages, cfg, classify_error, clean_reply, client, clip_input,
    drop_ooc_sentences, familiarity_of, get_history, in_peak, ltm, ooc_words, peak, rate_limited, save_history,
    short_hint, system_prompt, tagger, vision, _hour_window, _locks,
)
from .gallery import Gallery
from .qzone import (
    RIGHT_PUBLIC, Comment, Qzone, QzoneError, addressed_to_me, mention_targets, mentions_in, parse_comments, plain_text,
    post_pics,
)

DATA = BOT_DIR / cfg.qzone_data_dir
DAILY_DIR = DATA / "daily"
POSTS_FILE = DATA / "posts.json"
STATE_FILE = DATA / "state.json"
SEEN_FILE = DATA / "comments_seen.json"
BJ = peak.BEIJING
WEEKDAYS = "一二三四五六日"


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _now() -> datetime:
    return datetime.now(BJ)


def _hm(text: str) -> int:
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


def _in_ranges(ranges: str, minute: int) -> bool:
    for a, b in peak.parse_ranges(ranges):
        if a <= b and a <= minute < b:
            return True
        if a > b and (minute >= a or minute < b):     # 跨午夜，比如 23:00-02:00
            return True
    return False


# ------------------------------------------------------------------ 空间接口、图库
async def _cookie() -> str:
    bots = list(get_bots().values())
    if not bots:
        raise QzoneError("login", "机器人还没连上 NapCat")
    errors = []
    for dom in ("user.qzone.qq.com", "qzone.qq.com"):
        try:
            res = await bots[0].call_api("get_cookies", domain=dom)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{dom}: {e}")
            continue
        raw = res.get("cookies") if isinstance(res, dict) else res
        if isinstance(raw, str) and "skey" in raw:
            qz.cookie_source = dom
            return raw
        errors.append(f"{dom}: 返回里没有 skey")
    raise QzoneError("login", "NapCat get_cookies 没拿到可用 cookie：" + "；".join(errors))


qz = Qzone(_cookie, DATA / "qzone.log")
gallery = Gallery(BOT_DIR / cfg.qzone_image_dir, DATA, client, cfg.vision_model, tagger)


# ------------------------------------------------------------------ 熔断：出现风控信号就自动停一段时间
RISK_FILE = DATA / "risk.json"
EVENT_LOG = BOT_DIR / "data" / "risk_events.log"


def risk_state() -> dict:
    return _read(RISK_FILE, {})


def risk_block() -> str | None:
    """熔断中就返回说明（到几点、为什么），否则 None"""
    r = risk_state()
    until = float(r.get("until") or 0)
    if until > time.time():
        return f"熔断中，到 {datetime.fromtimestamp(until, BJ):%m-%d %H:%M}（原因：{r.get('reason', '')}）"
    return None


def log_event(text: str) -> None:
    """风控相关的事都记一行到 data/risk_events.log，方便对照下线时间"""
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{_now():%Y-%m-%d %H:%M:%S}｜{text}\n")
    except OSError:
        pass


async def _on_risk(kind: str, note: str) -> None:
    hours = cfg.qzone_risk_pause_hours
    until = time.time() + hours * 3600
    already = risk_block()
    _write(RISK_FILE, {"until": until, "reason": note, "kind": kind, "at": time.time()})
    log_event(f"空间风控信号（{kind}）：{note} → 空间功能自动停 {hours:g} 小时")
    logger.warning(f"QQ 空间：出现风控信号（{note}），空间功能自动停 {hours:g} 小时")
    if already:
        return
    bots = list(get_bots().values())
    if bots:
        await alert_admins(bots[0], "qzone_risk",
                           f"【QQ 机器人提醒】QQ 空间出现风控信号：{note}。为了防止设备被下线，空间功能（发说说、回评论、回 @）"
                           f"已自动停 {hours:g} 小时，到 {datetime.fromtimestamp(until, BJ):%m-%d %H:%M} 自动恢复。"
                           "想提前恢复就发 /说说 恢复。聊天不受影响。")


qz.on_risk = _on_risk


# ------------------------------------------------------------------ 闸门：下线了不动空间；刚上线、刚要到凭证不写
# 9/26 的 #3、#4 都是机器人刚连上 1 分多钟、刚要到凭证几秒就往空间里写，随后被踢；上线半小时后再写的那次没事。
_online_since: float | None = None      # 这次连上 NapCat 的时间；None = 没连上或账号已被下线
_offline_note = "机器人还没连上 NapCat"
_grace = 30 * 60.0                      # 这次上线的冷静期（每次上线在 30～60 分钟里随机，节奏别太固定）
_cookie_gap = 7 * 60.0                  # 这份凭证要放多久才能写（每次要到新凭证都重新随机 5～10 分钟）
_gap_for = 0.0                          # _cookie_gap 是按哪一次要到的凭证算的


def _new_grace() -> float:
    lo = cfg.qzone_startup_grace_minutes
    hi = max(lo, cfg.qzone_startup_grace_max_minutes)
    return random.uniform(lo, hi) * 60


def _gap() -> float:
    global _cookie_gap, _gap_for
    if qz.cookie_at and qz.cookie_at != _gap_for:
        _gap_for = qz.cookie_at
        _cookie_gap = random.uniform(5, 10) * 60
    return _cookie_gap


def read_block() -> str | None:
    """定时任务、/说说 查评论 能不能读空间：下线、熔断、刚上线都不读"""
    why = write_block("GET")
    if why:
        return why
    if time.time() - _online_since < _grace:
        return f"刚上线 {int((time.time() - _online_since) // 60)} 分钟，{datetime.fromtimestamp(_online_since + _grace, BJ):%H:%M} 之后才碰空间"
    return None


def write_block(method: str = "POST") -> str | None:
    """现在能不能碰空间：返回不能的原因，能就返回 None。
    任何请求：下线了、熔断中都不发（连凭证也不要）；写请求另外要过“刚上线”“刚要到凭证”两道"""
    if _online_since is None:
        return _offline_note
    rb = risk_block()
    if rb:
        return f"空间功能{rb}"
    if method != "POST":
        return None
    now = time.time()
    if now - _online_since < _grace:
        return f"刚上线 {int((now - _online_since) // 60)} 分钟，{datetime.fromtimestamp(_online_since + _grace, BJ):%H:%M} 之后才写空间"
    if not qz.cookie_at:
        return "还没有空间凭证：先要一个，过几分钟再写"
    if now - qz.cookie_at < _gap():
        return f"刚要到新凭证，{datetime.fromtimestamp(qz.cookie_at + _gap(), BJ):%H:%M} 之后才写空间"
    return None


def write_ready_at() -> float:
    """最早什么时候可以写（给定时任务安排重试用）"""
    t = time.time()
    if _online_since is not None:
        t = max(t, _online_since + _grace)
    t = max(t, (qz.cookie_at or time.time()) + _gap())
    return t


qz.guard = write_block


async def _alert(e: Exception, what: str) -> None:
    """空间接口出问题时私信管理员（同一类 1 小时只提醒一次）"""
    kind = getattr(e, "kind", "other")
    if kind not in ("login", "page", "api"):      # 限流、验证页、403 由熔断那边提醒
        return
    bots = list(get_bots().values())
    if bots:
        await alert_admins(bots[0], f"qzone_{kind}",
                           f"【QQ 机器人提醒】QQ 空间{what}失败：{e}。详细记录在 bot\\data\\qzone\\qzone.log")


# ------------------------------------------------------------------ 今日见闻
def _day_file(day: str):
    return DAILY_DIR / f"{day}.json"


def save_moments(key: str, moments: list, people: dict) -> None:
    """长期记忆整理完调用：把“今日见闻”记下来"""
    if key.startswith("group_"):
        src = "group"
    elif key.startswith("private_"):
        if not cfg.qzone_private_moments:
            return
        src = "private"
    elif key.startswith("qzone_"):
        src = "qzone"
    else:
        return
    day = _now().strftime("%Y-%m-%d")
    items = _read(_day_file(day), [])
    added = 0
    for m in moments[:2]:
        if not isinstance(m, dict):
            continue
        try:
            qq = int(m.get("qq"))
        except (TypeError, ValueError):
            continue
        if qq not in people:
            continue
        if src == "private" and not ltm.get_user(qq).get("facts"):     # 私聊只算“有印象”的人
            continue
        event = str(m.get("event") or "").strip()[:30]
        if not event or any(event == x.get("event") for x in items):
            continue
        items.append({
            "qq": qq, "event": event, "mood": str(m.get("mood") or "").strip()[:8],
            "keywords": [str(k).strip()[:8] for k in (m.get("keywords") or []) if str(k).strip()][:4],
            "src": src, "ts": int(time.time()),
        })
        added += 1
    if added:
        _write(_day_file(day), items[-40:])
        logger.info(f"今日见闻 +{added}（{key}）")
    # 只留最近 30 天
    cutoff = (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    for f in DAILY_DIR.glob("*.json"):
        if f.stem < cutoff:
            f.unlink(missing_ok=True)


ltm.moment_sink = save_moments


def today_moments() -> list[dict]:
    return _read(_day_file(_now().strftime("%Y-%m-%d")), [])


TIER_WEIGHT = {"close": 5.0, "acquaintance": 3.0, "stranger": 1.0, "disliked": 0.6}
COUNT_WEIGHTS = [(0, 0.10), (1, 0.55), (2, 0.25), (3, 0.10)]


def choose_moments(moments: list[dict]) -> list[dict]:
    """按好感挑 0～3 件：很熟的人最容易被写进去；讨厌的人只偶尔拿来吐槽"""
    allow_disliked = random.random() < 0.2
    pool = []
    for m in moments:
        tier = familiarity_of(int(m["qq"]))
        if tier == "disliked" and not allow_disliked:
            continue
        pool.append(dict(m, tier=tier))
    r, acc, n = random.random(), 0.0, 1
    for k, w in COUNT_WEIGHTS:
        acc += w
        if r < acc:
            n = k
            break
    n = min(n, cfg.qzone_max_moments, len(pool))
    picked = []
    while pool and len(picked) < n:
        weights = [TIER_WEIGHT[m["tier"]] for m in pool]
        m = random.choices(pool, weights=weights)[0]
        pool = [x for x in pool if x is not m and x["qq"] != m["qq"]]    # 同一个人一天只写一件
        picked.append(m)
    return picked


def _tier_hint(m: dict) -> str:
    tier = m["tier"]
    if tier == "close":
        name = ltm.get_user(int(m["qq"])).get("name") or ""
        call = f"（对方的称呼：{name}）" if name else ""
        return ("对方是你很熟的人：这件事可以写得分量重一点，嘴硬但看得出你在意。可以让对方一看就知道是在说他："
                f"用对方的称呼{call}，或者写“某个老熟人”“那个总来烦我的家伙”之类。")
    if tier == "acquaintance":
        return "对方是熟人：随口一提，带点调侃，写成“一个熟人”“之前认识的某人”，不写名字。"
    if tier == "stranger":
        return "对方是新认识的人：一笔带过，写成“新认识的人”“一个刚认识的人”，不写名字。"
    return "对方是你讨厌的家伙：只拿来吐槽一句，写成“那个烦人的家伙”，不写名字。"


# ------------------------------------------------------------------ 写文案
LENGTHS = [
    ("一句话，10～25 个字", 80),
    ("两三句，30～70 个字，可以用换行分成几行", 160),
    ("一小段游记，100～200 字", 350),
]

DIARY_PROMPT = """【这次不是聊天，是写旅行日记，发在 QQ 空间的说说里】现在是{date}（星期{weekday}）{period}。
这条日记会被很多人看到。配图是今天的你：{desc}
今天用传信魔法聊过天的人，都是你旅途中“新认识的人”或者熟人（不要把他们叫作“旅人”）。
{moments}
写作要求：
- 文案要和配图对得上：可以写图里的场景、你在做什么、你的心情。
- 篇幅：{length}。
- 用你写旅行日记时的口吻（和聊天时不一样）：冷静的第一人称旁白，吐槽写在叙述里，自恋、嘴硬、有点毒舌，偶尔流露一点真心。像随手写的日记，不要写成文章。
- 日记里才会用的习惯可以偶尔用上，但大多数时候一样都不用；用的话一条最多一样，别和最近几条用同一样（“话虽如此”尤其容易用多，要少用）：先把自己描述一番再自问自答（“……那位魔女究竟是谁？没错，就是我。”）、“话虽如此”“顺带一提”“原来如此原来如此”“可喜可贺可喜可贺”、结尾一句淡淡的感想（比如“卑鄙跟温柔果然挺类似的呢。”）。
- 不写任何人的 QQ 号、群名，不写别人私事的细节；除了上面明确允许的称呼，不写任何人的名字。
- 不落款（不要写“——伊蕾娜”），不加“#话题”，最多一个颜文字，也可以不加。不要出现“日记”“说说”“配图”“画像”这些字眼。
- 别和最近几条重复开头、句式和话题。最近几条：{recent}
只输出正文。"""

NO_MOMENT = ("今天没什么特别想写的见闻：就写旅途中的一件小事（在哪个国家或小镇、看到了什么、吃了什么），"
             "随手编一件就好，不要搬出书里有名有姓的人物和事件；也可以只写图里的场景和心情。")


def _period(now: datetime) -> str:
    h = now.hour
    return "清晨" if h < 9 else "上午" if h < 12 else "下午" if h < 18 else "晚上" if h < 23 else "深夜"


def recent_posts(n: int = 5) -> list[dict]:
    return _read(POSTS_FILE, [])[-n:]


_QQ_RE = re.compile(r"\d{5,}")


def privacy_problems(text: str, banned_names: set[str]) -> list[str]:
    bad = [n for n in banned_names if n and len(n) >= 2 and n in text]
    if _QQ_RE.search(text):
        bad.append("一串数字")
    return bad


async def make_post() -> dict:
    """生成一条说说草稿：{text, image, desc, moments}；失败抛异常"""
    if not cfg.deepseek_api_key:
        raise RuntimeError("没有配置 DEEPSEEK_API_KEY")
    await gallery.prepare()
    moments = choose_moments(today_moments())
    keywords = [k for m in moments for k in m.get("keywords", [])]
    img = gallery.pick(keywords, cfg.qzone_image_reuse_days)
    if not img:
        raise RuntimeError(f"图库里没有可用的图（{gallery.image_dir}）")

    lw = list(cfg.qzone_length_weights) + [0] * (3 - len(cfg.qzone_length_weights))
    length, max_tokens = random.choices(LENGTHS, weights=lw[:3])[0]
    if moments:
        lines = ["今天想写进去的事（挑着写，不用全写，也可以只写其中一件）："]
        for m in moments:
            lines.append(f"- {m['event']}（你的感受：{m.get('mood') or '说不上来'}）——{_tier_hint(m)}")
        moment_text = "\n".join(lines)
    else:
        moment_text = NO_MOMENT
    recent = "；".join(f"「{p['text'][:30]}」" for p in recent_posts()) or "（还没有）"
    now = _now()
    prompt = DIARY_PROMPT.format(date=f"{now.month}月{now.day}日", weekday=WEEKDAYS[now.weekday()],
                                 period=_period(now), desc=img["desc"], moments=moment_text,
                                 length=length, recent=recent)
    banned = {ltm.get_user(int(m["qq"])).get("name", "") for m in today_moments()
              if familiarity_of(int(m["qq"])) != "close"}
    messages = [{"role": "system", "content": system_prompt()}, {"role": "system", "content": prompt}]

    async def ask(extra: list[dict]) -> str:
        resp = await client.chat.completions.create(
            model=cfg.deepseek_model, messages=messages + extra, temperature=cfg.llm_temperature,
            max_tokens=max(min(max_tokens, cfg.qzone_max_tokens), 60), extra_body={"thinking": {"type": "disabled"}},
        )
        ch = resp.choices[0]
        t = clean_reply(ch.message.content or "", truncated=ch.finish_reason == "length")
        return re.sub(r"\n*\s*[—-]{1,2}\s*伊蕾娜\s*$", "", t).strip()

    text = await ask([])
    bad = ooc_words(text, "") + privacy_problems(text, banned)
    if bad:
        logger.info(f"说说文案有问题（{bad}），重写：{text[:40]}")
        text = await ask([{"role": "assistant", "content": text},
                          {"role": "system", "content": f"刚才写的有问题（出现了：{'、'.join(bad)}）。伊蕾娜不知道这些东西，"
                                                        "也不能写出别人的名字和号码。请完全按要求重写，只输出正文。"}])
        if ooc_words(text, "") or privacy_problems(text, banned):
            parts = re.split(r"(?<=[。！？!?…~\n])", text)
            text = "".join(p for p in parts if not ooc_words(p, "") and not privacy_problems(p, banned)).strip()
    if not text:
        raise RuntimeError("文案写出来有问题，删完就没了")
    return {"text": text, "image": img["file"], "desc": img["desc"],
            "moments": [f"{m['event']}（{ltm.TIER_NAMES[m['tier']]}）" for m in moments], "at": time.time()}


async def publish(draft: dict) -> str:
    img = gallery.items.get(draft["image"])
    if not img:
        raise RuntimeError("草稿里的图不在图库里了")
    tid = await qz.publish(draft["text"], [gallery.image_bytes(img)], right=RIGHT_PUBLIC)
    gallery.mark_used(draft["image"])
    posts = _read(POSTS_FILE, [])
    posts.append({"tid": tid, "ts": int(time.time()), "text": draft["text"], "image": draft["image"],
                  "desc": draft["desc"], "moments": draft.get("moments", [])})
    _write(POSTS_FILE, posts[-60:])
    logger.info(f"说说已发出 tid={tid}：{draft['text'][:40]}")
    return tid


DIARY_WORDS = ("说说", "空间", "日记", "动态", "配图", "那张图", "你发的")
TODAY_WORDS = ("今天", "今日", "今晚", "刚才", "最新", "最近")


def asks_today_diary(text: str, force: bool = False) -> bool:
    """是不是在问她今天 / 最近写的日记、说说（不是问以前旅途里的故事——那些也叫“旅行日记”）"""
    return force or (any(w in text for w in ("日记", "说说", "动态")) and any(w in text for w in TODAY_WORDS))


def diary_context(text: str, force: bool = False) -> str:
    """聊天时有人提到她的说说 / 日记：告诉她写过什么、今天的写了没有（聊天插件调用）。
    force=True：对方在接着刚才日记的话题追问（“然后呢”），话里没提“日记”也照样给"""
    if not force and not any(w in text for w in DIARY_WORDS):
        return ""
    posts = recent_posts(1)
    last = posts[-1] if posts else None
    now = _now()
    today = now.strftime("%Y-%m-%d")
    last_day = datetime.fromtimestamp(last["ts"], BJ).strftime("%Y-%m-%d") if last else ""
    parts = []
    if last and time.time() - last["ts"] <= 3 * 86400:
        when = datetime.fromtimestamp(last["ts"], BJ)
        parts.append(f"【你最近写的旅行日记】{when.month}月{when.day}日{'（就是今天）' if last_day == today else ''}："
                     f"「{last['text']}」（配图是你自己：{last['desc']}）。别人提到的话，你知道自己写过这个。")
    if last_day != today and asks_today_diary(text, force):
        try:
            st = state()
            plan = "今天不打算写了" if st.get("posted") else f"打算晚上 {st['post_at']} 左右写"
        except Exception:  # noqa: BLE001
            plan = "打算晚上再写"
        events = "；".join(str(m.get("event", "")) for m in today_moments()[:4] if m.get("event"))
        parts.append(f"【今天的旅行日记】今天的还没写，{plan}。"
                     + (f"今天记下来、可能会写进去的事：{events}。" if events else "今天还没遇到什么值得写的事。")
                     + "对方问起今天的日记，就照这个说（可以说还没写、写好了再给他看），"
                     "别编今天没发生的事，也别把以前旅途里的故事（【回忆参考】里的）说成今天的日记。")
    return "\n".join(parts)


# ------------------------------------------------------------------ 评论
def _seen() -> dict:
    return _read(SEEN_FILE, {})


def _save_seen(seen: dict) -> None:
    if len(seen) > 3000:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-3000:])
    _write(SEEN_FILE, seen)


def _comment_key(tid: str, root: Comment, item: Comment | None) -> str:
    c = item or root
    return f"{tid}|{root.tid}|{c.tid if item else '-'}|{c.uin}|{c.time}"


def _answered(me: int, root: Comment, item: Comment | None) -> bool:
    """她是不是已经在这条之后回过这个人了（上次回完没来得及记“已看过”的情况，免得再回一遍）"""
    c = item or root
    for r in root.replies:
        if r.uin != me or (c.time and r.time and r.time < c.time) or r is c:
            continue
        ms = r.mentions
        if item is None and (not ms or root.uin in ms):
            return True
        if item is not None and (item.uin in ms or (not ms and item.uin == root.uin)):
            return True
    return False


def _rounds_with(me: int, root: Comment, uin: int) -> int:
    """她在这一楼已经回过这个人几次"""
    n = 0
    for r in root.replies:
        if r.uin != me:
            continue
        ms = r.mentions
        if uin in ms or (not ms and uin == root.uin):
            n += 1
    return n


COMMENT_PROMPT = """【这是你旅行日记（QQ 空间说说）下的评论】你那天写的是：「{post}」（配图是你自己：{desc}）
这一楼到目前为止：
{thread}
现在「{nick}」对你说了最后那句。像回评论一样回一句：一两句话，不要换行。"""

ELSEWHERE_COMMENT_PROMPT = """【这是「{owner}」的 QQ 空间说说下的评论，有人在跟你说话】那条说说写的是：「{post}」{pic}
这一楼到目前为止：
{thread}
现在「{nick}」对你说了最后那句。像回评论一样回一句：一两句话，不要换行。"""

ELSEWHERE_POST_PROMPT = """【「{nick}」在自己的 QQ 空间说说里提到了你】他写的是：「{post}」{pic}
你在这条说说下面评论一句：一两句话，不要换行。"""

FRIEND_POST_PROMPT = """【你在刷 QQ 空间，刷到了「{nick}」刚发的说说】他写的是：「{post}」{pic}
这不是在跟你说话，是你自己看到的。你顺手在下面评论一句：一两句话，不要换行，不要 @ 人，别像在回复谁找你；不想说的话不用硬凑。"""


def _thread_text(me: int, root: Comment, upto: Comment) -> str:
    lines = []
    for c in [root] + root.replies:
        who = "你" if c.uin == me else f"【{c.nick or c.uin}】"
        lines.append(f"{who}：{c.text[:60]}")
        if c is upto:
            break
    return "\n".join(lines[-8:])


async def _pic_hint(urls: list[str]) -> str:
    """别人说说的配图：看第一张（和聊天里看图一样，同一张只看一次）"""
    if not urls or not cfg.vision_enabled:
        return ""
    try:
        desc = await vision.describe({"url": urls[0]})
    except Exception:  # noqa: BLE001
        desc = None
    return f"（配图：{desc}）" if desc else "（配了图，你没看清）"


async def _respond(uin: int, nick: str, text: str, scene: str) -> str | None:
    """按聊天规则想一句回复（不负责发出去）；返回 None 表示这次不回（讨厌、限流、出错……），"" 表示想不出来"""
    fam = familiarity_of(uin)
    if fam == "disliked" and random.random() < cfg.dislike_ignore_prob:
        logger.info(f"空间：讨厌的人，不想理 {uin}")
        return ""
    limited = rate_limited(uin, None)
    if limited == "cooldown":            # 刚回过这个人（比如同时在说说和评论里 @ 她）：等一会儿再回，不拖到下一轮
        await asyncio.sleep(cfg.user_cooldown + 1)
        limited = rate_limited(uin, None)
    if limited:
        return None
    key = f"qzone_{uin}"
    history = get_history(key)
    extra = "\n\n".join(x for x in (ltm.context_for(uin, nick, None), scene, FAMILIARITY_HINT[fam] + short_hint()) if x)
    messages = api_messages([{"role": "system", "content": system_prompt()}] + history[-6:]) + [
        {"role": "system", "content": extra}, {"role": "user", "content": text}]
    try:
        resp = await client.chat.completions.create(
            model=cfg.deepseek_model, messages=messages, temperature=cfg.llm_temperature,
            max_tokens=cfg.short_reply_max_tokens, extra_body={"thinking": {"type": "disabled"}})
        ch = resp.choices[0]
        reply = clean_reply(ch.message.content or "", truncated=ch.finish_reason == "length")
        bad = ooc_words(reply, text)
        if bad:
            logger.info(f"空间回复出戏（{bad}），重新生成：{reply[:40]}")
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model, temperature=cfg.llm_temperature, max_tokens=cfg.short_reply_max_tokens,
                messages=messages + [{"role": "assistant", "content": reply}, {"role": "system", "content":
                    f"刚才的回复出戏了（出现了：{'、'.join(bad)}）。伊蕾娜不知道这些东西。请完全以伊蕾娜的身份重新回复，不要道歉，不要解释。"}],
                extra_body={"thinking": {"type": "disabled"}})
            ch = resp.choices[0]
            reply = clean_reply(ch.message.content or "", truncated=ch.finish_reason == "length")
            if ooc_words(reply, text):
                reply = drop_ooc_sentences(reply, text)
    except Exception as e:  # noqa: BLE001
        kind = classify_error(e)
        logger.error(f"空间：DeepSeek 调用失败（{kind or '其他错误'}）：{e}")
        if kind:
            bots = list(get_bots().values())
            if bots:
                await alert_admins(bots[0], kind)
        return None
    reply = re.sub(r"([，。！？!?…~、,])\s*\n+\s*", r"\1", reply)    # 评论不分条：几行合成一行
    return re.sub(r"\s*\n+\s*", "，", reply).strip()[:150]


def _remember(uin: int, nick: str, said: str, reply: str, where: str, taboo: bool = True) -> None:
    """记进短期记忆、长期记忆；雷点照常扣分（taboo=False：对方不是在跟她说话，比如她刷到的好友说说，不扣）"""
    key = f"qzone_{uin}"
    history = get_history(key)
    user_line = f"【{nick}】（{where}）{said}"
    history.extend([{"role": "user", "content": user_line, "uid": uin, "name": nick, "ts": time.time()},
                    {"role": "assistant", "content": reply, "ts": time.time()}])
    save_history(key)
    tier_now = familiarity_of(uin)
    ltm.bump_talk(uin, nick)
    hit = taboo and next((w for w in cfg.taboo_words if w in said), None)
    if hit:
        k = cfg.taboo_tier_multiplier.get(tier_now, 1.0)
        if k > 0:
            ltm.taboo_penalty(uin, cfg.taboo_penalty * k, cfg.taboo_daily_max * k,
                              f"说她{hit}（{ltm.TIER_NAMES.get(tier_now, tier_now)}）")
    ltm.add_pending(key, [{"role": "user", "content": user_line, "uid": uin, "name": nick},
                          {"role": "assistant", "content": reply}])


async def reply_comment(me: int, post: dict, root: Comment, item: Comment | None,
                        owner: int | None = None, owner_nick: str = "", pic: str = "", earlier: list[str] | None = None) -> str:
    """回一条评论（她自己的说说，或 owner 的说说）；返回 replied / skipped / retry
    earlier：同一个人在这条说说下、这条之前连着说的几句（合成一次回，免得一条条回显得刷屏）"""
    why = write_block()
    if why:                              # 现在写不了：先别调模型（省钱，也不占冷却和额度）
        raise QzoneError("hold", why)
    c = item or root
    text = clip_input("\n".join((earlier or []) + [c.text])) or "（评论了一个表情）"
    nick = c.nick or str(c.uin)
    if owner and owner != me:
        scene = ELSEWHERE_COMMENT_PROMPT.format(owner=owner_nick or owner, post=plain_text(str(post.get("content") or ""))[:200],
                                               pic=pic, thread=_thread_text(me, root, c), nick=nick)
        where = f"在「{owner_nick or owner}」的说说下对你说"
    else:
        known = next((p for p in _read(POSTS_FILE, []) if p["tid"] == post.get("tid")), None)
        post_text = (known or {}).get("text") or str(post.get("content") or "")[:200]
        desc = (known or {}).get("desc") or "（记不清了）"
        scene = COMMENT_PROMPT.format(post=post_text, desc=desc, thread=_thread_text(me, root, c), nick=nick)
        where = "在你的日记下评论"
    async with _locks[f"qzone_{c.uin}"]:
        reply = await _respond(c.uin, nick, text, scene)
        if reply is None:
            return "retry"
        if not reply:
            return "skipped"
        ok = await qz.reply(str(post.get("tid")), root, c, reply, owner=owner)
        if not ok:
            # 回查没找到：不写进记忆、不算回过（免得她以为自己说过）；这条也不再重试（万一其实发出去了，重试会重复）
            logger.warning(f"空间：回复 {nick} 没能确认发出，不记进记忆，也不再重试：{reply}")
            return "unconfirmed"
        _remember(c.uin, nick, text, reply, where)
    logger.info(f"空间：回了 {nick}（{c.uin}）：{reply}")
    return "replied"


async def reply_post_mention(me: int, owner: int, owner_nick: str, d: dict, pic: str) -> str:
    """别人在自己的说说正文里 @ 了她：在那条说说下评论一句"""
    why = write_block()
    if why:
        raise QzoneError("hold", why)
    text = clip_input(plain_text(str(d.get("content") or ""))) or "（一条说说）"
    scene = ELSEWHERE_POST_PROMPT.format(nick=owner_nick, post=text, pic=pic)
    async with _locks[f"qzone_{owner}"]:
        reply = await _respond(owner, owner_nick, text, scene)
        if reply is None:
            return "retry"
        if not reply:
            return "skipped"
        ok = await qz.comment(str(d.get("tid")), owner, reply)
        if not ok:
            logger.warning(f"空间：在 {owner_nick} 的说说下评论没能确认发出，不记进记忆，也不再重试：{reply}")
            return "unconfirmed"
        _remember(owner, owner_nick, text, reply, "在自己的说说里提到你")
    logger.info(f"空间：在 {owner_nick}（{owner}）的说说下评论：{reply}")
    return "replied"


def _merge_same_person(todo: list, tid_of, uin_of, text_of) -> list:
    """同一条说说下、同一个人还没回的几条：只回最新那条，前面几条的话一起带上"""
    groups: dict = {}
    for t in todo:
        groups.setdefault((tid_of(t), uin_of(t)), []).append(t)
    out = []
    for items in groups.values():
        items.sort(key=lambda t: t["time"])
        last = dict(items[-1])
        last["earlier"] = [text_of(t) for t in items[:-1] if text_of(t)]
        last["also_keys"] = [t["key"] for t in items[:-1]]
        out.append(last)
    return sorted(out, key=lambda t: t["time"])


def _mark_seen(keys: list[str]) -> None:
    seen = _seen()
    for k in keys:
        seen[k] = int(time.time())
    _save_seen(seen)


_round = {"n": 0}          # 这一轮（poll_all）已经回了几条


def _round_full() -> bool:
    return _round["n"] >= cfg.qzone_max_replies_per_poll


async def _gap_between_replies(i: int) -> None:
    if i or _round["n"]:
        await asyncio.sleep(random.uniform(cfg.qzone_reply_gap_min, cfg.qzone_reply_gap_max))


def _count_reply() -> None:
    _round["n"] += 1
    st = state()
    st["comment_count"] = st.get("comment_count", 0) + 1
    save_state(st)


def _quota_left() -> int:
    st = state()
    if st.get("comment_day") != st["date"]:
        st["comment_day"], st["comment_count"] = st["date"], 0
        save_state(st)
    return cfg.qzone_comment_daily_max - st.get("comment_count", 0)


async def poll_comments(force: bool = False) -> str:
    """查一轮自己说说下的评论并回复；返回一句给管理员看的总结"""
    me = (await qz.ctx()).uin
    posts = await qz.list_posts(5)
    cutoff = time.time() - cfg.qzone_comment_days * 86400
    seen = _seen()
    todo = []
    for p in posts:
        if int(p.get("created_time") or 0) < cutoff:
            continue
        for root in await qz.comments_of(p):
            for item in [None] + root.replies:
                c = item or root
                k = _comment_key(str(p.get("tid")), root, item)
                if k in seen or c.uin == me:
                    continue
                if not addressed_to_me(me, root, item):
                    seen[k] = int(time.time())          # 别人之间在聊：不插嘴
                    continue
                if c.time and time.time() - c.time > cfg.qzone_comment_max_age_hours * 3600:
                    seen[k] = int(time.time())          # 太久以前的：不补回
                    continue
                if _rounds_with(me, root, c.uin) >= cfg.qzone_comment_max_rounds:
                    seen[k] = int(time.time())
                    logger.info(f"空间评论：和 {c.nick} 在这一楼已经来回 {cfg.qzone_comment_max_rounds} 轮了，不再回")
                    continue
                if _answered(me, root, item):
                    seen[k] = int(time.time())          # 之前其实回过了，只是没来得及记
                    continue
                todo.append({"time": c.time, "post": p, "root": root, "item": item, "key": k})
    _save_seen(seen)
    todo = _merge_same_person(todo, lambda t: str(t["post"].get("tid")), lambda t: (t["item"] or t["root"]).uin,
                              lambda t: (t["item"] or t["root"]).text)
    replied = skipped = 0
    for i, t in enumerate(todo):
        if _quota_left() <= 0 or _round_full():
            break                                           # 剩下的留到下一轮
        await _gap_between_replies(i)                       # 连着回几条时隔开几分钟
        res = await reply_comment(me, t["post"], t["root"], t["item"], earlier=t["earlier"])
        if res == "retry":
            continue
        _mark_seen([t["key"]] + t["also_keys"])
        if res == "replied":
            replied += 1
            _count_reply()
        elif res == "unconfirmed":
            _round["n"] += 1                                  # 可能其实发出去了：本轮额度照算，今天的上限不算
        else:
            skipped += 1
    return f"自己的说说：查了 {len(posts)} 条，新评论 {len(todo)} 条，回了 {replied} 条，不理 {skipped} 条"


async def find_mentions(me: int) -> tuple[list[dict], list[str]]:
    """读“与我相关”，找出别人说说里在对她说的话。返回 (待回复的列表, 已经看过的动态 key)
    每项：{"kind": "post"|"comment", "owner", "owner_nick", "d", "root", "item", "key", "time"}"""
    items = await qz.about_me(20)
    seen = _seen()
    now = time.time()
    max_age = cfg.qzone_comment_max_age_hours * 3600
    fresh: dict[tuple[int, str], dict] = {}
    feed_keys = []
    for it in items:
        if it["appid"] != "311" or not it["tid"] or not it["owner"] or it["owner"] == me:
            continue                                   # 她自己的说说由 poll_comments 管；赞之类的不管
        if now - it["abstime"] > max_age:
            continue
        fk = f"feed|{it['key']}|{it['abstime']}"
        if fk in seen:
            continue
        feed_keys.append(fk)
        fresh.setdefault((it["owner"], it["tid"]), dict(it, states=set(), cids=set(), fks=[]))
        fresh[(it["owner"], it["tid"])]["states"].add(it["state"])
        fresh[(it["owner"], it["tid"])]["fks"].append(fk)
        m = re.match(r"^(\d+)_", it["key"])
        if it["state"] == "评论提到我" and m:
            fresh[(it["owner"], it["tid"])]["cids"].add(m.group(1))     # key 开头的数字 = 那条评论的编号
    todo = []
    for (owner, tid), it in fresh.items():
        try:
            d = await qz.detail(tid, owner)
        except QzoneError as e:
            if e.kind == "api":          # 接口明确说不行（多半是没权限看）：以后也看不了，标成已看过
                logger.info(f"空间：读 {it['nick']} 的说说失败（可能没权限看）：{e}")
                continue
            raise                        # 下线、熔断、限流、网络……：这一轮直接停，动态留到下一轮再看
        d.setdefault("tid", tid)
        owner_nick = str(d.get("name") or it["nick"] or owner)
        post_at_me, hits = mention_targets(me, d)
        roots = parse_comments(d.get("commentlist"))
        # 详情里的 @ 标记万一没带上：以“与我相关”里的动作为准再补一遍
        post_at_me = post_at_me or "提到我" in it["states"]
        if not hits:
            for root in roots:
                if root.tid in it["cids"] and root.uin != me:
                    hits.append((root, None))
        if post_at_me:
            k = f"mpost|{owner}|{tid}"
            created = int(d.get("created_time") or it["abstime"])
            if k not in seen and not any(r.uin == me for r in roots) and now - created <= max_age:
                todo.append({"kind": "post", "owner": owner, "owner_nick": owner_nick, "d": d,
                             "root": None, "item": None, "key": k, "time": created, "uin": owner,
                             "text": plain_text(str(d.get("content") or "")), "feed_keys": it["fks"]})
            else:
                seen[k] = int(now)
        for root, item in hits:
            c = item or root
            k = f"m|{owner}|{tid}|{root.tid}|{c.tid if item else '-'}|{c.uin}|{c.time}"
            if k in seen:
                continue
            if ((c.time and now - c.time > max_age) or _rounds_with(me, root, c.uin) >= cfg.qzone_comment_max_rounds
                    or _answered(me, root, item)):
                seen[k] = int(now)
                continue
            todo.append({"kind": "comment", "owner": owner, "owner_nick": owner_nick, "d": d,
                         "root": root, "item": item, "key": k, "time": c.time or it["abstime"], "uin": c.uin,
                         "text": c.text, "feed_keys": it["fks"]})
        await asyncio.sleep(random.uniform(1.5, 4))     # 连着读几条说说时隔开一点
    _save_seen(seen)
    # 说说正文 @ 她和评论 @ 她分开算；评论里同一个人连着说的几句合成一次
    posts = [t for t in todo if t["kind"] == "post"]
    comments = _merge_same_person([t for t in todo if t["kind"] == "comment"],
                                  lambda t: t["d"]["tid"], lambda t: t["uin"], lambda t: t["text"])
    for t in posts:
        t.setdefault("earlier", [])
        t.setdefault("also_keys", [])
    return sorted(posts + comments, key=lambda t: t["time"]), feed_keys


async def poll_mentions() -> str:
    """别人空间里 @ 她、回她的话：按聊天规则回"""
    me = (await qz.ctx()).uin
    todo, feed_keys = await find_mentions(me)
    replied = skipped = 0
    pics: dict[str, str] = {}
    pending_feeds: set[str] = set()      # 有没回成、下次还要再看的动态
    for i, t in enumerate(todo):
        if _quota_left() <= 0 or _round_full():
            pending_feeds.update(x for tt in todo[i:] for x in tt["feed_keys"])
            break
        await _gap_between_replies(i)
        d = t["d"]
        if d["tid"] not in pics:
            pics[d["tid"]] = await _pic_hint(post_pics(d))
        if t["kind"] == "post":
            res = await reply_post_mention(me, t["owner"], t["owner_nick"], d, pics[d["tid"]])
        else:
            res = await reply_comment(me, d, t["root"], t["item"], owner=t["owner"],
                                      owner_nick=t["owner_nick"], pic=pics[d["tid"]], earlier=t["earlier"])
        if res == "retry":
            pending_feeds.update(t["feed_keys"])
            continue
        _mark_seen([t["key"]] + t["also_keys"])
        if res == "replied":
            replied += 1
            _count_reply()
        elif res == "unconfirmed":
            _round["n"] += 1
        else:
            skipped += 1
    # 这一轮看过的动态记下来，下次不再重复读详情（有新动静时 key 或时间会变）
    _mark_seen([fk for fk in feed_keys if fk not in pending_feeds])
    return f"别人的空间：{len(todo)} 处在跟她说话，回了 {replied} 处，不理 {skipped} 处"


# ------------------------------------------------------------------ 刷好友动态
def _friend_quota_left() -> int:
    st = state()
    if st.get("friend_day") != st["date"]:
        st["friend_day"], st["friend_count"], st["friend_uins"] = st["date"], 0, []
        save_state(st)
    return cfg.qzone_friend_comment_daily_max - st.get("friend_count", 0)


def _count_friend(uin: int) -> None:
    st = state()
    st["friend_count"] = st.get("friend_count", 0) + 1
    st["friend_uins"] = sorted(set(st.get("friend_uins", [])) | {uin})
    save_state(st)


def _friend_key(owner: int, tid: str) -> str:
    return f"friend|{owner}|{tid}"


def friend_candidates(me: int, items: list[dict]) -> list[dict]:
    """好友动态里，可能去评论的说说：好友自己发的（不是评论、转发别人的动态）、够新的；同一条只留一次"""
    now = time.time()
    max_age = cfg.qzone_friend_post_max_age_hours * 3600
    out, got = [], set()
    for it in items:
        owner = it["owner"]
        if it["appid"] != "311" or not it["tid"] or not owner or owner == me:
            continue
        if it["opuin"] and it["opuin"] != owner:
            continue                                   # 是别人在这条说说下的动作（评论、点赞……），不是好友刚发的
        if not it["abstime"] or now - it["abstime"] > max_age:
            continue
        if (owner, it["tid"]) in got:
            continue
        got.add((owner, it["tid"]))
        out.append(it)
    return out


def _friend_skip_reason(me: int, d: dict, created_fallback: int) -> str:
    """读到详情后，还有哪些情况不评"""
    if d.get("rt_tid") or d.get("rt_con") or d.get("rt_uin"):
        return "是转发"
    created = int(d.get("created_time") or created_fallback)
    if time.time() - created > cfg.qzone_friend_post_max_age_hours * 3600:
        return "发得太久了"
    if me in mentions_in(str(d.get("content") or "")):
        return "说说里 @ 了她（由回 @ 那边管）"
    if any(r.uin == me or any(x.uin == me for x in r.replies) for r in parse_comments(d.get("commentlist"))):
        return "她已经在下面说过话了"
    if not plain_text(str(d.get("content") or "")).strip() and not post_pics(d):
        return "没字也没图"
    return ""


async def comment_friend_post(me: int, owner: int, owner_nick: str, d: dict, pic: str) -> str:
    """在好友的说说下主动评论一句；返回 replied / skipped / retry / unconfirmed"""
    why = write_block()
    if why:
        raise QzoneError("hold", why)
    text = clip_input(plain_text(str(d.get("content") or ""))) or "（只发了图）"
    scene = FRIEND_POST_PROMPT.format(nick=owner_nick, post=text, pic=pic)
    async with _locks[f"qzone_{owner}"]:
        reply = await _respond(owner, owner_nick, text, scene)
        if reply is None:
            return "retry"
        if not reply:
            return "skipped"
        ok = await qz.comment(str(d.get("tid")), owner, reply)
        if not ok:
            logger.warning(f"空间：在 {owner_nick} 的说说下评论没能确认发出，不记进记忆，也不再重试：{reply}")
            return "unconfirmed"
        _remember(owner, owner_nick, text, reply, "发了条说说，你刷到后在下面评论了一句", taboo=False)
    logger.info(f"空间：刷到 {owner_nick}（{owner}）的说说，评论：{reply}")
    return "replied"


async def poll_friends() -> str:
    """刷一次好友动态：每条新说说按好感抽一次签，抽中的去评论一句；每轮最多评 1 条"""
    if _friend_quota_left() <= 0:
        return f"好友动态：今天已评 {cfg.qzone_friend_comment_daily_max} 条，不刷了"
    if _round_full():
        return "好友动态：这一轮回复已满，没刷"
    me = (await qz.ctx()).uin
    cands = friend_candidates(me, await qz.friend_feeds(20))
    seen = _seen()
    done_today = set(state().get("friend_uins", []))
    picked, fresh = [], 0
    for it in cands:
        k = _friend_key(it["owner"], it["tid"])
        if k in seen:
            continue
        fresh += 1
        prob = cfg.qzone_friend_comment_prob.get(familiarity_of(it["owner"]), 0.0)
        if it["owner"] in done_today or random.random() >= prob:
            seen[k] = int(time.time())                 # 没抽中：这条就当看过了，下次不再抽
        else:
            picked.append(it)
    _save_seen(seen)
    commented = reads = 0
    notes = []
    for it in picked:
        k = _friend_key(it["owner"], it["tid"])
        if commented or reads >= 3 or _round_full() or _friend_quota_left() <= 0:
            _mark_seen([k])                            # 抽中了但这轮轮不上：也不留到下一轮（免得多抽几次签）
            continue
        try:
            d = await qz.detail(it["tid"], it["owner"])
        except QzoneError as e:
            if e.kind == "api":
                _mark_seen([k])
                continue
            raise
        reads += 1
        d.setdefault("tid", it["tid"])
        nick = str(d.get("name") or it["nick"] or it["owner"])
        skip = _friend_skip_reason(me, d, it["abstime"])
        if skip:
            _mark_seen([k])
            notes.append(f"{nick}：{skip}")
            await asyncio.sleep(random.uniform(1.5, 4))
            continue
        await _gap_between_replies(0)
        res = await comment_friend_post(me, it["owner"], nick, d, await _pic_hint(post_pics(d)))
        if res == "retry":
            continue                                   # 限流、模型出错：留到下一轮
        _mark_seen([k])
        if res in ("replied", "unconfirmed"):          # 没确认的也算今天的一条（万一其实发出去了）
            commented += res == "replied"
            _round["n"] += 1
            _count_friend(it["owner"])
    out = f"好友动态：新说说 {fresh} 条，抽中 {len(picked)} 条，评论了 {commented} 条"
    return out + (f"（{'；'.join(notes)}）" if notes else "")


async def poll_all() -> str:
    _round["n"] = 0
    parts = [await poll_comments()]
    if cfg.qzone_mention_reply:
        await asyncio.sleep(random.uniform(2, 5))
        parts.append(await poll_mentions())
    if cfg.qzone_friend_comment:
        await asyncio.sleep(random.uniform(2, 5))
        parts.append(await poll_friends())
    st = state()
    st["last_poll"] = time.time()
    save_state(st)
    return "；".join(parts) + f"（今天共回 {st.get('comment_count', 0)}/{cfg.qzone_comment_daily_max}）"


# ------------------------------------------------------------------ 定时
def _new_day_state(day: str) -> dict:
    (a, b), = peak.parse_ranges(cfg.qzone_post_window)[:1] or [(1230, 1350)]
    m = random.randint(a, max(a, b - 1))
    old = _read(STATE_FILE, {})
    return {"date": day, "post_at": f"{m // 60:02d}:{m % 60:02d}", "posted": False, "post_tries": 0,
            "summary_done": False, "paused": old.get("paused", False), "last_poll": old.get("last_poll", 0),
            "comment_day": day, "comment_count": 0, "friend_day": day, "friend_count": 0, "friend_uins": []}


def state() -> dict:
    day = _now().strftime("%Y-%m-%d")
    st = _read(STATE_FILE, {})
    if st.get("date") != day:
        st = _new_day_state(day)
        _write(STATE_FILE, st)
        logger.info(f"QQ 空间：今天的说说定在 {st['post_at']} 发")
    return st


def save_state(st: dict) -> None:
    _write(STATE_FILE, st)


async def daily_summary() -> int:
    """日结：今天有新消息、但还没攒够一批的会话，先整理一次"""
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    keys = [k for k in ltm.pending_keys(since=start) if k.startswith(("group_", "private_", "qzone_"))]
    for k in keys:
        await ltm.summarize(k, force=True)
    return len(keys)


_poll_gap = 0.0


_retry_poll_at = 0.0     # 上一轮有回复被闸门挡住：到这个时间再查一次（不用等满 2 小时）
_was_quiet = False       # 上一次 tick 是不是在勿扰时段 / 高峰里
_release_at = 0.0        # 勿扰 / 高峰结束后，到这个时间才开始查


async def _adopt_recent_post() -> bool:
    """最近 40 分钟里空间有没有她新发的说说（上次“结果不确定”其实发出去了）；有就记下来，返回 True"""
    known = {p["tid"] for p in _read(POSTS_FILE, [])}
    for p in await qz.list_posts(3):
        if int(p.get("created_time") or 0) >= time.time() - 40 * 60:
            tid = str(p.get("tid"))
            if tid not in known:
                posts = _read(POSTS_FILE, [])
                posts.append({"tid": tid, "ts": int(p.get("created_time") or time.time()),
                              "text": str(p.get("content") or ""), "image": "", "desc": "", "moments": []})
                _write(POSTS_FILE, posts[-60:])
            logger.info(f"QQ 空间：上次的说说其实发出去了（tid={tid}），不再重发")
            return True
    return False


async def _warm_cookie() -> None:
    """要写空间但还没有凭证：先要一个，然后按闸门等几分钟再写（别“要到凭证几秒就写”）"""
    if not qz.cookie_at:
        try:
            await qz.ctx()
        except QzoneError as e:
            logger.warning(f"QQ 空间：要凭证失败：{e}")


async def tick() -> None:
    global _poll_gap, _retry_poll_at
    st = state()
    if st.get("paused") or read_block():
        return                       # 下线、熔断、刚上线（30～60 分钟内读也不读）：空间一概不碰
    now = _now()
    minute = now.hour * 60 + now.minute
    window = peak.parse_ranges(cfg.qzone_post_window)
    end = window[0][1] if window else 24 * 60

    if not st["summary_done"] and minute >= _hm(cfg.qzone_daily_summary_at) and not in_peak():
        st["summary_done"] = True
        save_state(st)
        n = await daily_summary()
        logger.info(f"QQ 空间：日结完成，整理了 {n} 个会话")

    if not st["posted"] and _hm(st["post_at"]) <= minute < end + 60 and st.get("post_tries", 0) < 3:
        last = recent_posts(1)
        if last and time.time() - last[-1]["ts"] < cfg.qzone_post_min_gap_hours * 3600:
            st["posted"] = True          # 离上一条（比如手动发的）还不到 12 小时：今天这条不发了
            save_state(st)
            logger.info(f"QQ 空间：离上一条说说不到 {cfg.qzone_post_min_gap_hours:g} 小时，今天的定时说说跳过")
            return
        if write_block():
            await _warm_cookie()
            return                   # 到点了但现在还不能写：过几分钟再来，不算失败次数
        if st.get("post_uncertain"):
            # 上次发的时候超时/格式不对，不知道发没发出去：先看看空间里是不是已经有了，免得发两条
            try:
                if await _adopt_recent_post():
                    st = state()
                    st["posted"], st["post_uncertain"] = True, False
                    save_state(st)
                    return
            except QzoneError as e:
                logger.info(f"QQ 空间：查上次说说发没发出去失败（{e}），这次先不发")
                return
            st = state()
            st["post_uncertain"] = False
            save_state(st)
        st["post_tries"] = st.get("post_tries", 0) + 1
        save_state(st)
        if not st["summary_done"]:
            st["summary_done"] = True
            save_state(st)
            await daily_summary()
        try:
            await publish(await make_post())
            st = state()
            st["posted"] = True
            save_state(st)
        except Exception as e:  # noqa: BLE001
            st = state()
            if isinstance(e, QzoneError) and e.kind == "hold":
                st["post_tries"] = max(0, st.get("post_tries", 1) - 1)     # 被闸门挡住不算失败
                save_state(st)
                logger.info(f"QQ 空间：说说先不发（{e.msg}）")
                return
            if isinstance(e, QzoneError) and e.kind in ("network", "format", "page"):
                st["post_uncertain"] = True                  # 不知道发没发出去：下次先查再发
                save_state(st)
            logger.warning(f"QQ 空间：第 {st['post_tries']} 次发说说失败：{e}")
            # 10 分钟后再试（最多 3 次）
            later = min(end + 59, minute + 10)
            st["post_at"] = f"{later // 60:02d}:{later % 60:02d}"
            save_state(st)
            if st["post_tries"] >= 3:
                await _alert(e if isinstance(e, QzoneError) else QzoneError("api", str(e)), "发说说（今天试了 3 次）")

    global _was_quiet, _release_at
    quiet_now = in_peak() or _in_ranges(cfg.qzone_comment_quiet, minute)
    if quiet_now:
        _was_quiet = True
    elif _was_quiet:                 # 勿扰时段 / 高峰刚结束：再随机推迟一会儿，别每天 09:00 准点查
        _was_quiet = False
        _release_at = time.time() + random.uniform(0, cfg.qzone_quiet_release_max_minutes * 60)
    if cfg.qzone_comment_reply and not quiet_now and time.time() >= _release_at:
        if not _poll_gap:
            _poll_gap = cfg.qzone_comment_poll_minutes * 60 * random.uniform(0.85, 1.15)
        due = time.time() - float(st.get("last_poll") or 0) >= _poll_gap
        retry = _retry_poll_at and time.time() >= _retry_poll_at
        if due or retry:
            _poll_gap = 0.0
            _retry_poll_at = 0.0
            try:
                logger.info("QQ 空间：" + await poll_all())
            except QzoneError as e:
                st = state()
                st["last_poll"] = time.time()
                save_state(st)
                if e.kind == "hold":
                    if _online_since is not None:          # 只是刚要到凭证：到点再查一次，把没回的回掉
                        _retry_poll_at = write_ready_at() + random.uniform(30, 120)
                    logger.info(f"QQ 空间：这一轮先不回复（{e.msg}）")
                    return
                logger.warning(f"QQ 空间：查评论失败：{e}")
                await _alert(e, "查评论")


_task: "asyncio.Task | None" = None


async def _loop() -> None:
    await asyncio.sleep(90)
    try:
        await gallery.prepare()          # 启动后先把新图处理掉（角色识别模型一般已经准备好了）
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图库处理出错：{e}")
    while True:
        try:
            await tick()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"QQ 空间定时任务出错：{e}")
        await asyncio.sleep(60)


@get_driver().on_startup
async def _():
    global _task
    if cfg.qzone_enabled:
        _task = asyncio.create_task(_loop())
        st = state()
        logger.info(f"QQ 空间日记已开启：今天{'已经发过' if st['posted'] else '定在 ' + st['post_at'] + ' 发'}"
                    f"｜评论回复={'开' if cfg.qzone_comment_reply else '关'}")
    else:
        logger.info("QQ 空间日记没开（.env 里 QZONE_ENABLED=true 开启）")


@get_driver().on_shutdown
async def _():
    if _task:
        _task.cancel()
    await qz.close()


# ------------------------------------------------------------------ 管理员指令
_last_draft: dict | None = None

USAGE = ("/说说 预览：生成一条给你看，不发\n/说说 发预览：把刚才预览的那条发出去\n/说说 立即发：马上生成并发出（离它不到 12 小时的定时说说会跳过）\n"
         "/说说 删除最新：删掉最近一条\n/说说 今日：今天的见闻和发布安排\n/说说 图库：图库情况\n/说说 查评论：马上查一轮评论（自己说说下的、别人空间里 @ 她的）并回复，顺便刷好友动态\n"
         "/说说 好友动态：只看不评，列出最近好友发的说说和她评论的几率\n"
         "/说说 日结：马上把今天的聊天整理成见闻\n/说说 暂停 / 恢复：停掉或恢复自动发说说和回评论（恢复也会解除风控熔断）")

diary_cmd = on_command("说说", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


def _draft_msg(d: dict, head: str) -> Message:
    img = gallery.items.get(d["image"])
    msg = Message(head + "\n")
    if img:
        msg += MessageSegment.image(gallery.image_bytes(img))
    msg += f"\n{d['text']}\n\n（图：{d['image']}｜见闻：{'；'.join(d['moments']) or '没用'}）"
    return msg


@diary_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    global _last_draft
    cmd = arg.extract_plain_text().strip()
    blocked = risk_block()
    if blocked and cmd in ("发预览", "立即发", "删除最新", "查评论", "好友动态"):
        await diary_cmd.finish(f"（空间功能{blocked}。这时候再动空间容易让设备被下线；确定要恢复就先发 /说说 恢复）")
    if cmd in ("查评论", "好友动态") and read_block():
        await diary_cmd.finish(f"（先不查：{read_block()}）")
    if cmd == "删除最新" and write_block():
        await _warm_cookie()
        await diary_cmd.finish(f"（先不删：{write_block()}）")
    try:
        if cmd == "预览":
            _last_draft = await make_post()
            await diary_cmd.finish(_draft_msg(_last_draft, "（预览，还没发。要发这条就回 /说说 发预览）"))
        elif cmd == "发预览":
            if not _last_draft or time.time() - _last_draft["at"] > 7200:
                await diary_cmd.finish("（没有两小时内的预览，先 /说说 预览）")
            why = write_block()
            if why:
                await _warm_cookie()
                await diary_cmd.finish(f"（先不发：{write_block() or why}。预览还留着，过会儿再发 /说说 发预览）")
            tid = await publish(_last_draft)
            _last_draft = None
            await diary_cmd.finish(f"（发出去了 tid={tid}。离这条不到 {cfg.qzone_post_min_gap_hours:g} 小时的定时说说会跳过）")
        elif cmd == "立即发":
            why = write_block()
            if why:
                await _warm_cookie()
                await diary_cmd.finish(f"（先不发：{write_block() or why}）")
            d = await make_post()
            tid = await publish(d)
            await diary_cmd.finish(_draft_msg(d, f"（已发出 tid={tid}。离这条不到 {cfg.qzone_post_min_gap_hours:g} 小时的定时说说会跳过）"))
        elif cmd == "删除最新":
            posts = _read(POSTS_FILE, [])
            if not posts:
                await diary_cmd.finish("（没有她发过的说说记录）")
            p = posts.pop()
            await qz.delete(p["tid"])
            _write(POSTS_FILE, posts)
            await diary_cmd.finish(f"（已删除：{p['text'][:30]}）")
        elif cmd == "今日":
            st = state()
            ms = today_moments()
            lines = [f"（今天：{'已发' if st['posted'] else '定在 ' + st['post_at'] + ' 发'}｜日结{'已做' if st['summary_done'] else '还没做'}"
                     f"｜回评论 {st.get('comment_count', 0)}/{cfg.qzone_comment_daily_max}"
                     f"｜评好友 {st.get('friend_count', 0) if st.get('friend_day') == st['date'] else 0}/{cfg.qzone_friend_comment_daily_max}"
                     f"｜{'⏸ 已暂停' if st.get('paused') else ('自动运行中' if cfg.qzone_enabled else '总开关没开')}）"]
            if blocked:
                lines.append(f"⚠️ 空间功能{blocked}")
            wb = write_block()
            if wb:
                lines.append(f"⏳ 现在不写空间：{wb}")
            lines.append(f"今日见闻 {len(ms)} 件：" if ms else "今日见闻：还没有")
            for m in ms[-12:]:
                who = ltm.get_user(int(m["qq"])).get("name") or m["qq"]
                lines.append(f"· {m['event']}（{m.get('mood', '')}｜{who}·{ltm.TIER_NAMES[familiarity_of(int(m['qq']))]}｜{m['src']}）")
            await diary_cmd.finish("\n".join(lines))
        elif cmd == "图库":
            n = await gallery.prepare()
            await diary_cmd.finish((f"（刚处理了 {n} 张新图）\n" if n else "") + gallery.stats(cfg.qzone_image_reuse_days))
        elif cmd == "查评论":
            await diary_cmd.finish("（" + await poll_all() + "）")
        elif cmd == "好友动态":
            me = (await qz.ctx()).uin
            items = await qz.friend_feeds(20)
            cands = friend_candidates(me, items)
            seen = _seen()
            lines = [f"（只看不评：好友动态读到 {len(items)} 条，其中 {cfg.qzone_friend_post_max_age_hours:g} 小时内好友自己发的说说 {len(cands)} 条"
                     f"｜今天已评 {cfg.qzone_friend_comment_daily_max - _friend_quota_left()}/{cfg.qzone_friend_comment_daily_max}"
                     f"｜{'开' if cfg.qzone_friend_comment else '关'}）"]
            for it in cands[:10]:
                fam = familiarity_of(it["owner"])
                when = datetime.fromtimestamp(it["abstime"], BJ).strftime("%H:%M")
                mark = "已看过" if _friend_key(it["owner"], it["tid"]) in seen else "还没抽"
                lines.append(f"· {when} {it['nick'] or it['owner']}（{ltm.TIER_NAMES.get(fam, fam)}，"
                             f"几率 {cfg.qzone_friend_comment_prob.get(fam, 0.0):.0%}，{mark}）")
            await diary_cmd.finish("\n".join(lines))
        elif cmd == "日结":
            n = await daily_summary()
            await diary_cmd.finish(f"（整理了 {n} 个会话，今日见闻现在有 {len(today_moments())} 件）")
        elif cmd in ("暂停", "恢复"):
            st = state()
            st["paused"] = cmd == "暂停"
            save_state(st)
            if cmd == "恢复" and blocked:
                RISK_FILE.unlink(missing_ok=True)
                log_event("管理员手动解除空间熔断")
                await diary_cmd.finish(f"（已解除熔断并恢复。之前{blocked}）")
            await diary_cmd.finish("（已暂停自动发说说和回评论）" if st["paused"] else "（已恢复）")
        else:
            await diary_cmd.finish(USAGE)
    except QzoneError as e:
        if e.kind == "hold":
            await diary_cmd.finish(f"（先不动空间：{e.msg}）")
        await diary_cmd.finish(f"（QQ 空间接口出错：{e}。详细记录在 data\\qzone\\qzone.log）")
    except RuntimeError as e:
        await diary_cmd.finish(f"（没成：{e}）")


# ------------------------------------------------------------------ 断线记录（对照“风险设备下线”的时间用）
def _context_line() -> str:
    now = time.monotonic()
    replies = sum(1 for t in _hour_window if now - t <= 3600)
    lw = qz.last_write
    if lw:
        ago = (time.time() - lw[0]) / 60
        last = f"最近一次空间写操作：{ago:.0f} 分钟前（{lw[1]}）"
    else:
        last = "没有空间写操作的记录"
    return f"最近一小时回复 {replies} 条｜{last}｜空间功能{'开' if cfg.qzone_enabled else '关'}"


def _go_online() -> None:
    global _online_since, _grace
    _online_since = time.time()
    _grace = _new_grace()
    qz.forget_ctx()


def _go_offline(note: str) -> None:
    global _online_since, _offline_note
    _online_since = None
    _offline_note = note
    qz.forget_ctx()                  # 被踢那次登录的凭证别再用（9/26 被踢后程序还拿它回了一条评论）


@get_driver().on_bot_connect
async def _(bot: Bot):
    _go_online()
    log_event(f"机器人连上 NapCat（QQ {bot.self_id}）｜空间功能 {_grace / 60:.0f} 分钟后（{datetime.fromtimestamp(_online_since + _grace, BJ):%H:%M}）才开始动")


@get_driver().on_bot_disconnect
async def _(bot: Bot):
    _go_offline("机器人和 NapCat 断开了")
    log_event(f"机器人和 NapCat 断开（QQ {bot.self_id}；可能是被下线，也可能是 NapCat 关了）｜{_context_line()}")


async def _is_offline_notice(event: NoticeEvent) -> bool:
    return "offline" in str(getattr(event, "notice_type", "")).lower()


offline_notice = on_notice(rule=_is_offline_notice, priority=1, block=False)


@offline_notice.handle()
async def _(event: NoticeEvent):
    detail = {k: v for k, v in event.model_dump().items() if k not in ("time", "self_id", "post_type")}
    _go_offline("账号被下线了")
    log_event(f"NapCat 报告账号下线：{detail}｜{_context_line()}｜空间功能已停，重新上线后再恢复")
    logger.warning(f"账号被下线：{detail}")


# 下线后 NapCat 有时不断开连接、自己重新登录：又收到消息时，问一下 NapCat 账号是不是真的在线了（迟到的旧消息不算）
_last_online_check = 0.0


async def _back_online(event: MessageEvent) -> bool:
    return _online_since is None


back_online = on_message(rule=_back_online, priority=0, block=False)


async def _really_online(bot: Bot) -> bool:
    try:
        st = await bot.call_api("get_status")
        if isinstance(st, dict) and "online" in st:
            return bool(st.get("online"))
    except Exception:  # noqa: BLE001
        pass
    try:
        info = await bot.call_api("get_login_info")
        return bool(isinstance(info, dict) and info.get("user_id"))
    except Exception:  # noqa: BLE001
        return False


@back_online.handle()
async def _(bot: Bot):
    global _last_online_check
    if time.time() - _last_online_check < 60:       # 别每条消息都去问
        return
    _last_online_check = time.time()
    if not await _really_online(bot):
        return
    _go_online()
    log_event(f"下线后 NapCat 确认账号重新在线了｜空间功能 {_grace / 60:.0f} 分钟后（{datetime.fromtimestamp(_online_since + _grace, BJ):%H:%M}）才开始动")
