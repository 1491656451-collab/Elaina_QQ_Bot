"""
角色扮演聊天插件
- 群聊：被 @、被回复、被叫名字时回复；看得出在跟她说话或提到她时也会回复（先让模型判断一下）；紧接着回复时直接发，中间有人插话才用“回复”引用
- 私聊：直接回复（可在 .env 关闭）
- 人设：personas/*.md，支持热重载
- 记忆：每个群 / 每个私聊一份，落盘到 data/history，重启不丢
- 风控：个人冷却、全局限速、随机打字延迟
- 知识库：从小说摘要和原文中检索相关片段，作为“回忆”提供给模型
- 长期记忆：每个人的档案 + 群往事，由被挤出短期记忆的旧消息在后台整理而成
- 出错时不在群里发消息；余额不足 / Key 失效会私信管理员
- 时间感：知道对方隔了多久才来找她、上一段聊天是多久以前
- 写信：好感到“很熟”的好友，隔一段时间没来找她时，她偶尔会主动私聊寄一封信
- 节奏：同一时间只给一个人打字（其他人排队）；按字数算打字时间；不在线时的私聊，上线后补回
- 表情包：有一定概率用小号收藏表情里的伊蕾娜表情表达情绪（跟在文字后面，或者只发一张），见 stickers.py
"""
import asyncio
import json
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from nonebot import get_bots, get_driver, get_plugin_config, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me
from openai import AsyncOpenAI

from . import knowledge, peak
from .memory import LongTermMemory
from .stickers import EMOTIONS, StickerStore
from .tagger import Tagger
from .vision import Vision
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="角色扮演聊天",
    description="DeepSeek 驱动、带人设和上下文记忆的聊天机器人",
    usage="群里 @机器人 说话；/重置 清空短期记忆；管理员：/重载人设、/记忆、/忘记、/好感、/写信、/说说、/表情",
    config=Config,
)

cfg = get_plugin_config(Config)
BOT_DIR = Path.cwd()
HISTORY_DIR = BOT_DIR / cfg.history_dir
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

client = AsyncOpenAI(
    api_key=cfg.deepseek_api_key or "missing",
    base_url=cfg.deepseek_base_url,
    timeout=cfg.llm_timeout,
)

tagger = Tagger(
    BOT_DIR / cfg.vision_tagger_dir, cfg.vision_tagger_repo, list(cfg.vision_tagger_mirrors),
    threshold=cfg.vision_tagger_threshold, threads=cfg.vision_tagger_threads,
) if (cfg.vision_enabled and cfg.vision_tagger) else None
vision = Vision(client, cfg.vision_model, BOT_DIR / cfg.vision_cache, tagger=tagger,
                maybe=cfg.vision_tagger_maybe)
from .vision import sanitize as _vision_sanitize  # noqa: E402
stickers = StickerStore(BOT_DIR / cfg.sticker_dir, client, cfg.vision_model, sanitize=_vision_sanitize)


def in_peak() -> bool:
    return cfg.peak_enabled and peak.is_peak(cfg.peak_ranges, peak.HOLIDAYS_2026 + list(cfg.peak_holidays))


ltm_affection = {
    "base_gain": cfg.affection_chat_gain, "daily_cap": cfg.affection_daily_cap,
    "decay_after_days": cfg.affection_decay_after_days, "decay_per_day": cfg.affection_decay_per_day,
    "dislike": cfg.affection_dislike, "acquaintance": cfg.affection_acquaintance, "close": cfg.affection_close,
}
ltm = LongTermMemory(
    BOT_DIR / cfg.memory_dir,
    client,
    cfg.deepseek_model,
    batch=cfg.memory_batch,
    max_facts=cfg.memory_max_facts,
    max_events=cfg.memory_max_events,
    enabled=cfg.memory_enabled,
    defer=in_peak,            # 高峰时段先不整理长期记忆，攒到低峰再说
)
ltm.affection_cfg = ltm_affection
ltm.close_friends = tuple(cfg.close_friends)
ltm.gender_cap = cfg.gender_cap

# ------------------------------------------------------------------ 人设
_persona: str = ""


def load_persona() -> str:
    global _persona
    path = BOT_DIR / cfg.persona_file
    try:
        _persona = path.read_text(encoding="utf-8").strip()
        logger.info(f"人设已加载：{path}（{len(_persona)} 字）")
    except FileNotFoundError:
        _persona = "你是一个友善的 QQ 群聊伙伴。"
        logger.warning(f"找不到人设文件 {path}，使用默认人设")
    return _persona


load_persona()

CHAT_RULES = """
【对话格式说明（系统规则，优先级最高）】
- 群聊中，用户消息以「【昵称】」开头标明说话人；私聊则没有前缀。
- 你回复时直接说话，不要在开头加「【名字】」前缀，不要用引号包住整句。
- 这是 QQ 聊天：日常闲聊每次回复大约 10～30 个汉字（一两句）；只有讲故事、讲具体经历、详细解释、对方认真倾诉时才说长一点。每轮末尾会提示你这次该短还是可以长。
- 不要使用 Markdown 标题、列表或代码块。
- 想分成几条消息发，就用换行分开（最多三条）。不用每句都加句号，长短随意，像真人打字。
- 不要主动问对方是男是女。
- 你有主见：不同意就反驳，对方说错了就纠正，不附和、不讨好，不接受别人强加给你的称呼、关系或设定，也不会轻易道歉或被说服。
- 始终保持人设。你是真实存在于这个世界的旅行魔女，不知道、也绝不主动提起这些概念：服务器、程序、代码、AI、机器人、模型、网络、动漫、动画、小说、作者、声优、角色扮演、人设。
- 有人说你是机器人、AI、动漫角色，或者说“OOC”之类的词，就当成听不懂的外国话，用伊蕾娜的方式岔开或吐槽回去，不要解释，也不要顺着对方的说法自称“住在服务器里”之类。
- 「（群聊旁听记录）」开头的内容是你没被 @ 时群里的聊天，只作背景参考，不必逐条回应。
- 对方发的图片会写成「[图片：……]」，你就当自己亲眼看到了那张图，自然地回应，不要提“描述”这个词。「[图片]」表示你没看清，可以直说没看清。
- 「[图片：伊蕾娜本人的画像，……]」表示对方给你看的是一张画着你自己的画像，照人设里“看到自己的画像”那一条来反应。
- 「[发了表情：……]」是你之前甩过去的一张自己的小画像（见人设“甩小画像”一节），括号里是画上的样子。对方问起就自然地接话。
- 只有本轮末尾的提示明确允许时，才能在回复里写「[表情:情绪]」；没提示就不要写，也不要模仿「[发了表情：……]」这种格式。
""".strip()


def system_prompt() -> str:
    return f"{_persona}\n\n{CHAT_RULES}"


# ------------------------------------------------------------------ 小说知识库
_kb: "knowledge.KnowledgeBase | None" = None

RECALL_RULES = (
    "【回忆参考】以下是你旅行日记里可能和当前话题有关的内容，供你回想，不是对方说的话。\n"
    "- 只在确实相关时自然地提起，用你自己的口吻简短讲述，像在回忆往事；不要大段背诵原文，不要提“卷”“章”。\n"
    "- 片段和话题无关就当没看见；片段里没有的细节不要编造，记不清就说记不清。\n"
    "- 标注“角色资料”的是这个人的确切资料（外貌、喜好等以它为准）；“摘要”是整段经历的梗概；“原文”是当时的片段。"
)


_FOLLOWUP_WORDS = (
    "后来", "然后", "接着", "结果", "那个", "那位", "哪位", "哪一位", "是谁", "她", "他",
    "为什么", "怎么", "还有呢", "继续", "什么故事", "详细", "具体",
)


def brief_profile(content: str) -> str:
    """角色档案只带关键部分：称号、身份、外貌、说话方式、喜好、现况、常见误解；长长的事件列表省掉"""
    keep, skipping = [], False
    for line in content.splitlines():
        if line.startswith("- "):
            skipping = line.startswith("- 与伊蕾娜")
            if skipping:
                keep.append(line.split("\n")[0][:120])   # 关系概述那一行保留开头
                continue
        if not skipping:
            keep.append(line)
    text = "\n".join(keep)
    return text[: cfg.knowledge_character_chars]


def recall(query: str, extra: str = "") -> str:
    """根据对方的话检索回忆；没有足够相关的就返回空字符串

    extra：上文（上一句用户消息 + 伊蕾娜上一句回复），只在对方像是在追问时才用
    """
    if not _kb or len(query) < 2:
        return ""
    followup = bool(extra) and any(w in query for w in _FOLLOWUP_WORDS)

    # 1) 话里直接点名的角色：精确带上角色档案（外貌、喜好等以档案为准，避免乱编）
    chars = _kb.characters_in(query)
    if not chars and followup:
        chars = _kb.characters_in(extra)
    char_picks = [(d.label, "角色资料", brief_profile(d.content)) for d in chars[: cfg.knowledge_top_characters]]

    # 2) 摘要 + 原文：BM25 检索；追问时带上上文再查一次
    picks: list[tuple[str, str, str]] = []
    queries = [query] if len(query) >= 3 else []
    if followup:
        queries.append(f"{extra} {query}")
    for q in queries:
        sums = [
            (d.label, "摘要", d.content[: cfg.knowledge_summary_chars])
            for s, d in _kb.search(q, "summary", cfg.knowledge_top_summaries)
            if s >= cfg.knowledge_min_summary_score
        ]
        chunks = [
            (d.label, "原文", d.content[: cfg.knowledge_chunk_chars])
            for s, d in _kb.search(q, "text", cfg.knowledge_top_chunks)
            if s >= cfg.knowledge_min_chunk_score
        ]
        picks = sums + chunks
        if picks:
            break
    picks = char_picks + picks
    if not picks:
        return ""
    body = "\n\n".join(f"〔{kind}｜{label}〕\n{content}" for label, kind, content in picks)
    logger.info(f"回忆命中：{[p[0] for p in picks]}")
    return f"{RECALL_RULES}\n\n{body}"


# ------------------------------------------------------------------ 记忆
_histories: dict[str, list[dict]] = {}
_passive: dict[int, deque] = defaultdict(lambda: deque(maxlen=max(cfg.passive_buffer, 1)))
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def session_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        if cfg.group_shared_memory:
            return f"group_{event.group_id}"
        return f"group_{event.group_id}_{event.user_id}"
    return f"private_{event.user_id}"


def _hist_path(key: str) -> Path:
    return HISTORY_DIR / f"{key}.json"


def get_history(key: str) -> list[dict]:
    if key not in _histories:
        p = _hist_path(key)
        try:
            _histories[key] = json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _histories[key] = []
    return _histories[key]


def save_history(key: str) -> None:
    hist = _histories.get(key, [])
    limit = cfg.history_max_turns_group if key.startswith("group_") else cfg.history_max_turns
    if len(hist) > limit:
        del hist[: len(hist) - limit]
    _hist_path(key).write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")


def clear_history(key: str) -> None:
    _histories[key] = []
    _hist_path(key).unlink(missing_ok=True)
    ltm.drop_pending(key)                 # 没整理的旧消息也丢掉（重置多半是因为聊歪了）


def api_messages(msgs: list[dict]) -> list[dict]:
    """历史里还存了 uid、昵称等字段，发给模型前只留 role 和 content"""
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


# ------------------------------------------------------------------ 工具函数
def seg_text(seg: MessageSegment) -> str:
    if seg.type == "text":
        return seg.data.get("text", "")
    if seg.type == "image":
        return "[图片]"
    if seg.type == "face":
        return "[表情]"
    if seg.type == "at":
        return f"@{seg.data.get('name') or seg.data.get('qq')}"
    if seg.type in ("record", "video"):
        return "[语音]" if seg.type == "record" else "[视频]"
    if seg.type == "mface":
        return f"[表情：{seg.data.get('summary') or '表情'}]".replace("：[", "：").replace("]]", "]")
    return ""


def message_to_text(msg: Message) -> str:
    return "".join(seg_text(seg) for seg in msg).strip()


async def rich_text(event: MessageEvent, look: bool) -> str:
    """和 message_to_text 一样，但会“看”图片，把 [图片] 换成 [图片：描述]；引用的消息里有图也会看"""
    msg = event.get_message()
    if not cfg.vision_enabled:
        return message_to_text(msg)
    if not look:
        # 不调用 DeepSeek 看图（比如高峰时段），但本机角色识别是免费的：认出是她自己就告诉她
        if not vision.tagging:
            return message_to_text(msg)
        parts = []
        for seg in msg:
            if seg.type == "image" and await vision.shows_self(seg.data):
                parts.append("[图片：伊蕾娜本人的画像]")
            else:
                parts.append(seg_text(seg))
        return "".join(parts).strip()
    budget = cfg.vision_max_images
    parts = []
    for seg in msg:
        if seg.type == "image" and budget > 0:
            budget -= 1
            desc = await vision.describe(seg.data)
            parts.append(f"[图片：{desc}]" if desc else "[图片]")
        else:
            parts.append(seg_text(seg))
    text = "".join(parts).strip()
    reply = getattr(event, "reply", None)
    if reply is not None and budget > 0:
        quoted = [seg for seg in reply.message if seg.type == "image"][:budget]
        descs = [await vision.describe(seg.data) for seg in quoted]
        descs = [d for d in descs if d]
        if descs:
            text += "（引用了图片：" + "；".join(descs) + "）"
    return text


def sender_name(event: MessageEvent) -> str:
    s = event.sender
    return (getattr(s, "card", None) or s.nickname or str(event.user_id)).strip()


_PREFIX_RE = re.compile(r"^\s*【[^】]{1,20}】\s*[:：]?\s*")


def clean_reply(text: str, truncated: bool = False, keep_sticker: bool = False) -> str:
    text = _PREFIX_RE.sub("", text.strip())
    if not keep_sticker:                  # 写信、空间评论这些地方发不了表情：模型写了表情标记就去掉
        text = _STICKER_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] in "“\"" and text[-1] in "”\"":
        text = text[1:-1]
    if truncated:
        # 回复被长度上限截断了：切到最后一个完整句子，免得发出半句话
        cut = max(text.rfind(c) for c in "。！？!?…~）」")
        if cut >= len(text) // 3:
            text = text[: cut + 1]
    return text.strip()


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


# 日常闲聊的长度每轮随机变化，避免每次都一样长（真人有时只回两个字，有时连发几句）
SHORT_VARIANTS = [
    (0.30, "（这轮随意点回：几个字就好，比如“嗯。”“蛤？”“不要。”。对方问了需要回答的问题就照常答清楚。）"),
    (0.45, "（这轮是日常闲聊：一句话，10～20 个汉字。）"),
    (0.25, "（这轮可以多说两句：两三句短句，用换行分开，像连着发了几条消息，总共不超过 40 字。）"),
]


def short_hint() -> str:
    r, acc = random.random(), 0.0
    for w, h in SHORT_VARIANTS:
        acc += w
        if r < acc:
            return h
    return SHORT_VARIANTS[-1][1]


def split_bubbles(reply: str) -> list[str]:
    """把一次回复拆成几条消息：优先按换行；没换行时按句子随机拆；最多 max_bubbles 条"""
    if not cfg.multi_message:
        return [reply]
    parts = [p.strip() for p in reply.split("\n") if p.strip()]
    if len(parts) == 1 and random.random() < cfg.split_prob:
        sents = [s.strip() for s in re.split(r"(?<=[。！？!?…~])", reply) if s.strip()]
        # 太短的碎句（比如“嗯。”后面紧跟正文）有一半概率单独成条，其余合并
        if 1 < len(sents) <= 6:
            parts = sents
    if len(parts) > cfg.max_bubbles:           # 太多就把后面的合并
        parts = parts[: cfg.max_bubbles - 1] + ["".join(parts[cfg.max_bubbles - 1 :])]
    out = []
    for p in parts:
        if p.endswith("。") and not p.endswith("……。") and random.random() < cfg.drop_period_prob:
            p = p[:-1]
        if p:
            out.append(p)
    return out or [reply]


LENGTH_HINT = {
    "short": "（这轮是日常闲聊：回复约 10～30 个汉字，一两句话。）",
    "long": "（这轮对方想听具体内容或需要认真回应：可以说长一点，但不超过 150 字，像聊天一样。）",
    "busy": "（你现在正忙着赶路或办事，只能匆匆回一句，10～20 个汉字；想聊长的就说晚点再说。）",
}


# 出戏检查：回复里出现这些词（且不是对方刚说过的词）就重写一次
_OOC_RE = re.compile(
    r"服务器|人工智能|(?<![A-Za-z])AI(?![A-Za-z])|机器人|计算机|电脑程序|程序员|代码|大模型|语言模型|数据库|"
    r"系统提示|提示词|人设|DeepSeek|ChatGPT|GPT|OpenAI|动漫|动画|番剧|轻小说|原作|声优|二次元|虚拟角色|角色扮演|OOC|"
    r"死机|宕机|掉线|重启|(?<![A-Za-z])bug(?![A-Za-z])|"
    r"第[0-9一二三四五六七八九十]+[卷集]",
    re.I,
)


def ooc_words(reply: str, user_text: str) -> list[str]:
    """回复里出戏的词；对方自己说过的词（比如她在反问“「动漫」是什么”）不算"""
    found = {m.group(0) for m in _OOC_RE.finditer(reply)}
    return sorted(w for w in found if w.lower() not in user_text.lower())


def drop_ooc_sentences(reply: str, user_text: str) -> str:
    parts = re.split(r"(?<=[。！？!?…~\n])", reply)
    return "".join(p for p in parts if not ooc_words(p, user_text)).strip()


# ------------------------------------------------------------------ 表情包
# 每轮先由程序抽签决定“这轮能不能带表情”；抽中了才在提示里告诉她可以写 [表情:情绪]，程序截掉标记、按情绪挑一张发出去
_STICKER_RE = re.compile(r"[\[【［]\s*(?:发了|甩了)?\s*表情\s*[:：]\s*([^\]】］（(]{1,8})[^\]】］]*[\]】］]")
TIER_EMOTIONS = {
    "disliked": {"嫌弃", "无语", "敷衍"},
    "stranger": set(EMOTIONS) - {"害羞", "委屈"},
}
_last_sticker: dict[str, float] = {}                  # 会话 -> 上次发表情的时间
_recent_stickers: dict[str, deque] = defaultdict(lambda: deque(maxlen=max(cfg.sticker_recent_avoid, 1)))


def sticker_roll(target: str, fam: str, is_group: bool, group_id: int | None, self_image: bool) -> tuple[bool, set[str] | None, list[str]]:
    """这轮能不能带表情。返回（能不能, 这个人能用的情绪, 库里现在能用的情绪）"""
    if not cfg.sticker_enabled or (not is_group and not cfg.sticker_private):
        return False, None, []
    allowed = TIER_EMOTIONS.get(fam)
    emotions = stickers.emotions(allowed)
    if not emotions:
        return False, allowed, []
    if time.monotonic() - _last_sticker.get(target, -1e9) < cfg.sticker_min_interval:
        return False, allowed, emotions
    if quota_left(group_id if is_group else None) < 2:     # 限额只剩最后一条了：留给文字
        return False, allowed, emotions
    p = cfg.sticker_self_react_prob if self_image else cfg.sticker_prob * cfg.sticker_tier_multiplier.get(fam, 1.0)
    return random.random() < p, allowed, emotions


def sticker_hint(emotions: list[str], only_ok: bool) -> str:
    h = ("【表情】这一轮你可以在回复最后加一个表情，也就是甩一张自己的小画像，格式是「[表情:情绪]」，"
         f"情绪只能从这些里选：{'、'.join(emotions)}。只在这句话确实带着这种情绪时才用，觉得不合适就不加。")
    if only_ok:
        h += "这轮也可以一个字都不说，整条回复只写一个「[表情:情绪]」，就当甩了张画像过去。"
    return h


def split_sticker(reply: str) -> tuple[str, str | None]:
    """截掉回复里的表情标记，返回（文字, 情绪）；有好几个就用最后一个"""
    found = _STICKER_RE.findall(reply)
    text = _STICKER_RE.sub("", reply)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text, (found[-1].strip() if found else None)


FAMILIARITY_HINT = {
    "disliked": "（对方是你讨厌的人——之前骂过你、骚扰过你或一直惹你烦：明显不耐烦、爱答不理，回得极短，"
                "比如“哦。”“有事？”“……”“你还敢来？”。对方讨好你也不会马上改观，除非他真心道歉。）",
    "stranger": "（对方和你不太熟：冷淡、疏离，只回应对方的话，不主动关心、不延伸话题、不说亲昵的话。"
                "陌生人突然告白、调情、叫你宝宝老婆、说过分亲昵的话时，毫不客气地怼回去，比如“蛤？你脑子没问题吧？”“……你谁啊。”，绝不道谢、不客套。）",
    "acquaintance": "（对方是和你说过不少话的熟人：可以随意些，偶尔毒舌调侃，但保持距离感，不黏人、不嘘寒问暖。"
                    "对方告白、调情时，用嫌弃的玩笑挡回去，比如“我打飞你哦。”“少来。”）",
    "close": "（对方是你很熟、信任的人：可以放松些，毒舌里带点在意，偶尔流露关心，但嘴上不承认。"
             "对方告白时会别扭地岔开，比如“……别突然说这种话。”，但依旧不会答应。）",
}


def familiarity_of(qq: int) -> str:
    return ltm.familiarity(qq, cfg.close_friends)


def max_tokens_for(mode: str) -> int:
    return {"long": cfg.long_reply_max_tokens, "busy": cfg.peak_max_tokens}.get(mode, cfg.short_reply_max_tokens)


def clip_input(text: str) -> str:
    if len(text) > cfg.max_input_chars:
        return text[: cfg.max_input_chars] + "……（后面太长，省略了）"
    return text


# ------------------------------------------------------------------ 时间感
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


# ------------------------------------------------------------------ 性别：对方主动说了才算，她先质疑一句，对方再确认才接受
_F_WORDS = "女生|女孩子|女孩纸|女孩|女的|妹子|妹纸|女人|姑娘|小姐姐|女性|少女"
_M_WORDS = "男生|男孩子|男孩|男的|汉子|男人|大老爷们|老爷们|爷们|男性|小哥哥|少年"
_CLAIM_RE = re.compile(
    rf"(?:我|本人|人家|俺)(?:其实|本来|可|也|就|确实|真的)?(?:就)?是(?:个|一个|一名|一位)?\s*(?P<w>{_F_WORDS}|{_M_WORDS})(?![的们朋])"
    rf"|(?:本人|性别)[:：]?\s*(?P<c>[男女])(?![朋友孩人])"
)
_YES_RE = re.compile(r"^(?:真的|真滴|是的|是啊|是呀|是滴|对|对啊|对呀|对的|嗯|嗯嗯|当然|确定|千真万确|骗你干嘛|骗你干什么|没骗你|不骗你|如假包换)|真的是|没骗|不信拉倒")
_NO_RE = re.compile(r"骗你的|开玩笑|开个玩笑|假的|逗你的|逗你玩|才不是|不是啦")


def gender_claim(text: str) -> str | None:
    """对方说“我是女生 / 本人男”之类的，返回 female / male；没说返回 None"""
    for m in _CLAIM_RE.finditer(text):
        w = m.group("w") or m.group("c")
        start = m.start()
        if "不" in text[max(0, start - 1): m.end() - len(w)]:     # “我不是女生”
            continue
        if w.startswith("女") or w in _F_WORDS.split("|"):
            return "female"
        return "male"
    return None


def gender_step(qq: int, text: str) -> str:
    """处理对方关于自己性别的说法，返回给模型的提示（没有就返回空）"""
    prof = ltm.get_user(qq)
    claim = gender_claim(text)
    pending = prof.get("gender_pending")
    if pending and time.time() - pending.get("ts", 0) > cfg.gender_confirm_window:
        prof.pop("gender_pending", None)
        pending = None
    word = {"female": "女生", "male": "男生"}
    if pending:
        value = pending["value"]
        if _NO_RE.search(text) or (claim and claim != value):
            prof.pop("gender_pending", None)
            ltm.save_user(prof)
            if claim:                     # 改口了：按新的说法重新质疑
                return gender_step(qq, text)
            return "【性别】对方承认刚才说自己是" + word[value] + "是开玩笑的。你就当没听过。"
        if claim == value or _YES_RE.search(text.strip()):
            ltm.set_gender(qq, value)
            logger.info(f"性别已确认：{qq} → {word[value]}")
            return (f"【性别】对方又确认了一次，说自己确实是{word[value]}。你接受了（可以嘴上说一句“好吧，姑且信你”之类），"
                    "以后就这么认为，不要再质疑。")
        return ""                         # 聊别的去了：先不管，等窗口过期
    if not claim or claim == prof.get("gender"):
        return ""
    prof["gender_pending"] = {"value": claim, "ts": time.time()}
    ltm.save_user(prof)
    extra = ""
    if prof.get("gender") and prof["gender"] != claim:
        extra = f"而且对方以前明明说自己是{word[prof['gender']]}。"
    elif prof.get("gender_guess") and prof["gender_guess"] != claim:
        extra = f"而且你一直觉得对方更像{word[prof['gender_guess']]}。"
    return (f"【性别】对方说自己是{word[claim]}。{extra}你不会马上相信，用你的方式质疑一句，让对方再确认一次"
            "（比如“真的假的？”“嗯……我怎么有点不信呢”）。别追问细节，也别说要验证。")


# ------------------------------------------------------------------ 送东西：面包、钱
# 嘴上说“[给面包]”“给你钱”不会直接当真：面包每人每天只收一次；钱不加好感，她按帮的忙、接的委托收合理的报酬
_BREAD = r"(?:面包|吐司|可颂|牛角包|法棍|贝果|菠萝包|甜甜圈|羊角包|🍞|🥐|🥖|🥯)"
_GIVE = r"(?:给|送|递|塞|投喂|喂|分|赏|孝敬|上供|献上|奉上|请(?=你|您))(?!我)"
_TO_HER = r"(?:你|您|伊蕾娜(?:小姐)?|魔女小姐)?"
_BREAD_RE = re.compile(
    rf"[\[【［]\s*(?:给|送)?{_BREAD}\s*[\]】］]"
    rf"|{_GIVE}\s*{_TO_HER}[^，,。？?！!\n\[\]【】［］]{{0,6}}?{_BREAD}"
    rf"|{_BREAD}\s*(?:给你|送你|请你|拿去|拿着|接着)"
    rf"|^[🍞🥐🥖🥯\s]+$"
)
_NUM = r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百千万亿几半]+)"
_WORLD = r"(?:铜币|银币|金币)"
_FOREIGN = r"(?:元|块钱|块(?![面蛋饼糖石肉])|毛钱|人民币|rmb|美元|美金|日元|円|欧元|英镑|比特币|[Qq]币|点券|软妹币|大洋)"
_PAY = r"(?:给|送|赏|付|转|打|塞|递|发|奉上|支付)"
_WORLD_AMT = rf"{_NUM}\s*(?:枚|个|袋|箱)?\s*{_WORLD}"
_FOREIGN_AMT = rf"{_NUM}\s*(?:万|千|百)?\s*{_FOREIGN}"
_REWARD = r"(?:报酬|酬劳|谢礼|工钱|委托费|小费|定金|订金|酬金)"
_MONEY_WORLD_RE = re.compile(rf"{_PAY}[^，,。？?！!\n]{{0,8}}?{_WORLD_AMT}|{_WORLD_AMT}\s*(?:给你|送你|拿去|拿着|赏你)"
    rf"|{_WORLD_AMT}\s*(?:是|当|作为|做)?\s*(?:你的)?{_REWARD}|{_REWARD}\s*(?:是|有|为|给你)?\s*{_WORLD_AMT}|[\[【［][^\]】］]{{0,6}}{_WORLD}[^\]】］]{{0,6}}[\]】］]", re.I)
_MONEY_FOREIGN_RE = re.compile(
    rf"{_PAY}[^，,。？?！!\n]{{0,8}}?{_FOREIGN_AMT}|{_FOREIGN_AMT}\s*(?:给你|送你|拿去|拿着|赏你)"
    rf"|{_FOREIGN_AMT}\s*(?:是|当|作为|做)?\s*(?:你的)?{_REWARD}|{_REWARD}\s*(?:是|有|为|给你)?\s*{_FOREIGN_AMT}"
    rf"|(?:QQ|微信|支付宝)?红包|转账|转你|打钱|[\[【［][^\]】］]{{0,6}}(?:{_FOREIGN}|红包)[^\]】］]{{0,6}}[\]】］]",
    re.I,
)
_MONEY_VAGUE_RE = re.compile(
    r"[\[【［]\s*(?:给|送|打|赏)?\s*钱\s*[\]】］]|给你钱|给你点钱|钱给你|赏你|打赏|给你(?:点)?(?:零花钱|小费|报酬|酬劳|工钱|委托费|路费|谢礼)"
    r"|付(?:你|给你)?(?:报酬|酬劳|工钱|委托费)"
)


def _not_negated(text: str, m: re.Match) -> bool:
    return not (m.start() > 0 and text[m.start() - 1] in "不没别")


def detect_gifts(text: str) -> list[tuple[str, str]]:
    """看对方是不是在说送她东西：返回 [(种类, 原话片段)]，钱和面包各最多一项。
    种类：world（铜币银币金币）/ foreign（别的钱）/ vague（没说给多少）/ bread"""
    out = []
    for group in ((("world", _MONEY_WORLD_RE), ("foreign", _MONEY_FOREIGN_RE), ("vague", _MONEY_VAGUE_RE)), (("bread", _BREAD_RE),)):
        hit = next(((kind, m.group(0).strip()) for kind, rx in group for m in rx.finditer(text) if _not_negated(text, m)), None)
        if hit:
            out.append(hit)
    return out


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


# ------------------------------------------------------------------ 出错通知
_last_alert: dict[str, float] = {}


def classify_error(e: Exception) -> str | None:
    code = getattr(e, "status_code", None)
    msg = str(e)
    if code == 402 or "Insufficient Balance" in msg or "余额" in msg:
        return "balance"
    if code == 401 or "Authentication" in msg or "invalid api key" in msg.lower():
        return "auth"
    return None


async def alert_admins(bot: Bot, kind: str, detail: str = "") -> None:
    """私信所有管理员；同一类问题 admin_alert_interval 秒内只提醒一次"""
    now = time.monotonic()
    if now - _last_alert.get(kind, -1e9) < cfg.admin_alert_interval:
        return
    _last_alert[kind] = now
    text = {
        "balance": "【QQ 机器人提醒】DeepSeek 余额不足，伊蕾娜暂时无法回复。请到 https://platform.deepseek.com/ 充值，充值后不用重启。",
        "auth": "【QQ 机器人提醒】DeepSeek API Key 无效或已失效，伊蕾娜暂时无法回复。请检查 .env 里的 DEEPSEEK_API_KEY，改完重开 start.bat。",
        "nokey": "【QQ 机器人提醒】还没有配置 DEEPSEEK_API_KEY，伊蕾娜无法回复。请在 .env 里填好后重开 start.bat。",
    }.get(kind, f"【QQ 机器人提醒】{detail}")
    for su in bot.config.superusers:
        try:
            await bot.send_private_msg(user_id=int(su), message=text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"私信管理员 {su} 失败（需要和机器人是好友）：{e}")


# ------------------------------------------------------------------ 风控
_last_trigger: dict[int, float] = {}
_global_window: deque = deque()


_hour_window: deque = deque()
_group_hour: dict[int, deque] = defaultdict(deque)
_farewell_at: dict[str, float] = {}      # 限额范围 -> 上次说“我要上路了”的时间


def quota_left(group_id: int | None) -> int:
    """这一小时里还能回几条（全局和本群取小的）"""
    now = time.monotonic()
    left = cfg.global_rate_per_hour - sum(1 for t in _hour_window if now - t <= 3600)
    if group_id:
        left = min(left, cfg.group_rate_per_hour - sum(1 for t in _group_hour[group_id] if now - t <= 3600))
    return left


def rate_limited(user_id: int, group_id: int | None = None) -> str | None:
    now = time.monotonic()
    if now - _last_trigger.get(user_id, -1e9) < cfg.user_cooldown:
        return "cooldown"
    while _global_window and now - _global_window[0] > 60:
        _global_window.popleft()
    if len(_global_window) >= cfg.global_rate_per_minute:
        return "global"
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.popleft()
    if len(_hour_window) >= cfg.global_rate_per_hour:
        return "hourly"
    gq = _group_hour[group_id] if group_id else None
    if gq is not None:
        while gq and now - gq[0] > 3600:
            gq.popleft()
        if len(gq) >= cfg.group_rate_per_hour:
            return "group_hourly"
    _last_trigger[user_id] = now
    _global_window.append(now)
    _hour_window.append(now)
    if gq is not None:
        gq.append(now)
    return None


def typing_delay(reply: str) -> float:
    """第一条：先看消息、想一想，再按字数打字"""
    d = random.uniform(cfg.reply_delay_min, cfg.reply_delay_max) + len(reply) * cfg.reply_delay_per_char * random.uniform(0.8, 1.3)
    return min(d, cfg.reply_delay_cap)


def bubble_gap(bubble: str) -> float:
    """后面几条：停顿一下，再按这条的字数打字（打字速度每次略有快慢）"""
    d = random.uniform(cfg.bubble_gap_min, cfg.bubble_gap_max) + len(bubble) * cfg.reply_delay_per_char * random.uniform(0.8, 1.3)
    return min(d, cfg.bubble_gap_cap)


# 她只有一双手：同一时间只给一个人打字、发消息，其他人排队
_hands = asyncio.Lock()
_last_sent: dict = {"target": None, "at": -1e9}


def _target_of(event: MessageEvent) -> str:
    return f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}"


async def switch_pause(target: str) -> None:
    """刚回完别人（别的群 / 别的私聊）又要回这边：像切换聊天窗口一样停一下"""
    if _last_sent["target"] not in (None, target) and time.monotonic() - _last_sent["at"] < 30:
        await asyncio.sleep(random.uniform(cfg.switch_gap_min, cfg.switch_gap_max))


def mark_sent(target: str) -> None:
    _last_sent["target"], _last_sent["at"] = target, time.monotonic()


# ------------------------------------------------------------------ 触发规则
def _allowed(event: MessageEvent) -> bool:
    if isinstance(event, GroupMessageEvent):
        if not cfg.enable_group:
            return False
        return not cfg.group_whitelist or event.group_id in cfg.group_whitelist
    if isinstance(event, PrivateMessageEvent):
        if not cfg.enable_private:
            return False
        if cfg.private_friends_only and event.sub_type != "friend":
            return False
        return not cfg.private_whitelist or event.user_id in cfg.private_whitelist
    return False


allowed = Rule(_allowed)


async def _is_group_not_to_me(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent) and not event.is_tome()


# ------------------------------------------------------------------ 命令
reset_cmd = on_command("重置", aliases={"清空记忆", "reset"}, rule=to_me() & allowed, priority=5, block=True)


@reset_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    # 群聊里只有管理员能重置；私聊每个人都能清自己的
    if isinstance(event, GroupMessageEvent) and not await SUPERUSER(bot, event):
        await reset_cmd.finish("（只有管理员才能清空群里的记忆哦）")
    clear_history(session_key(event))
    if isinstance(event, GroupMessageEvent):
        _passive.pop(event.group_id, None)
    await reset_cmd.finish("（记忆已清空）")


reload_cmd = on_command("重载人设", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@reload_cmd.handle()
async def _():
    p = load_persona()
    await reload_cmd.finish(f"（人设已重载，{len(p)} 字）")


# ------------------------------------------------------------------ 长期记忆指令（仅管理员）
def _parse_target(event: MessageEvent, arg: Message) -> tuple[str, int] | None:
    """解析指令参数：@某人 / QQ号 → ("user", qq)；“本群” → ("group", 群号)；空 → None"""
    for seg in arg:
        if seg.type == "at" and str(seg.data.get("qq", "")).isdigit():
            return "user", int(seg.data["qq"])
    t = arg.extract_plain_text().strip()
    if t in ("本群", "这个群", "群") and isinstance(event, GroupMessageEvent):
        return "group", event.group_id
    if t.isdigit():
        return "user", int(t)
    if t in ("我", "自己"):
        return "user", event.user_id
    return None


# ------------------------------------------------------------------ 识图纠正（仅管理员）
see_cmd = on_command("认图", rule=to_me(), permission=SUPERUSER, priority=5, block=True)
SEE_USAGE = ("用法：引用一张图（或和图一起发）+ /认图 → 看她认出了什么；"
             "/认图 是 → 以后这张图就当成她自己；/认图 不是 → 以后不当成她；/认图 取消 → 恢复自动识别")


@see_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    images = [seg.data for seg in event.get_message() if seg.type == "image"]
    reply = getattr(event, "reply", None)
    if reply is not None:
        images += [seg.data for seg in reply.message if seg.type == "image"]
    if not images:
        await see_cmd.finish(SEE_USAGE)
    data = images[0]
    word = arg.extract_plain_text().strip()
    if word in ("是", "是她", "是伊蕾娜", "伊蕾娜"):
        ok = vision.set_override(data, True)
        await see_cmd.finish("（记下了：这张图画的是伊蕾娜。下次看到会按“她自己的画像”来描述）" if ok else "（这张图没法记，缺少图片编号）")
    if word in ("不是", "否", "不是她"):
        ok = vision.set_override(data, False)
        await see_cmd.finish("（记下了：这张图画的不是伊蕾娜）" if ok else "（这张图没法记，缺少图片编号）")
    if word in ("取消", "清除", "自动"):
        vision.set_override(data, None)
        await see_cmd.finish("（已恢复自动识别，下次看到会重新看一遍）")
    if word:
        await see_cmd.finish(SEE_USAGE)
    if vision.tagging:
        await vision.shows_self(data)      # 还没认过的图，先本机认一次（不花钱）
    e = vision.info(data)
    if not e:
        await see_cmd.finish("（这张图她还没看过，也没法下载了）")
    who = "；".join(e.get("c", [])) or "没认出角色"
    judged = "管理员指定" + ("是" if e["o"] else "不是") + "伊蕾娜" if "o" in e else ("算伊蕾娜" if vision.is_self(e) else "不算伊蕾娜")
    lines = [
        f"本机识别：{who}（伊蕾娜置信度 {e.get('s', 0):.2f}，门槛 {cfg.vision_tagger_threshold}，再确认线 {cfg.vision_tagger_maybe}）" if e.get("t") else "本机识别：还没认过（模型没准备好时看的图）",
        f"结论：{judged}",
        f"她看到的：{e['d']}" if e.get("d") else "她看到的：还没描述过",
    ]
    await see_cmd.finish("（" + "\n".join(lines) + "）")


# ------------------------------------------------------------------ 表情包管理（仅管理员）
sticker_cmd = on_command("表情", aliases={"表情包"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)
STICKER_USAGE = ("用法：/表情 → 概况；/表情 列表；/表情 3 → 发出 3 号看看；/表情 3 嫌弃 无语 → 改标签；"
                 "/表情 3 禁用 / 启用；/表情 刷新 → 重新拉收藏；/表情 测试 嫌弃 → 按情绪挑一张\n"
                 "情绪词表：" + "、".join(EMOTIONS))


@sticker_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    words = arg.extract_plain_text().split()
    if not words:
        head = f"（{stickers.summary()}｜发表情{'开' if cfg.sticker_enabled else '关'}，概率 {cfg.sticker_prob:g}）"
        await sticker_cmd.finish(head + "\n" + STICKER_USAGE)
    op = words[0]
    if op in ("刷新", "更新", "同步"):
        try:
            added, removed, _n = await stickers.refresh(bot, cfg.sticker_fetch_count)
        except Exception as e:  # noqa: BLE001
            await sticker_cmd.finish(f"（拉取收藏表情失败：{e}）")
        pending = len(stickers.unlabeled())
        if pending:
            _spawn_labeling()
        note = f"，{pending} 张正在后台打标签（{'高峰时段先不打，低峰再说' if in_peak() else '大约每张几秒'}），过一会儿再看 /表情 列表" if pending else ""
        await sticker_cmd.finish(f"（新增 {added} 张，取消收藏 {removed} 张{note}）\n（{stickers.summary()}）")
    if op in ("列表", "全部", "list"):
        lines = stickers.list_lines()
        if not lines:
            await sticker_cmd.finish("（表情库是空的：先在小号收藏里存表情，再发 /表情 刷新）")
        for i in range(0, len(lines), 15):               # 太长的消息分几条发
            await bot.send(event, "\n".join(lines[i:i + 15]))
            await asyncio.sleep(1.0)
        await sticker_cmd.finish()
    if op == "测试":
        if len(words) < 2:
            await sticker_cmd.finish("用法：/表情 测试 嫌弃")
        it = stickers.pick(words[1])
        if not it:
            await sticker_cmd.finish(f"（库里没有“{words[1]}”或相近情绪的表情）")
        await bot.send(event, f"（{it['no']} 号：{'、'.join(it['tags'])}｜{it.get('desc') or ''}）")
        await sticker_cmd.finish(stickers.segment(it, cfg.sticker_sub_type))
    if op.isdigit():
        no = int(op)
        it = stickers.by_no(no)
        if not it:
            await sticker_cmd.finish(f"（没有 {no} 号表情）")
        rest = words[1:]
        if not rest:
            await bot.send(event, f"（{no} 号：{'、'.join(it.get('tags') or []) or '无情绪'}｜{it.get('desc') or ''}）")
            await sticker_cmd.finish(stickers.segment(it, cfg.sticker_sub_type))
        if rest[0] in ("禁用", "停用", "不用"):
            stickers.set_disabled(no, True)
            await sticker_cmd.finish(f"（{no} 号已禁用）")
        if rest[0] in ("启用", "恢复"):
            stickers.set_disabled(no, False)
            await sticker_cmd.finish(f"（{no} 号已启用）")
        tags = stickers.set_tags(no, rest)
        if not tags:
            await sticker_cmd.finish("（这些词对不上情绪词表：" + "、".join(EMOTIONS) + "）")
        await sticker_cmd.finish(f"（{no} 号的标签改成了：{'、'.join(tags)}；以后刷新也不会覆盖）")
    await sticker_cmd.finish(STICKER_USAGE)


MEMORY_USAGE = "用法：/记忆 @某人（或 QQ号、我、本群）；/忘记 @某人（或 QQ号、我、本群）"

mem_show = on_command("记忆", aliases={"查看记忆"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@mem_show.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    target = _parse_target(event, arg)
    if not target:
        await mem_show.finish(MEMORY_USAGE)
    kind, tid = target
    await mem_show.finish(ltm.describe_group(tid) if kind == "group" else ltm.describe_user(tid))


aff_cmd = on_command("好感", aliases={"好感度"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@aff_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    """/好感 @某人 查看；/好感 @某人 +10 / -10 / =50 调整（也可以用 QQ号 或 我）"""
    text = arg.extract_plain_text().strip()
    ats = [seg for seg in arg if seg.type == "at" and str(seg.data.get("qq", "")).isdigit()]
    qq, rest = None, text
    if ats:
        qq = int(ats[0].data["qq"])
    else:
        first, _, rest = text.partition(" ")
        if first.isdigit():
            qq = int(first)
        elif first in ("我", "自己"):
            qq = event.user_id
    if qq is None:
        await aff_cmd.finish("用法：/好感 @某人（或 QQ号、我）查看；后面加 +10、-10 或 =50 调整")
    m = re.match(r"\s*([+\-=])\s*(\d+(?:\.\d+)?)", rest)
    if m:
        op, num = m.group(1), float(m.group(2))
        if op == "=":
            ltm.adjust(qq, set_to=num)
        else:
            ltm.adjust(qq, delta=num if op == "+" else -num)
    await aff_cmd.finish(ltm.describe_affection(qq, cfg.close_friends))


gender_cmd = on_command("性别", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@gender_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    """/性别 @某人 查看；/性别 @某人 男 / 女 / 清除 手动设置"""
    text = arg.extract_plain_text().strip()
    ats = [seg for seg in arg if seg.type == "at" and str(seg.data.get("qq", "")).isdigit()]
    first, _, rest = text.partition(" ")
    qq = int(ats[0].data["qq"]) if ats else int(first) if first.isdigit() else event.user_id if first in ("我", "自己") else None
    if ats:
        rest = text
    if qq is None:
        await gender_cmd.finish("用法：/性别 @某人（或 QQ号、我）查看；后面加 男、女 或 清除 来设置")
    op = rest.strip()
    if op in ("男", "女"):
        ltm.set_gender(qq, "male" if op == "男" else "female")
    elif op in ("清除", "未知"):
        ltm.set_gender(qq, None)
    prof = ltm.get_user(qq)
    await gender_cmd.finish(f"（{prof.get('name') or qq}｜性别：{ltm.gender_text(prof)}｜好感 {ltm.effective_score(prof):g}｜{ltm.TIER_NAMES[familiarity_of(qq)]}）")


mem_forget = on_command("忘记", aliases={"删除记忆"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@mem_forget.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    target = _parse_target(event, arg)
    if not target:
        await mem_forget.finish(MEMORY_USAGE)
    kind, tid = target
    if kind == "group":
        ok = ltm.forget_group(tid)
        await mem_forget.finish("（本群的往事已清空）" if ok else "（本群本来就没有往事记录）")
    ok = ltm.forget_user(tid)
    await mem_forget.finish(f"（已删除关于 {tid} 的长期记忆）" if ok else f"（关于 {tid} 本来就没有长期记忆）")


# ------------------------------------------------------------------ 群消息计数（决定要不要用“回复”形式）
# 每个群收到的消息都 +1；机器人发送前比较一下：触发之后如果有人插话，就引用原消息回复，否则直接发
_group_seq: dict[int, int] = defaultdict(int)


async def _is_group(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


counter = on_message(rule=Rule(_is_group), priority=1, block=False)


@counter.handle()
async def _(event: GroupMessageEvent):
    _group_seq[event.group_id] += 1
    ltm.note_name(event.group_id, event.user_id, sender_name(event))


# ------------------------------------------------------------------ 旁听（没 @ 的群消息）
passive = on_message(rule=Rule(_is_group_not_to_me) & allowed, priority=99, block=False)


@passive.handle()
async def _(event: GroupMessageEvent):
    if cfg.passive_buffer <= 0:
        return
    text = message_to_text(event.get_message())
    if text:
        _passive[event.group_id].append((event.user_id, sender_name(event), clip_input(text)))


# ------------------------------------------------------------------ 没 @ 也回复：判断是不是在跟她说话
_last_bot_msg: dict[int, tuple[float, int, str]] = {}   # 群号 -> (时间, 她回复的对象 QQ, 她说的话)
_last_smart: dict[int, float] = {}
_engaged: dict[tuple[int, int], float] = {}   # (群号, QQ) -> 这个人最近一次在跟她说话的时间

JUDGE_PROMPT = """判断群聊里的一条新消息，是不是在跟“伊蕾娜”说话。伊蕾娜是群里的角色扮演机器人，一位旅行魔女。

伊蕾娜最近在群里说的话：{last}
最近的群聊：
{recent}
新消息：【{name}】{text}

如果新消息是在叫她、问她、对她说话、回应她刚才的话，或者在谈论她并明显希望她回应，回答“是”。
如果是群友之间在聊天，只是顺带提到这个名字（比如在聊动画、小说里的角色），不需要她回应，回答“否”。
如果是在跟别人说话、用“她”“这个机器人”之类第三人称谈论她（比如向别人介绍她），回答“否”。
只输出一个字：是 或 否。"""


def _looks_like_question(text: str) -> bool:
    return any(c in text for c in "?？") or text.endswith(("吗", "呢", "吧", "嘛"))


async def _judge(event: GroupMessageEvent, name: str, text: str) -> bool:
    last = _last_bot_msg.get(event.group_id)
    recent = "\n".join(f"【{n}】{t}" for _, n, t in list(_passive.get(event.group_id, []))[-4:]) or "（无）"
    prompt = JUDGE_PROMPT.format(last=last[2][:100] if last else "（最近没说话）", recent=recent, name=name, text=text[:200])
    try:
        r = await client.chat.completions.create(
            model=cfg.deepseek_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return (r.choices[0].message.content or "").strip().startswith("是")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"判断是否在跟她说话失败，按“否”处理：{e}")
        return False


async def _addressed(bot: Bot, event: MessageEvent) -> bool:
    """@ 她 / 回复她 / 以昵称开头 → 一定回；否则在群里看是否明显在跟她说话"""
    now = time.monotonic()
    if event.is_tome():
        if isinstance(event, GroupMessageEvent):
            _engaged[(event.group_id, event.user_id)] = now
        return True
    if not (cfg.smart_reply and isinstance(event, GroupMessageEvent)) or not _allowed(event):
        return False
    text = message_to_text(event.get_message())
    if not text or text.startswith(tuple(bot.config.command_start or {"/"})):
        return False
    # 群里没 @ 她的纯图片 / 纯表情：不当成在跟她说话（不回复，只进旁听，显示为 [图片]）。
    # 例外：本机角色识别认出图里画的是她，她可能看一眼、说一句
    if not re.sub(r"\[(图片|语音|视频|表情[^\]]*)\]|\s", "", text):
        return await _self_image_react(event)
    gid = event.group_id
    # 连续发言：刚叫过她的人，紧接着发的话直接算在跟她说
    if now - _engaged.get((gid, event.user_id), -1e9) <= cfg.followup_window:
        _engaged[(gid, event.user_id)] = now
        return True
    if len(text) < 2 or in_peak():       # 高峰时段她很忙，不主动接话（也省掉判断的 token）
        return False
    if now - _last_smart.get(gid, -1e9) < cfg.smart_min_interval:
        return False
    names = [n for n in list(cfg.smart_names) + list(bot.config.nickname or []) if n]
    mentioned = any(n in text for n in names)
    last = _last_bot_msg.get(gid)
    recent_talk = bool(last) and now - last[0] <= cfg.smart_window
    continuing = recent_talk and (event.user_id == last[1] or "你" in text or _looks_like_question(text))
    if not (mentioned or continuing):
        return False                     # 大部分群消息在这里就结束了，不花 token
    ok = await _judge(event, sender_name(event), text) if cfg.smart_judge else mentioned
    if ok:
        _last_smart[gid] = now
        _engaged[(gid, event.user_id)] = now
        logger.info(f"没 @ 也回复：群{gid} {sender_name(event)}：{text[:30]}")
    return ok


_last_self_react: dict[int, float] = {}
_self_react_seen: dict[str, float] = {}   # 图片 -> 上次为它搭话的时间（同一张表情包一天只搭一次）


async def _self_image_react(event: GroupMessageEvent) -> bool:
    """群里有人发了伊蕾娜的画像（没 @ 她）：本机识别，不花 token；按概率和间隔决定要不要搭话"""
    if not (cfg.vision_enabled and cfg.vision_self_react and vision.tagging) or in_peak():
        return False
    gid, now = event.group_id, time.monotonic()
    if now - _last_self_react.get(gid, -1e9) < cfg.vision_self_react_interval:
        return False
    if now - _last_smart.get(gid, -1e9) < cfg.smart_min_interval:
        return False
    images = [seg.data for seg in event.get_message() if seg.type == "image"][: cfg.vision_max_images]
    for data in images:
        key = vision.cache_key(data)
        if now - _self_react_seen.get(key, -1e9) < 86400:
            continue
        if await vision.shows_self(data):
            _self_react_seen[key] = now
            if random.random() >= cfg.vision_self_react_prob:
                logger.info(f"群{gid} {sender_name(event)} 发了伊蕾娜的图，这次没搭话")
                return False
            _last_self_react[gid] = _last_smart[gid] = now
            logger.info(f"群{gid} {sender_name(event)} 发了伊蕾娜的图，看一眼搭句话")
            return True
    return False


# ------------------------------------------------------------------ 主对话
chat = on_message(rule=Rule(_addressed) & allowed, priority=10, block=True)


_peak_last_real: dict[int, float] = {}
_peak_last_busy: dict[int, float] = {}
_inbox: dict[tuple[str, int], list[str]] = defaultdict(list)   # (会话, QQ) -> 还没回复的消息
_inbox_token: dict[tuple[str, int], int] = {}
_token_counter = 0


_inbox_first: dict[tuple[str, int], float] = {}   # (会话, QQ) -> 这一批第一条消息到的时间

# ---- 话说完了没有：决定等多久
_UNFINISHED_TAIL = ("，", ",", "、", "：", ":", "…", "...", "；", ";", "（", "(", "“", "—")
_UNFINISHED_WORDS = (
    "然后", "就是", "而且", "但是", "不过", "所以", "因为", "结果", "还有", "话说", "对了", "那个", "这个",
    "我跟你说", "跟你说", "听我说", "问你个事", "问你件事", "问你个问题", "问个问题", "问一下", "请问",
    "等下", "等等", "等一下", "我想", "我觉得", "其实", "比如", "关于", "和", "跟", "的话", "如果", "要是",
)
_CALL_ONLY_RE = re.compile(r"(?:在吗|在不在|在么|在嘛|你在吗|有人吗|喂+|诶+|哎+|欸+|嘿+|在？?)")


def merge_delay(text: str, names: list[str] | tuple = ()) -> float:
    """看这条消息像不像话还没说完：没说完多等一会儿，说完了少等一会儿"""
    t = text.strip()
    core = re.sub(r"[\s。.!！~～?？]+$", "", t)
    if not core:
        return cfg.merge_wait
    bare = re.sub(r"[\s,，。.!！~～?？]", "", t)
    if t == "（@了你一下，没说话）" or _CALL_ONLY_RE.fullmatch(bare) or any(bare == n or bare == f"@{n}" for n in names if n):
        return cfg.merge_wait_incomplete          # 只叫了她一声 / 问在不在：正事多半在下一条
    if t.endswith(_UNFINISHED_TAIL) or core in _UNFINISHED_WORDS or core.endswith(_UNFINISHED_WORDS):
        return cfg.merge_wait_incomplete
    if t[-1] in "?？!！。~～" or core.endswith(("吗", "呢", "吧", "嘛", "啊", "呀", "啦", "了")) or len(core) >= 12:
        return cfg.merge_wait_complete
    return cfg.merge_wait


async def wait_for_more(ik: tuple[str, int], text: str, names: list[str] | tuple = ()) -> int | None:
    """放进收件箱等一会儿（按话说完没说完决定等多久）；期间同一个人又发了新消息，返回 None（让最新那条来回复）"""
    global _token_counter
    now = time.monotonic()
    if not _inbox[ik]:
        _inbox_first[ik] = now
    _inbox[ik].append(text)
    _token_counter += 1
    token = _inbox_token[ik] = _token_counter
    left = cfg.merge_wait_max - (now - _inbox_first.get(ik, now))    # 从第一条算起最多等 merge_wait_max 秒
    await asyncio.sleep(max(0.0, min(merge_delay(text, names), left)))
    return token if _inbox_token.get(ik) == token else None


# ---- 水话：只是应一声、笑一下、发个表情
_FILLER_RE = re.compile(
    r"(?:哈|呵|嘿|嘻|hh|h|233+|w|嗯|恩|哦|噢|喔|额|呃|啊|好的?|好吧|好滴|好哒|行吧?|ok|okk|收到|知道了|晓得了|懂了|了解|明白了?"
    r"|确实|是的?|对的?|嗯呢|也是|笑死|绷|草|\[表情[^\]]*\]|\[语音\])+",
    re.I,
)
_SKIP_RE = re.compile(r"[\[【（(［]\s*不回\s*[\]】）)］]")


def is_filler(text: str) -> bool:
    bare = re.sub(r"[\s,，。.!！~～…]", "", text)
    return bool(bare) and bool(_FILLER_RE.fullmatch(bare))


def she_asked(history: list[dict]) -> bool:
    """她上一句是不是在问对方（问了的话，对方“嗯”一声也算回答，要接着聊）"""
    last = next((h["content"] for h in reversed(history) if h["role"] == "assistant"), "")
    last = re.sub(r"\[发了表情[^\]]*\]", "", last).strip()
    return bool(last) and (last[-1] in "?？" or last.endswith(("吗", "呢", "吧", "嘛")))


@chat.handle()
async def _(bot: Bot, event: MessageEvent):
    await converse(bot, event)


async def converse(bot: Bot, event: MessageEvent, catchup_age: float | None = None) -> None:
    """回复一条（或连着的几条）消息。catchup_age 不为空时，表示这是上线后补回的未读消息，值是最早那条过去了几秒"""
    global _token_counter
    text = await rich_text(event, look=cfg.vision_in_peak or not in_peak())
    if not text:
        text = "（@了你一下，没说话）"

    if not cfg.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，已跳过回复")
        await alert_admins(bot, "nokey")
        return

    key = session_key(event)
    is_group = isinstance(event, GroupMessageEvent)

    # 连续发送：先把这条放进收件箱，等几秒；期间同一个人又发了，就交给最新那条一起回复
    ik = (key, event.user_id)
    if catchup_age is None:
        names = list(cfg.smart_names) + list(getattr(bot.config, "nickname", None) or [])
        token = await wait_for_more(ik, text, names)
        if token is None:
            return
    else:                                 # 补回未读：几条已经合在一起了，不用再等
        _inbox[ik].append(text)
        _token_counter += 1
        token = _inbox_token[ik] = _token_counter

    # 水话（“哈哈”“嗯”“好的”、一个表情）：按关系远近，有一定概率直接不接话（不调用模型）
    pending = _inbox.get(ik, [])
    if cfg.skip_filler and pending and all(is_filler(t) for t in pending):
        async with _locks[key]:
            history = get_history(key)
            if not she_asked(history) and random.random() < cfg.skip_filler_prob.get(familiarity_of(event.user_id), 0.5):
                texts = _inbox.pop(ik, [])
                name = sender_name(event)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": f"【{name}】{joined}" if is_group else joined,
                                "uid": event.user_id, "name": name, "ts": time.time()})
                save_history(key)
                logger.info(f"水话，不接了 user={event.user_id}：{joined[:20]}")
                return

    # 高峰时段：很忙。每人每 10 分钟最多一次正经回复；其余时候随机回一句“在忙”（不调用模型）或干脆不回
    busy = in_peak()
    if busy:
        now = time.monotonic()
        recent_real = now - _peak_last_real.get(event.user_id, -1e9) < cfg.peak_user_interval
        recent_busy = now - _peak_last_busy.get(event.user_id, -1e9) < cfg.peak_user_interval
        if recent_real or random.random() < cfg.peak_busy_prob:
            texts = _inbox.pop(ik, [])
            if recent_busy or not texts:
                logger.info(f"高峰时段，忙着没回 user={event.user_id}")
                return
            _peak_last_busy[event.user_id] = now
            line = peak.busy_line()
            async with _locks[key]:
                history = get_history(key)
                name = sender_name(event)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": f"【{name}】{joined}" if is_group else joined,
                                "uid": event.user_id, "name": name, "ts": time.time()})
                history.append({"role": "assistant", "content": line, "ts": time.time()})
                save_history(key)
            await asyncio.sleep(random.uniform(cfg.peak_extra_delay_min, cfg.peak_extra_delay_max))
            async with _hands:
                await switch_pause(_target_of(event))
                await bot.send(event, line)
                mark_sent(_target_of(event))
            return
        _peak_last_real[event.user_id] = now

    # 被讨厌的人：有一定概率直接不理（不花 token）
    if familiarity_of(event.user_id) == "disliked" and random.random() < cfg.dislike_ignore_prob:
        _inbox.pop(ik, None)
        logger.info(f"讨厌的人，不想理 user={event.user_id}")
        return

    limited = rate_limited(event.user_id, event.group_id if is_group else None)
    waited = 0.0
    while limited in ("cooldown", "global"):
        # 个人冷却 / 这一分钟回太多了：排队等一等再回，而不是直接丢掉
        if limited == "cooldown":
            wait = cfg.user_cooldown - (time.monotonic() - _last_trigger.get(event.user_id, -1e9))
        else:
            wait = 60 - (time.monotonic() - _global_window[0]) + random.uniform(1, 5) if _global_window else 1
        wait = max(0.1, wait)
        if waited + wait > max(cfg.queue_max_wait, cfg.user_cooldown + 1):
            break
        await asyncio.sleep(wait)
        waited += wait
        if _inbox_token.get(ik) != token:
            return
        limited = rate_limited(event.user_id, event.group_id if is_group else None)
    if limited:
        logger.info(f"限流跳过 user={event.user_id} reason={limited}")
        _inbox.pop(ik, None)
        return

    async with _locks[key]:
        texts = _inbox.pop(ik, [])
        if not texts:                     # 已经被前一条合并回复过了
            return
        text = clip_input("\n".join(texts))
        seq_at_trigger = _group_seq[event.group_id] if is_group else 0
        history = get_history(key)
        new_entries: list[dict] = []

        name = sender_name(event)
        if is_group and _passive.get(event.group_id):
            buf = list(_passive.pop(event.group_id))
            new_entries.append({
                "role": "user",
                "content": "（群聊旁听记录）\n" + "\n".join(f"【{n}】{t}" for _, n, t in buf),
                "speakers": {n: uid for uid, n, _ in buf},
                "ts": time.time(),
            })

        user_content = f"【{name}】{text}" if is_group else text
        new_entries.append({"role": "user", "content": user_content, "uid": event.user_id, "name": name, "ts": time.time()})

        # 检索回忆：用本条消息；找不到时带上此前最近一条用户消息再试一次（应对“那后来呢？”）
        prev_user = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
        prev_bot = next((h["content"] for h in reversed(history) if h["role"] == "assistant"), "")
        # 高峰时段不检索小说（省 token）
        memo = recall(text, f"{prev_user[-60:]} {prev_bot[-120:]}") if cfg.knowledge_enabled and not busy else ""
        long_memo = ltm.context_for(event.user_id, name, event.group_id if is_group else None)
        mode = "busy" if busy else reply_mode(text)
        fam = familiarity_of(event.user_id)
        length_hint = short_hint() if mode == "short" else LENGTH_HINT[mode]
        time_memo = time_hint(history, ltm.last_seen(event.user_id), fam, ltm.get_user(event.user_id).get("last_letter"))
        gender_memo = gender_step(event.user_id, text)
        gifts = detect_gifts(text)
        bread_first = any(k == "bread" for k, _ in gifts) and not ltm.bread_given_today(event.user_id)
        gift_memo = "\n".join(gift_hint(k, snip[:20], bread_first, fam) for k, snip in gifts)
        diary_memo = qzone_diary.diary_context(text)     # 有人提到她的说说：告诉她最近写了什么
        late_memo = ""
        if catchup_age is not None:
            late_memo = (f"【刚看到】对方这几条消息是你不在的时候发的，最早一条已经是 {human_gap(catchup_age)}前了，你现在才看到。"
                         "回的时候可以随口带一句刚看到（比如“刚才在赶路，没看消息”），一句带过就行，"
                         "不要提掉线、离线、手机、网络这类词，也不用道歉。")
        # 表情：先抽签，抽中了才告诉她这轮可以甩一张（有人发她的画像时更容易抽中）
        target = _target_of(event)
        self_image = "[图片：伊蕾娜本人的画像" in text
        use_sticker, sticker_allowed, sticker_emotions = sticker_roll(
            target, fam, is_group, event.group_id if is_group else None, self_image)
        sticker_memo = ""
        if use_sticker:
            only_ok = length_hint == SHORT_VARIANTS[0][1] and random.random() < cfg.sticker_only_prob
            sticker_memo = sticker_hint(sticker_emotions, only_ok)
        skip_memo = ""
        if (cfg.skip_by_model and catchup_age is None and len(text) <= 12 and not _looks_like_question(text)
                and not she_asked(history) and not gifts):
            skip_memo = ("【可以不回】对方这句像是随口一说（附和、应一声、客套、道别之类）。"
                         "如果你觉得没必要接话，就只输出「[不回]」这三个字符；想回就正常回。")
        extra = "\n\n".join(x for x in (long_memo, memo, time_memo, gender_memo, diary_memo, late_memo, gift_memo, FAMILIARITY_HINT[fam] + length_hint, sticker_memo, skip_memo) if x)
        recall_msg = [{"role": "system", "content": extra}]

        messages = api_messages(
            [{"role": "system", "content": system_prompt()}]
            + history
            + new_entries[:-1]
            + recall_msg          # 长期记忆和回忆只放在本轮，不写进聊天记忆
            + new_entries[-1:]
        )

        try:
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model,
                messages=messages,
                temperature=cfg.llm_temperature,
                max_tokens=max_tokens_for(mode),
                extra_body={"thinking": {"type": "enabled" if cfg.llm_thinking else "disabled"}},
            )
            choice = resp.choices[0]
            reply, emotion = split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))
            bad = ooc_words(reply, text)
            if bad:
                # 出戏了：让她以伊蕾娜的身份重说一次
                logger.info(f"回复出戏（{bad}），重新生成：{reply[:40]}")
                resp = await client.chat.completions.create(
                    model=cfg.deepseek_model,
                    messages=messages + [
                        {"role": "assistant", "content": reply},
                        {"role": "system", "content": f"刚才的回复出戏了（出现了：{'、'.join(bad)}）。伊蕾娜不知道这些东西。请完全以伊蕾娜的身份重新回复这条消息，不要道歉，不要解释。"},
                    ],
                    temperature=cfg.llm_temperature,
                    max_tokens=max_tokens_for(mode),
                    extra_body={"thinking": {"type": "enabled" if cfg.llm_thinking else "disabled"}},
                )
                choice = resp.choices[0]
                reply, emotion = split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))
                if ooc_words(reply, text):
                    reply = drop_ooc_sentences(reply, text)   # 还出戏就删掉出戏的句子；删光了就不发
        except Exception as e:  # noqa: BLE001
            # 出错时不在聊天里发任何东西；余额不足 / Key 失效私信管理员
            kind = classify_error(e)
            logger.error(f"DeepSeek 调用失败（{kind or '其他错误'}），本条不回复：{e}")
            if kind:
                await alert_admins(bot, kind)
            return

        # 她觉得没必要接话：不发，只把对方的话记下来
        if _SKIP_RE.search(reply):
            rest = _SKIP_RE.sub("", reply).strip()
            if not rest:
                history.extend(new_entries)
                save_history(key)
                ltm.bump_talk(event.user_id, name)
                logger.info(f"她觉得没必要接话 user={event.user_id}：{text[:20]}")
                return
            reply, emotion = rest, None       # 写了“[不回]”又写了话：以话为准

        # 表情：只有这轮抽中了才发；没抽中她却写了标记，就当没写
        sticker = None
        if emotion:
            if use_sticker:
                sticker = stickers.pick(emotion, set(_recent_stickers[target]), sticker_allowed)
                logger.info(f"表情：想发“{emotion}”" + (f" → {sticker['no']} 号（{'、'.join(sticker['tags'])}）" if sticker else "，库里没有合适的"))
            else:
                logger.info(f"表情：这轮没抽中，忽略她写的“{emotion}”")
        if not reply and not sticker:
            return
        sticker_note = ""
        if sticker:
            sticker_note = f"[发了表情：{sticker['tags'][0]}" + (f"（{sticker['desc']}）" if sticker.get("desc") else "") + "]"
        record = "\n".join(x for x in (reply, sticker_note) if x)

        history.extend(new_entries)
        history.append({"role": "assistant", "content": record, "ts": time.time()})
        save_history(key)
        tier_now = familiarity_of(event.user_id)     # 按说这句话时的关系算
        ltm.bump_talk(event.user_id, name)
        if bread_first:                               # 今天第一次送面包：收下（讨厌的人送的不加分）
            ltm.take_bread(event.user_id, 0 if tier_now == "disliked" else cfg.gift_bread_affection)
        hit = next((w for w in cfg.taboo_words if w in text), None)
        if hit:
            k = cfg.taboo_tier_multiplier.get(tier_now, 1.0)
            if k > 0:
                ltm.taboo_penalty(event.user_id, cfg.taboo_penalty * k, cfg.taboo_daily_max * k,
                                  f"说她{hit}（{ltm.TIER_NAMES.get(tier_now, tier_now)}）")
        # 这一轮交给长期记忆：攒够一批就在后台整理档案、评估好感
        # 长期记忆不需要知道她甩了哪张画像；只发了表情的，给一个简短的说法
        ltm.add_pending(key, new_entries + [{"role": "assistant", "content": reply or f"（甩了一张{sticker['tags'][0]}的表情）"}])

    bubbles = split_bubbles(reply) if reply else []
    farewell = None
    # 同一时间只给一个人打字：别人的回复要等这边发完
    async with _hands:
        await switch_pause(target)
        delay = typing_delay(bubbles[0] if bubbles else "")
        if busy:
            delay += random.uniform(cfg.peak_extra_delay_min, cfg.peak_extra_delay_max)
        await asyncio.sleep(delay)

        for i, bubble in enumerate(bubbles):
            if i > 0:
                await asyncio.sleep(bubble_gap(bubble))            # 后面几条：像在接着打字
                _hour_window.append(time.monotonic())                 # 多发的每条都算进每小时限额
                if is_group:
                    _group_hour[event.group_id].append(time.monotonic())
            msg = bubble
            if i == 0 and is_group and _group_seq[event.group_id] > seq_at_trigger:
                # 中间有别人说话了（包括她排队的时候）：第一条引用原消息，免得大家不知道她在回谁
                msg = MessageSegment.reply(event.message_id) + bubble
            await bot.send(event, msg)
        if sticker:
            # 文字后面隔一两秒再甩表情；只发表情时，中间有人插话就引用原消息
            if bubbles:
                await asyncio.sleep(random.uniform(cfg.bubble_gap_min, cfg.bubble_gap_max))
                _hour_window.append(time.monotonic())
                if is_group:
                    _group_hour[event.group_id].append(time.monotonic())
            try:
                seg = stickers.segment(sticker, cfg.sticker_sub_type)
                if not bubbles and is_group and _group_seq[event.group_id] > seq_at_trigger:
                    seg = MessageSegment.reply(event.message_id) + seg
                await bot.send(event, seg)
                _last_sticker[target] = time.monotonic()
                _recent_stickers[target].append(sticker["file"])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"表情：发送失败（{sticker['no']} 号）：{e}")
        # 这一小时的限额用完了：补一句告别，让大家知道她接下来一段时间不会回（同一范围一小时只说一次）
        if cfg.farewell_on_limit and quota_left(event.group_id if is_group else None) <= 0:
            scope = f"group_{event.group_id}" if is_group and len(_hour_window) < cfg.global_rate_per_hour else "global"
            if time.monotonic() - _farewell_at.get(scope, -1e9) > 3600:
                _farewell_at[scope] = time.monotonic()
                farewell = peak.farewell_line()
                await asyncio.sleep(bubble_gap(farewell) + 1)
                await bot.send(event, farewell)
                logger.info(f"每小时限额用完，已告别：{scope}")
        mark_sent(target)
    if farewell:
        async with _locks[key]:
            get_history(key).append({"role": "assistant", "content": farewell, "ts": time.time()})
            save_history(key)
    if is_group:
        _last_bot_msg[event.group_id] = (time.monotonic(), event.user_id, record)


# ------------------------------------------------------------------ 主动写信
LETTER_PROMPT = """【这次不是聊天，是写信】你在旅途中，突然想给「{name}」写一封信，托人捎过去。{gap}
- 写你最近在旅途中的见闻：在哪个国家或小镇、遇到了什么小事。随手编一件就好，但不要搬出书里有名有姓的人物和事件。
- 顺带提一句和对方有关的事（参考上面的长期记忆和最近聊天；没有就不提）。
- 保持伊蕾娜的语气：自恋、嘴硬、有点毒舌。不肉麻，不直说“想你”，顶多别扭地表示一下在意。
- 60～150 字。第一行写「致 {name}：」，最后一行署名「——伊蕾娜」。只输出信的内容，不受平时聊天长度的限制。"""

_letters_sent: dict[str, int] = {}      # 日期 -> 今天已寄出几封
_letter_task: "asyncio.Task | None" = None


def _letter_window_now() -> bool:
    now = datetime.now(peak.BEIJING)
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in peak.parse_ranges(cfg.letter_hours)) and not in_peak()


def _letter_prob_per_check() -> float:
    """把“每天的概率”折算成每次检查的概率（只在可寄信的时段里检查）"""
    window = sum(b - a for a, b in peak.parse_ranges(cfg.letter_hours)) * 60 or 86400
    checks = max(1.0, window / max(60, cfg.letter_check_interval))
    return 1 - (1 - min(max(cfg.letter_daily_prob, 0.0), 0.999)) ** (1 / checks)


def letter_blocker(qq: int, friends: set[int], force: bool = False) -> str | None:
    """不能给这个人写信的原因；可以写就返回 None。force=True 时只检查硬性条件（好友、私聊开关）"""
    if not cfg.enable_private:
        return "私聊功能关着"
    if qq not in friends:
        return "不是机器人的 QQ 好友"
    if cfg.private_whitelist and qq not in cfg.private_whitelist:
        return "不在私聊白名单里"
    if force:
        return None
    if familiarity_of(qq) != "close":
        return "还没到很熟"
    prof = ltm.get_user(qq)
    seen, letter, now = ltm.last_seen(qq), prof.get("last_letter"), time.time()
    if not seen:
        return "还没聊过天"
    if now - seen < cfg.letter_min_silence_hours * 3600:
        return "最近刚聊过"
    if letter and now - letter < cfg.letter_min_days * 86400:
        return "刚写过信"
    if letter and letter > seen:
        return "上一封信还没回"
    return None


async def write_letter(bot: Bot, qq: int) -> str | None:
    """写一封信并私聊发出去；成功返回信的内容"""
    key = f"private_{qq}"
    prof = ltm.get_user(qq)
    name = prof.get("name") or str(qq)
    seen = ltm.last_seen(qq)
    gap = f"你们已经 {human_gap(time.time() - seen)}没说话了。" if seen else ""
    async with _locks[key]:
        history = get_history(key)
        long_memo = ltm.context_for(qq, name, None)
        messages = api_messages(
            [{"role": "system", "content": system_prompt()}]
            + history[-6:]
            + [{"role": "system", "content": "\n\n".join(x for x in (long_memo, LETTER_PROMPT.format(name=name, gap=gap)) if x)}]
        )
        try:
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model,
                messages=messages,
                temperature=cfg.llm_temperature,
                max_tokens=cfg.letter_max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
            choice = resp.choices[0]
            letter = clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            logger.error(f"写信失败（{kind or '其他错误'}）：{e}")
            if kind:
                await alert_admins(bot, kind)
            return None
        if not letter or ooc_words(letter, ""):
            logger.info(f"信写得不好（出戏或为空），这次不寄：{letter[:40]}")
            return None
        try:
            async with _hands:
                await switch_pause(key)
                await asyncio.sleep(typing_delay(letter[:40]))
                await bot.send_private_msg(user_id=qq, message=letter)
                mark_sent(key)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"寄信给 {qq} 失败：{e}")
            return None
        history.append({"role": "assistant", "content": letter, "ts": time.time()})
        save_history(key)
    ltm.mark_letter(qq)
    _hour_window.append(time.monotonic())
    _global_window.append(time.monotonic())
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    _letters_sent[today] = _letters_sent.get(today, 0) + 1
    logger.info(f"给 {name}（{qq}）寄了一封信")
    return letter


async def check_letters() -> int:
    """看看今天要不要给谁写信；返回寄出了几封"""
    if not (cfg.letter_enabled and cfg.deepseek_api_key and _letter_window_now()):
        return 0
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    if _letters_sent.get(today, 0) >= cfg.letter_max_per_day or quota_left(None) <= 2:
        return 0
    bots = list(get_bots().values())
    if not bots:
        return 0
    bot = bots[0]
    try:
        friends = {int(f["user_id"]) for f in await bot.get_friend_list()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"获取好友列表失败，这次不写信：{e}")
        return 0
    candidates = [qq for qq in set(ltm.user_ids()) | set(cfg.close_friends) if letter_blocker(qq, friends) is None]
    random.shuffle(candidates)
    sent, p = 0, _letter_prob_per_check()
    for qq in candidates:
        if _letters_sent.get(today, 0) >= cfg.letter_max_per_day:
            break
        if random.random() < p and await write_letter(bot, qq):
            sent += 1
            await asyncio.sleep(random.uniform(30, 120))    # 连着寄几封时隔开一点
    return sent


async def _letter_loop() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await check_letters()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查写信出错：{e}")
        await asyncio.sleep(cfg.letter_check_interval)


letter_cmd = on_command("写信", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@letter_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """/写信 @某人（或 QQ号、我）：让她马上给这个人写一封（测试用，不看好感和时间）"""
    try:
        friends = {int(f["user_id"]) for f in await bot.get_friend_list()}
    except Exception:  # noqa: BLE001
        friends = set()
    if arg.extract_plain_text().strip() == "检查":
        close = [qq for qq in sorted(set(ltm.user_ids()) | set(cfg.close_friends)) if familiarity_of(qq) == "close"]
        if not close:
            await letter_cmd.finish("（现在还没有“很熟”的人，不会自动写信）")
        lines = [f"· {ltm.get_user(qq).get('name') or qq}（{qq}）：{letter_blocker(qq, friends) or '符合，随时可能收到信'}" for qq in close]
        today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
        head = f"（今天已寄 {_letters_sent.get(today, 0)}/{cfg.letter_max_per_day} 封｜现在{'可以' if _letter_window_now() else '不在'}寄信时段）"
        await letter_cmd.finish(head + "\n" + "\n".join(lines))
    target = _parse_target(event, arg)
    if not target or target[0] != "user":
        await letter_cmd.finish("用法：/写信 @某人（或 QQ号、我）马上写一封；/写信 检查 → 看看谁符合自动写信的条件")
    qq = target[1]
    why = letter_blocker(qq, friends, force=True)
    if why:
        await letter_cmd.finish(f"（没法给 {qq} 写信：{why}）")
    letter = await write_letter(bot, qq)
    auto = letter_blocker(qq, friends)
    note = "自动写信条件：符合" if auto is None else f"平时不会自动写：{auto}"
    await letter_cmd.finish(f"（信已寄给 {qq}｜{note}）" if letter else "（这次没写成，看看后台日志）")


# ------------------------------------------------------------------ 表情库：定时拉收藏、后台打标签
_sticker_task: "asyncio.Task | None" = None
_label_task: "asyncio.Task | None" = None


def _spawn_labeling() -> None:
    """后台给新表情打标签（高峰时段先停，等 _sticker_loop 低峰时再叫起来）"""
    global _label_task
    if not cfg.deepseek_api_key or (_label_task and not _label_task.done()):
        return

    async def run():
        n = await stickers.label_pending(can_run=lambda: not in_peak())
        if n:
            logger.info(f"表情：打好了 {n} 张的标签（{stickers.summary()}）")

    _label_task = asyncio.create_task(run())


async def _sticker_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            if time.time() - stickers.last_refresh >= cfg.sticker_refresh_hours * 3600:
                await stickers.refresh(bot, cfg.sticker_fetch_count)
            if stickers.unlabeled() and not in_peak():
                _spawn_labeling()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"表情：拉取收藏表情失败（{e}），稍后再试")
            stickers.last_refresh = time.time() - cfg.sticker_refresh_hours * 3600 + 600   # 10 分钟后重试
        await asyncio.sleep(600)


# ------------------------------------------------------------------ 未读消息：不在线时别人发的私聊，上线后补回
# 在线时每分钟记一次“心跳”；重新连上时，把心跳之后别人发来、她还没回的私聊找出来，一个一个补回
ONLINE_FILE = BOT_DIR / "data" / "online_state.json"
_seen_ids: deque = deque(maxlen=300)      # 在线时收到过的私聊消息 id（补回时跳过）
_heartbeat_task: "asyncio.Task | None" = None
_catchup_task: "asyncio.Task | None" = None


def _load_online() -> dict:
    try:
        return json.loads(ONLINE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_online() -> None:
    ONLINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ONLINE_FILE.write_text(json.dumps({"heartbeat": time.time(), "seen": list(_seen_ids)}), encoding="utf-8")


async def _is_private(event: MessageEvent) -> bool:
    return isinstance(event, PrivateMessageEvent)


seen_private = on_message(rule=Rule(_is_private), priority=1, block=False)


@seen_private.handle()
async def _(event: PrivateMessageEvent):
    _seen_ids.append(int(event.message_id))


def _seg_list(raw) -> Message:
    if isinstance(raw, str):
        return Message(raw)
    return Message([MessageSegment(type=s.get("type", "text"), data=s.get("data") or {}) for s in raw or []])


def catchup_event(bot_id: int, user_id: int, msgs: list[dict]) -> PrivateMessageEvent:
    """把几条未读私聊合成一条消息事件（中间用换行隔开）"""
    merged = Message()
    for i, m in enumerate(msgs):
        if i:
            merged.append(MessageSegment.text("\n"))
        merged.extend(_seg_list(m.get("message")))
    last = msgs[-1]
    sender = last.get("sender") or {}
    return PrivateMessageEvent(
        time=int(last.get("time", time.time())), self_id=bot_id, post_type="message", sub_type="friend",
        user_id=user_id, message_type="private", message_id=int(last.get("message_id", 0)),
        message=merged, original_message=merged, raw_message=str(merged), font=0,
        sender={"user_id": user_id, "nickname": sender.get("nickname") or str(user_id)}, to_me=True,
    )


async def find_unread(bot: Bot, offline_since: float, seen: set[int]) -> list[tuple[int, list[dict]]]:
    """找出 offline_since 之后别人发来、还没回过的私聊。返回 [(QQ, [消息...])]，最早的人排前面"""
    now = time.time()
    oldest_ok = max(offline_since - 60, now - cfg.catchup_max_age_hours * 3600)
    try:
        friends = {int(f["user_id"]) for f in await bot.get_friend_list()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"获取好友列表失败，不补回未读：{e}")
        return []
    # 最近联系人里，最后一条消息在离线之后的私聊（拿不到就退回：所有聊过天的好友）
    try:
        recent = await bot.call_api("get_recent_contact", count=30)
        people = [int(c["peerUin"]) for c in recent or []
                  if int(c.get("chatType", 0)) == 1 and float(c.get("msgTime") or 0) >= oldest_ok]
    except Exception as e:  # noqa: BLE001
        logger.info(f"拿不到最近联系人（{e}），改为检查聊过天的好友")
        people = [int(f.stem.split("_")[1]) for f in HISTORY_DIR.glob("private_*.json") if f.stem.split("_")[1].isdigit()]
    out = []
    for qq in dict.fromkeys(people):
        if qq not in friends or qq == int(bot.self_id):
            continue
        if cfg.private_whitelist and qq not in cfg.private_whitelist:
            continue
        try:
            data = await bot.call_api("get_friend_msg_history", user_id=qq, count=20)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"获取 {qq} 的聊天记录失败：{e}")
            continue
        msgs = sorted((data or {}).get("messages", []) if isinstance(data, dict) else data or [], key=lambda m: m.get("time", 0))
        # 只看她最后一次说话之后、对方发的消息
        mine = [m for m in msgs if int(m.get("user_id", 0)) == int(bot.self_id)]
        after = mine[-1]["time"] if mine else 0
        last_hist = next((h["ts"] for h in reversed(get_history(f"private_{qq}")) if h.get("ts") and h["role"] == "user"), 0)
        unread = [m for m in msgs
                  if int(m.get("user_id", 0)) == qq
                  and m.get("time", 0) > after and m.get("time", 0) >= oldest_ok and m.get("time", 0) > last_hist
                  and int(m.get("message_id", 0)) not in seen]
        # 纯指令（/重置 之类）不补
        unread = [m for m in unread if not message_to_text(_seg_list(m.get("message"))).startswith(tuple(bot.config.command_start or {"/"}))]
        if unread:
            out.append((qq, unread[-8:]))    # 每人最多看最近 8 条
    out.sort(key=lambda x: x[1][0].get("time", 0))
    return out[: cfg.catchup_max_people]


async def catch_up(bot: Bot) -> int:
    """上线后补回未读私聊，返回补回了几个人"""
    state = _load_online()
    since = state.get("heartbeat")
    if not (cfg.catchup_enabled and cfg.enable_private and cfg.deepseek_api_key) or not since:
        return 0
    seen = set(state.get("seen", [])) | set(_seen_ids)
    todo = await find_unread(bot, float(since), seen)
    if not todo:
        return 0
    logger.info(f"上线后发现 {len(todo)} 个人有未读私聊，准备补回")
    done = 0
    await asyncio.sleep(random.uniform(5, 20))                 # 刚“上线”，先缓一缓
    for i, (qq, msgs) in enumerate(todo):
        if i:
            await asyncio.sleep(random.uniform(cfg.catchup_gap_min, cfg.catchup_gap_max))
        ev = catchup_event(int(bot.self_id), qq, msgs)
        if not _allowed(ev):
            continue
        try:
            await converse(bot, ev, catchup_age=time.time() - msgs[0].get("time", time.time()))
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"补回 {qq} 的未读失败：{e}")
        for m in msgs:
            _seen_ids.append(int(m.get("message_id", 0)))
    _save_online()
    return done


async def _heartbeat_loop() -> None:
    while True:
        try:
            _save_online()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"记录在线状态失败：{e}")
        await asyncio.sleep(60)


@get_driver().on_bot_connect
async def _(bot: Bot):
    global _heartbeat_task, _catchup_task, _sticker_task
    if cfg.sticker_enabled:
        if _sticker_task:
            _sticker_task.cancel()
        _sticker_task = asyncio.create_task(_sticker_loop(bot))
    if not cfg.catchup_enabled:
        return

    async def run():
        try:
            n = await catch_up(bot)
            if n:
                logger.info(f"已补回 {n} 个人的未读私聊")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"补回未读出错：{e}")
        global _heartbeat_task
        if _heartbeat_task is None or _heartbeat_task.done():
            _heartbeat_task = asyncio.create_task(_heartbeat_loop())

    _catchup_task = asyncio.create_task(run())    # 先补未读，补完再开始记心跳（免得心跳把离线时间盖掉）


@get_driver().on_bot_disconnect
async def _(bot: Bot):
    global _heartbeat_task, _sticker_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
    if _sticker_task:
        _sticker_task.cancel()
        _sticker_task = None
    if cfg.catchup_enabled:
        try:
            _save_online()
        except Exception:  # noqa: BLE001
            pass


@get_driver().on_shutdown
async def _():
    for t in (_letter_task, _heartbeat_task, _catchup_task, _sticker_task, _label_task):
        if t:
            t.cancel()



@get_driver().on_startup
async def _():
    global _kb, _letter_task
    if tagger:
        tagger.start()     # 后台准备角色识别模型（第一次会下载），不耽误启动
    if cfg.letter_enabled and cfg.enable_private:
        _letter_task = asyncio.create_task(_letter_loop())
    if cfg.knowledge_enabled:
        try:
            _kb = await asyncio.to_thread(
                knowledge.build_or_load,
                BOT_DIR / cfg.knowledge_summary_dir,
                BOT_DIR / cfg.knowledge_novel_dir,
                BOT_DIR / cfg.knowledge_cache,
                BOT_DIR / cfg.knowledge_characters_file,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"知识库加载失败，将不使用小说检索：{e}")
    if not cfg.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，机器人会提示未配置")
    logger.info(
        f"角色扮演聊天已启动：model={cfg.deepseek_model} 记忆=群聊{cfg.history_max_turns_group}条/私聊{cfg.history_max_turns}条 "
        f"群白名单={cfg.group_whitelist or '全部'} 长期记忆={'开' if cfg.memory_enabled else '关'} "
        f"高峰时段模式={'开' if cfg.peak_enabled else '关'}（现在{'是' if in_peak() else '不是'}高峰）"
    )
    logger.info(
        f"表情包：{'开' if cfg.sticker_enabled else '关'}（{stickers.summary()}）" + ("，连上 QQ 后会自动拉取收藏表情" if cfg.sticker_enabled else "")
    )


# ------------------------------------------------------------------ QQ 空间日记（放在最后导入：它要用到上面定义的东西）
from . import qzone_diary  # noqa: E402
