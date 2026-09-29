"""
角色扮演聊天插件
- 群聊：被 @、被回复、被叫名字时回复；看得出在跟她说话或提到她时也会回复（先让模型判断一下）；紧接着回复时直接发，中间有人插话才用“回复”引用
- 私聊：直接回复（可在 .env 关闭）
- 人设：personas/*.md，支持热重载
- 记忆：每个群 / 每个私聊一份，落盘到 data/history，重启不丢
- 风控：个人冷却、全局限速、随机打字延迟
- 知识库：从小说摘要和原文中检索相关片段，作为“回忆”提供给模型
- 长期记忆：每个人的档案 + 群往事，每轮对话攒够一批就在后台整理而成
- 出错时不在群里发消息；余额不足 / Key 失效会私信管理员
- 时间感：知道对方隔了多久才来找她、上一段聊天是多久以前
- 写信：好感到“很熟”的好友，隔一段时间没来找她时，她偶尔会主动私聊寄一封信
- 节奏：同一时间只给一个人打字（其他人排队）；按字数算打字时间；不在线时的私聊，上线后补回
- 表情包：有一定概率用小号收藏表情里的伊蕾娜表情表达情绪（跟在文字后面，或者只发一张），见 stickers.py
"""
import asyncio
import contextlib
import json
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import get_bots, get_driver, get_plugin_config, logger, on_command, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me
from openai import AsyncOpenAI

from . import budget, factcheck, knowledge, peak
from .memory import LongTermMemory
from .stickers import EMOTIONS, StickerStore
from .stickers import normalize as sticker_normalize
from .tagger import Tagger
from .vision import Vision
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="角色扮演聊天",
    description="DeepSeek 驱动、带人设和上下文记忆的聊天机器人",
    usage="群里 @机器人 说话；/重置 清空短期记忆；管理员：/重载人设、/记忆、/忘记、/好感、/花费、/写信、/说说、/表情",
    config=Config,
)

cfg = get_plugin_config(Config)
# bot 目录（这个文件在 bot/plugins/roleplay_chat/ 下）：不依赖“从哪个目录启动”，服务器上用 systemd 常驻也找得到数据
BOT_DIR = Path(__file__).resolve().parents[2]
if cfg.log_file:
    try:   # 控制台的日志也写进文件（按天分），事后能查“为什么没回”
        logger.add(str(BOT_DIR / cfg.log_file), level="INFO", encoding="utf-8", rotation="00:00",
                   retention=f"{cfg.log_retention_days} days", enqueue=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"日志文件打不开，只在控制台显示：{e}")
HISTORY_DIR = BOT_DIR / cfg.history_dir
_SAME_NAMES_FILE = BOT_DIR / "data" / "same_names.json"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

client = AsyncOpenAI(
    api_key=cfg.deepseek_api_key or "missing",
    base_url=cfg.deepseek_base_url,
    timeout=cfg.llm_timeout,
)

spend = budget.setup(budget.Budget(
    BOT_DIR / "data" / "usage", enabled=cfg.budget_enabled, total=cfg.budget_daily_total, reserve=cfg.budget_reserve,
    user_share=cfg.budget_user_share, group_share=cfg.budget_group_share, reset_hour=cfg.budget_reset_hour,
    price=dict(cfg.budget_price), peak_multiplier=cfg.budget_peak_multiplier, peak_ranges=cfg.budget_peak_ranges,
    holidays=peak.HOLIDAYS_2026 + list(cfg.peak_holidays),
))

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


if (cfg.affection_dislike, cfg.affection_acquaintance, cfg.affection_close) == (-20, 30, 70):
    # 9/29 分数范围从 -100～100 换成 -50～150；.env 里还是照旧版 .env.example 抄的档位，按新档位来，不然换算后的分数全对不上
    logger.warning("好感：.env 里的 AFFECTION_DISLIKE/ACQUAINTANCE/CLOSE 还是旧的 -20/30/70，已按新档位 0/90/130 处理，"
                   "请把这三行删掉或改成新值（见 .env.example）")
    cfg.affection_dislike, cfg.affection_acquaintance, cfg.affection_close = 0, 90, 130
ltm_affection = {
    "base_gain": cfg.affection_chat_gain, "daily_cap": cfg.affection_daily_cap,
    "decay_after_days": cfg.affection_decay_after_days, "decay_per_day": cfg.affection_decay_per_day,
    "min": cfg.affection_min, "max": cfg.affection_max, "start": cfg.affection_start,
    "dislike": cfg.affection_dislike, "friend": cfg.affection_friend,
    "acquaintance": cfg.affection_acquaintance, "close": cfg.affection_close,
    "summary_daily_cap": cfg.affection_summary_daily_cap,
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
ltm.summary_max_tokens = cfg.memory_summary_max_tokens
ltm.summary_client = client.with_options(timeout=cfg.memory_summary_timeout, max_retries=0)   # 整理：超时更长，出错不自动重试

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
- 群聊中，每条消息开头的「【说话人 → 对象】」标明谁在跟谁说：“→ 你”是在跟你说；“→ 别的名字”是在跟那个人说（@ 了他或回复他），不是对你说的；没有“→”是随口说给大家的。名字后面标“（群友）”的，是恰好和你同名的群友，不是你。名字后面带“#2”“#3”的，是和别人重名的另一位群友（“小明”和“小明#2”是两个人）；称呼对方时不要带这个记号，要区分时可以说“另一位小明”。私聊没有这个前缀。「（过了 X）」表示中间隔了这么久。
- 你回复时直接说话：开头不加「【名字】」「→」，不用引号包住整句，不用 Markdown，只用中文（对方的昵称原样称呼可以）。
- 这是 QQ 聊天：日常闲聊一两句、十几二十个字；只有讲故事、讲具体经历、详细解释、对方认真倾诉时才说长一点。每轮末尾会提示你这次该短还是可以长。想分成几条发就用换行分开（最多三条），不用每句都加句号。
- 「（群聊旁听记录）」开头的是你没被 @ 时群里的聊天，只作背景；但里面有人明显在问你、你还没回答，就顺带答一下。
- 图片：「[图片：……]」就当你亲眼看到了那张图，自然地回应，别提“描述”这个词；「[图片]」是你没看清，可以直说。只有写着「伊蕾娜本人的画像」的才是画的你（照人设“看到自己的画像”那一节反应），其他图里的人哪怕也是灰发少女，也不是你。
- 表情：「[发了表情：……]」是你之前甩过去的小画像，对方问起就接话。只有本轮末尾明确允许时，才能写「[表情:情绪]」。别人发来的「[图片]」「[表情]」「[语音]」这类说明，你回复时一律不要写。
- 始终保持人设：你是真实存在于这个世界的旅行魔女，不知道、也绝不主动提起服务器、程序、代码、AI、机器人、模型、网络、动漫、动画、小说、作者、声优、角色扮演、人设这些概念。有人说你是 AI、机器人、动漫角色，或者说“OOC”，就当成听不懂的外国话，用你的方式岔开或吐槽回去，不解释，也不顺着说。
- 分清谁在说、在跟谁说：“→ 别人”的话别替别人回答，群里的“你”也常常是在说别人；对方说“那个人说……”“他说……”“书里写……”，说话的是第三个人，不是你也不是对方，别回“我没说过”，顺着去想那是谁、在什么时候说的。
- 对方只是叫你一声、问你在不在，就像真人一样回“嗯？”“怎么了？”“在，有什么事？”；只是问好，就回个好，可以顺口聊一句（比如问问吃过饭没有），别接“怎么了？”“找我有事？”；对方说了只是想聊天，就接着聊。别回“要问什么？”“有什么问题？”“你想问什么就问吧”——别人找你多半只是想聊天。
- 自己说错了就认：对方纠正你讲的往事（“不对，是……”），或者说你刚才那句莫名其妙（“诶？”“哪有……”），先想想是不是自己记混、说岔了；是的话就先承认是自己记混了、扯远了，再自然地改口，每次说法不一样，可以嘴硬，不用道歉；拿不准就说记不太清。别硬圆，也别说成是对方记错了、听错了、先提的。（这和“有主见”不冲突：有主见说的是不附和别人的看法；自己的话说错了，就认。）
- 你的喜好和讨厌的东西（面包、钱、蘑菇……）只在话题真的碰到时才提，别硬扯进来。
- 不要主动问对方是男是女。
""".strip()


# 补回未读时加的提示（拼在“【刚看到】……你现在才看到。”后面）。以前第一个例子是“刚才在赶路”，于是一字不差说了好几次
LATE_HINT = ("回的时候可以随口带一句刚看到，理由自己想、换着说（比如刚在旅馆睡了一觉、刚在集市逛了逛、刚在写日记），"
             "别总说在赶路，一句带过就行；不要提掉线、离线、手机、网络这类词，也不用道歉。")


def system_prompt() -> str:
    return f"{_persona}\n\n{CHAT_RULES}"


# ------------------------------------------------------------------ 小说知识库
_kb: "knowledge.KnowledgeBase | None" = None
_fc: "factcheck.FactChecker | None" = None     # 记混检查（人和地方对不对得上）

RECALL_RULES = (
    "【回忆参考】以下是你旅行日记里可能和当前话题有关的内容，供你回想，不是对方说的话。\n"
    "- 只在确实相关时，用你自己的口吻简短讲述，像在回忆往事；不要大段背诵原文，不要提“卷”“章”。和话题无关的片段就当没看见。\n"
    "- 标注“角色资料”的是这个人的确切资料（外貌、喜好等以它为准）；“摘要”是整段经历的梗概；“原文”是当时的片段；"
    "“刚才聊到的”是你们刚才在聊的那段经历。\n"
    "- 片段里没写的细节不要编：记不清就说记不清；对方说起片段里没有的细节、别人说过的话，别断然否认，也别编一个结局，说记不太清，或者问对方是谁说的。\n"
    "- 片段和你上面自己说过的话对不上（比如地方、人对不上）：以片段为准，自然地改口，别顺着说错的继续编。\n"
    "- 这些都是你以前旅途里的事，不是今天发生的；别说成今天的经历，也别和今天的日记混在一起。\n"
    "- 对方只是在闲聊、没问起往事时，一般用不上这些片段；真要提，得先讲清楚是哪件事，别像对方早就知道一样突然冒出片段里的细节。"
)



# ------------------------------------------------------------------ 核对讲的往事（9/29）
# 讲长故事时再调一次模型，拿查到的资料核对：人、地点、谁做了什么、结局有没有和资料矛盾。讲错了就告诉她哪里错、重说一次
STORY_CHECK_PROMPT = """下面是角色扮演里“伊蕾娜”（小说《魔女之旅》的主角）刚写好的一条回复，以及她旅行日记里的相关资料。
请核对：回复里讲到的往事，有没有和资料**矛盾**的地方——人物张冠李戴、地点弄错、把两段经历拼在一起、谁做了什么说反、结局说错。

规则：
- 只看和资料矛盾的地方。资料里没写到的细节、她的感想和语气、玩笑和夸张，都不算错。
- 回复没有在讲往事（闲聊、说现在的事），直接算没问题。
- 拿不准就算没问题。

只输出 JSON：没问题就 {{"ok": true}}；有问题就 {{"ok": false, "problems": ["一句话说清哪里错了、资料里其实是怎样（不超过 50 字）"]}}，最多 3 条。

【资料】
{evidence}

【伊蕾娜的回复】
{reply}"""

STORY_FIX_PROMPT = ("【讲错了】你刚才讲的往事和日记对不上：\n{problems}\n"
                    "请重新回复这条消息：按日记里的来讲，拿不准的细节就说记不太清了。不要道歉，也不要提自己刚才讲错了。")


def _story_evidence(reply: str, memo: str) -> str:
    """核对用的资料：这一轮带给她的回忆片段 + 回复里提到的人的档案、提到的地方那几段经历、按回复内容再查的摘要"""
    parts, seen = [], set()
    body = memo.split("\n\n", 1)[1] if memo.startswith(RECALL_RULES) and "\n\n" in memo else memo
    if body:
        parts.append(body[:1500])
    if _fc is not None:
        lines = _fc.overview_for(reply)
        if lines:
            parts.append("旅途总览：\n" + "\n".join(lines))
    if _kb is not None:
        for d in _kb.characters_in(reply)[:2]:
            if d.label not in body and d.label not in seen:
                seen.add(d.label)
                parts.append(f"【{d.label}】{d.content[:700]}")
        for score, d in _kb.search(reply, "summary", 2):
            if score >= cfg.knowledge_min_summary_score and d.label not in body and d.label not in seen:
                seen.add(d.label)
                parts.append(f"【{d.label}】{d.content[:600]}")
    return "\n\n".join(parts)[:3500]


def _tells_story(reply: str, memo: str) -> bool:
    if len(re.sub(r"\s", "", reply)) < cfg.story_check_min_chars:
        return False
    if memo:
        return True
    if _fc is not None:
        places, people = _fc.mentions(reply)
        if places or people:
            return True
    return bool(_kb is not None and _kb.characters_in(reply))


async def story_check(reply: str, memo: str, user_id: int | None = None, group_id: int | None = None) -> list[str] | None:
    """讲长故事时核对一遍。返回讲错的地方；没问题、不用核对、核对出错都返回 None（出错不影响回复）"""
    if not cfg.story_check or not reply or not _tells_story(reply, memo):
        return None
    evidence = _story_evidence(reply, memo)
    if not evidence:
        return None
    try:
        r = await client.chat.completions.create(
            model=cfg.deepseek_model,
            messages=[{"role": "user", "content": STORY_CHECK_PROMPT.format(evidence=evidence, reply=reply)}],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        budget.track(r, "verify", user=user_id, group=group_id)
        data = json.loads(r.choices[0].message.content or "{}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"核对往事失败，照原样发：{e}")
        return None
    if not isinstance(data, dict) or data.get("ok", True) is not False:
        return None
    problems = [str(x).strip()[:80] for x in (data.get("problems") or []) if str(x).strip()][:3]
    return problems or None


_FOLLOWUP_WORDS = (
    "后来", "然后", "接着", "结果", "那个", "那位", "哪位", "哪一位", "是谁", "她", "他",
    "为什么", "怎么", "还有呢", "继续", "什么故事", "详细", "具体",
    "实际上", "其实", "不对", "记错", "记得", "想起来", "那句", "说过", "当时",
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


RECALL_CARRY_MINUTES = 30      # 刚才聊到的那段经历，这么久内接着带上
RECALL_CARRY_TURNS = 3         # 最多再带几轮（话题早换了就别再带）
_last_recall: dict[str, dict] = {}   # 会话 -> {"at": 时间, "picks": [(标题, 类型, 内容)], "left": 还能带几轮}


def _strip_her_names(text: str) -> str:
    """去掉话里叫她的名字（伊蕾娜小姐、魔女小姐、昵称……），剩下的才是话题"""
    names = sorted({n for n in _her_names() + ["伊蕾娜小姐", "伊蕾娜大人", "伊雷娜"] if n}, key=len, reverse=True)
    for n in names:
        if "之魔女" in n:                 # “灰之魔女”这种称号也可能是话题（“你考灰之魔女那次”），只去掉开头叫人的那种
            text = re.sub(rf"^\s*{re.escape(n)}[，,、!！\s]*", " ", text)
        else:
            text = text.replace(n, " ")
    return re.sub(r"\s+", " ", text).strip()


def recall(query: str, extra: str = "", key: str | None = None) -> str:
    """根据对方的话检索回忆；没有足够相关的就返回空字符串

    extra：上文（上一句用户消息 + 伊蕾娜上一句回复），只在对方像是在追问时才用
    key：会话。聊着某段经历时，接下来几轮哪怕对方只说“后来呢”“实际上并不是”，也把那段经历带上，
         不然她这一轮就不记得刚才在聊什么，只能瞎编
    """
    if not _kb or len(query) < 2:
        return ""
    followup = bool(extra) and any(w in query for w in _FOLLOWUP_WORDS)
    carried = _last_recall.get(key) if key else None
    if carried and (time.time() - carried["at"] > RECALL_CARRY_MINUTES * 60 or carried["left"] <= 0):
        _last_recall.pop(key, None)
        carried = None

    # 1) 话里直接点名的角色：精确带上角色档案（外貌、喜好等以档案为准，避免乱编）
    chars = _kb.characters_in(query)
    if not chars and followup:
        chars = _kb.characters_in(extra)
    char_picks = [(d.label, "角色资料", brief_profile(d.content)) for d in chars[: cfg.knowledge_top_characters]]

    # 2) 摘要 + 原文：BM25 检索。先用这句话本身查；像是在追问、而这句话本身查到的都不太像时，带上上文再查一次，
    #    结果接在后面（“后来发生了什么”单独查，只会查到些不相干的；但这句话里有新线索时，以它为准）
    # 叫她的名字（“伊蕾娜小姐……”）不算话题：小说里到处都是她的名字，带着它查，闲聊也会查到不相干的片段
    q_topic = _strip_her_names(query)
    x_topic = _strip_her_names(extra)
    # 很短的一句（“哇暴露本性了”“真的假的”）没多少线索，原文片段要更像才带
    chunk_min = cfg.knowledge_min_chunk_score * (1.25 if len(q_topic) < 10 and not followup else 1.0)

    def search(q: str) -> list[tuple[float, tuple[str, str, str]]]:
        out = [(s / cfg.knowledge_min_summary_score, (d.label, "摘要", d.content[: cfg.knowledge_summary_chars]))
               for s, d in _kb.search(q, "summary", cfg.knowledge_top_summaries) if s >= cfg.knowledge_min_summary_score]
        out += [(s / chunk_min * 0.8, (d.label, "原文", d.content[: cfg.knowledge_chunk_chars]))
                for s, d in _kb.search(q, "text", cfg.knowledge_top_chunks) if s >= chunk_min]
        return out

    direct = search(q_topic) if len(q_topic) >= 3 else []
    picks = [pk for _, pk in direct]
    if followup and max([sc for sc, _ in direct] + [0.0]) < 1.3:
        have = {(p[0], p[1]) for p in picks}
        more = [pk for _, pk in sorted(search(f"{x_topic} {q_topic}"), key=lambda x: -x[0]) if (pk[0], pk[1]) not in have]
        picks += more[:2]
    # 3) 刚才聊到的那段经历（按她上一轮自己讲的内容记下的，见 note_topic）：
    #    追问时放在最前面；这句话本身什么都没查到（“还真是”“哈哈”），放在后面备用；别的时候不带
    if carried:
        strong = max([sc for sc, _ in direct] + [0.0]) >= 1.3
        if (followup and not strong) or not direct:
            have = {p[0] for p in picks}
            extra_picks = [(label, kind + "·刚才聊到的", content) for label, kind, content in carried["picks"] if label not in have]
            picks = extra_picks[:1] + picks if followup else picks + extra_picks[:1]
            carried["left"] -= 1
    picks = char_picks + picks
    if not picks:
        return ""
    body = "\n\n".join(f"〔{kind}｜{label}〕\n{content}" for label, kind, content in picks)
    logger.info(f"回忆命中：{[p[0] for p in picks]}")
    return f"{RECALL_RULES}\n\n{body}"


_diary_topic: dict[str, float] = {}    # 会话 -> 上次聊到她日记 / 说说的时间


def diary_followup(key: str, text: str) -> bool:
    """刚才在聊她的日记，这句是在接着追问（“然后呢”“还有吗”、很短的一句）"""
    at = _diary_topic.get(key)
    if not at or time.time() - at > 600:
        return False
    return len(text.strip()) <= 8 or any(w in text for w in _FOLLOWUP_WORDS + ("还有吗", "写了什么", "写的什么"))


def note_topic(key: str, reply: str) -> None:
    """她这一轮讲了哪段经历：用她自己说的话查一下，查到很像的就记下，接下来几轮对方追问时接着带上"""
    if not _kb or len(reply) < 8:
        return
    sums = [(s / cfg.knowledge_min_summary_score, d) for s, d in _kb.search(reply, "summary", 5)]
    texts = [(s / cfg.knowledge_min_chunk_score * 0.8, d) for s, d in _kb.search(reply, "text", 5)]
    cand = sums + texts
    cur = _last_recall.get(key)
    if cur and time.time() - cur["at"] <= RECALL_CARRY_MINUTES * 60:
        # 还在聊刚才那段：她这句话和那段也沾边，就接着记那段（别被她话里的一两个词带去别的经历）
        labels = {p[0] for p in cur["picks"]}
        if any(d.label in labels and sc >= 0.6 for sc, d in cand):
            cur["at"], cur["left"] = time.time(), RECALL_CARRY_TURNS
            return
    best = max(cand, key=lambda x: x[0], default=None)
    if not best or best[0] < 1.3:
        return
    label = best[1].label
    # 同一段经历有摘要就带摘要（有来龙去脉和结局），没有就带那段原文
    summary = next((d for d in _kb.docs if d.kind == "summary" and d.label == label), None)
    doc, kind = (summary, "摘要") if summary else (best[1], "原文" if best[1].kind == "text" else "摘要")
    size = cfg.knowledge_summary_chars if kind == "摘要" else cfg.knowledge_chunk_chars
    _last_recall[key] = {"at": time.time(), "picks": [(label, kind, doc.content[:size])], "left": RECALL_CARRY_TURNS}


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


def message_to_text(msg: Message, drop_at: bool = False) -> str:
    """drop_at=True：去掉 @ 段（群聊里 @ 谁单独写在说话人后面，不混进正文）"""
    return "".join(seg_text(seg) for seg in msg if not (drop_at and seg.type == "at")).strip()


_BOT_SEGMENTS = {"markdown", "json", "xml", "ark", "keyboard", "button"}


def is_bot_like(event: MessageEvent) -> bool:
    """别的机器人发的（Markdown、卡片消息），或者在忽略名单里的人：完全不理"""
    if event.user_id in cfg.ignore_users:
        return True
    return any(seg.type in _BOT_SEGMENTS for seg in event.get_message())


def talking_to_others(event: MessageEvent) -> str | None:
    """群里 @ 了别人、或者回复的是别人的消息：返回那个人的名字（她只是旁观者）；否则 None"""
    if not isinstance(event, GroupMessageEvent):
        return None
    self_id = str(event.self_id)
    at_others = [seg for seg in event.get_message()
                 if seg.type == "at" and str(seg.data.get("qq")) not in (self_id, "all")]
    if at_others:
        return str(at_others[0].data.get("name") or at_others[0].data.get("qq"))
    reply = getattr(event, "reply", None)
    if reply is not None and str(getattr(reply.sender, "user_id", "")) != self_id:
        return str(reply.sender.card or reply.sender.nickname or reply.sender.user_id)
    return None


_HEAD_BAD_RE = re.compile(r"[【】\[\]［］→\r\n]")


def _clean_name(name) -> str:
    """说话人名字里去掉【】→ 这些格式符号，免得有人改群名片冒充别人、冒充格式"""
    name = _HEAD_BAD_RE.sub("", str(name or "")).strip()
    name = _DUP_MARK_RE.sub("", name).strip()       # 名片里自己写“#2”冒充重名记号：去掉
    return name[:16] or "某人"


_DUP_MARK_RE = re.compile(r"#\d{1,2}$")


# ---- 重名：同一个群里两个人名字一样时，后来的那位标成“名字#2”（#3……），先出现的不标
# 按群记在 data/same_names.json：群号 -> 名字 -> {QQ: [第一次见到, 最近一次见到]}；30 天没出现的不算
_same_names: dict | None = None
SAME_NAME_ACTIVE_DAYS = 30


def _names_reg() -> dict:
    global _same_names
    if _same_names is None:
        try:
            _same_names = json.loads(_SAME_NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _same_names = {}
    return _same_names


def _save_names_reg() -> None:
    try:
        now = time.time()
        for g in _names_reg().values():                      # 90 天没出现的删掉
            for n in list(g):
                g[n] = {q: v for q, v in g[n].items() if now - v[1] <= 90 * 86400}
                if not g[n]:
                    del g[n]
        tmp = _SAME_NAMES_FILE.with_name(_SAME_NAMES_FILE.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_names_reg(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_SAME_NAMES_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"重名记录存盘失败：{e}")


def name_label(gid: int | None, qq, name) -> str:
    """群里的称呼：名字和别人重了，就按先来后到加“#2”“#3”；没重名就是原名"""
    n = _clean_name(name)
    if not gid or not str(qq).isdigit() or n == "某人":
        return n
    owners = _names_reg().setdefault(str(gid), {}).setdefault(n, {})
    now, q = time.time(), str(qq)
    rec = owners.get(q)
    if rec is None:
        owners[q] = [now, now]
        _save_names_reg()
    elif now - rec[1] > 86400:                              # 最近一次见到的时间，一天更新一次就够
        rec[1] = now
        _save_names_reg()
    active = sorted((v[0], k) for k, v in owners.items() if k == q or now - v[1] <= SAME_NAME_ACTIVE_DAYS * 86400)
    if len(active) <= 1:
        return n
    k = [x for _, x in active].index(q) + 1
    return n if k == 1 else f"{n}#{k}"


def sender_label(event: MessageEvent) -> str:
    """说话人在聊天记录里的名字：群里重名的会带“#2”；私聊就是原名"""
    if isinstance(event, GroupMessageEvent):
        return name_label(event.group_id, event.user_id, sender_name(event))
    return sender_name(event)


def clean_body(text: str) -> str:
    """正文里的【】换成 []，免得有人在消息里伪造“【某某 → 你】”这种格式"""
    return text.replace("【", "[").replace("】", "]")


def _her_names() -> list[str]:
    try:
        nick = list(get_driver().config.nickname or [])
    except Exception:  # noqa: BLE001
        nick = []
    return [n for n in list(cfg.smart_names) + nick if n]


def _display_name(name) -> str:
    """群友的名字和她一模一样（或者就是她的名字加一两个字）：标成“（群友）”，免得她以为是自己、或者以为别人在叫她"""
    n = _HEAD_BAD_RE.sub("", str(name or "")).strip()[:20] or "某人"    # 传进来的已经是 name_label 整理过的，保留“#2”
    plain = re.sub(r"\s", "", n).lower()
    for her in _her_names():
        h = her.lower()
        if plain == h or (h in plain and len(plain) <= len(h) + 2):
            return f"{n}（群友）"
    return n


def speaker_head(event: MessageEvent, you: str = "你") -> str:
    """群消息的说话人和对象，单独写在正文前面：
    【Dev_Yanxi → 魔女西西】  【魔女西西 → Dev_Yanxi（回复）】  【鲨鱼（管理员）→ 你】  【羽水墨】（没指定对象）"""
    name = _display_name(sender_label(event))
    if not isinstance(event, GroupMessageEvent):
        return f"【{name}】"
    self_id = str(event.self_id)
    targets = []
    if event.is_tome():
        targets.append(you)
    for seg in event.get_message():
        if seg.type == "at":
            qq = str(seg.data.get("qq"))
            if qq == self_id:
                targets.append(you)
            elif qq == "all":
                targets.append("全体")
            else:
                targets.append(_display_name(name_label(event.group_id, qq, seg.data.get("name") or qq)))
    reply = getattr(event, "reply", None)
    if reply is not None:
        if str(getattr(reply.sender, "user_id", "")) == self_id:
            targets.append(you)
        else:
            targets.append(_display_name(name_label(event.group_id, getattr(reply.sender, "user_id", ""),
                                                    reply.sender.card or reply.sender.nickname or reply.sender.user_id)) + "（回复）")
    targets = list(dict.fromkeys(targets))[:3]
    return f"【{name} → {'、'.join(targets)}】" if targets else f"【{name}】"


def gap_line(prev_ts: float | None, ts: float) -> str:
    """两条消息隔得久（30 分钟以上），中间插一行“（过了 X）”"""
    if prev_ts and ts - prev_ts >= 1800:
        return f"（过了 {human_gap(ts - prev_ts)}）\n"
    return ""


def watch_block(items: list[tuple[float, str]]) -> str:
    """旁听记录：每条（时间, 已经带说话人的一行），隔得久的中间标出来"""
    out, prev = [], None
    for ts, line in items:
        g = gap_line(prev, ts)
        if g:
            out.append(g.strip())
        out.append(line)
        prev = ts
    return "（群聊旁听记录）\n" + "\n".join(out)


async def rich_text(event: MessageEvent, look: bool, drop_at: bool = False) -> str:
    """和 message_to_text 一样，但会“看”图片，把 [图片] 换成 [图片：描述]；引用的消息里有图也会看。
    drop_at=True：去掉 @ 段（群聊里 @ 谁写在说话人后面）"""
    msg = event.get_message()
    if drop_at:
        msg = Message([seg for seg in msg if seg.type != "at"])
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


_PREFIX_RE = re.compile(r"^\s*(?:【[^】]{1,40}】|[^\s【】]{1,16}\s*→\s*[^\s：:]{1,16}\s*[:：])\s*[:：]?\s*")


def clean_reply(text: str, truncated: bool = False, keep_sticker: bool = False) -> str:
    text = _PREFIX_RE.sub("", text.strip())
    text = re.sub(r"(?<=[^\s\d#])#\d{1,2}(?![\d])", "", text)     # 重名记号“小明#2”：她说出来时去掉
    text = _META_PAREN_RE.sub("", text).strip()                      # 抄进来的提示说明
    no_action = re.sub(r"\n{3,}", "\n\n", _ACTION_PAREN_RE.sub("", text)).strip()
    if no_action:                         # 括号里写的纯动作（“（过了两秒）”“（打了个哈欠）”）：去掉；整条只有动作就不动
        text = no_action
    if not keep_sticker:                  # 写信、空间评论这些地方发不了表情：模型写了表情标记就去掉
        text = _FAKE_MEDIA_RE.sub("", _STICKER_RE.sub("", text)).strip()
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
    # 9/30 00:30：不太熟的人跟她倾诉时，“可以说长一点”让她讲很长、还老讲自己拜师哭的那段。人设里写“点到为止”会被原样抄出来，
    # 所以放在程序里：不熟的人倾诉就只给这档长度
    "vent_stranger": "（对方在跟你倾诉，但你们还不太熟：认真回应，两三句话、60 字以内就够了。）",
}

# 倾诉的说法（和 _LONG_HINTS 里想听故事、要解释的那些分开）
_VENT_HINTS = ("难过", "伤心", "好累", "烦死", "崩溃", "想哭", "失恋", "压力", "考砸", "骂了我", "被骂", "做不好", "好烦", "委屈",
               "没用", "撑不住", "睡不着", "不开心")
_STORY_HINTS = ("讲讲", "讲个", "讲一下", "说说", "聊聊", "故事", "经历", "详细", "具体", "介绍", "解释")


def length_hint_for(mode: str, text: str, fam: str) -> str:
    if mode == "short":
        return short_hint(text)
    if (mode == "long" and fam in ("stranger", "disliked") and any(k in text for k in _VENT_HINTS)
            and not any(k in text for k in _STORY_HINTS)):
        return LENGTH_HINT["vent_stranger"]
    return LENGTH_HINT[mode]


# 出戏检查：标准在 oocheck.py（线上和回归测试共用）。她自己冒出来的、或者当成懂的概念来用的，才算出戏
from .oocheck import OOC_RE as _OOC_RE, ooc_words  # noqa: E402,F401
from .echocheck import echo_hint  # noqa: E402


# 模型偶尔把提示里的说明抄进回复，比如“（对方和你不太熟，礼貌打个招呼就行。）你好。”：这种括号整段去掉
# 括号里写的纯动作、舞台说明（9/29 23:50）：“（过了两秒）”“（停顿了一下）”“（打了个哈欠）”“（不接话，甩一张……）”。
# 括号心声（“（这人还真敢说啊。）”“（悄悄心动了一下）”）是她的说话方式，不动：只去掉以动作开头、不带“我 / 你”的短括号
_ACTION_PAREN_RE = re.compile(r"[（(][…\s]*(?:过了[一两三几半\d]*[秒分]|停顿|顿了|沉默|打了?个?哈欠|叹了?口?气|耸了?耸?肩|别开[眼脸视头]|"
                              r"扭过头|转过[头身]|笑了笑|不接话|甩了?一张|挑了?挑?眉|眨了?眨?眼|揉了?揉?眼)[^（）()我你]{0,14}[）)]")
_META_PAREN_RE = re.compile(r"[（(][^（）()]{0,60}(?:对方|这轮|回复|提示|系统|规则|人设)[^（）()]{0,60}[）)]")


def unprompted_hits(reply: str, context: str) -> list[str]:
    """回复里有、但对方这句和最近的聊天都没提到的词（比如突然冒出来的“蘑菇”）"""
    out = []
    for group in cfg.unprompted_words:
        words = [w for w in str(group).split("|") if w]
        if any(w in context for w in words):
            continue                       # 上文提到过（比如对方说“香菇”），她说“蘑菇”也正常
        out += [w for w in words if w in reply]
    return out


def drop_sentences_with(reply: str, words: list[str]) -> str:
    parts = re.split(r"(?<=[。！？!?…~\n])", reply)
    return "".join(p for p in parts if not any(w in p for w in words)).strip()


def drop_ooc_sentences(reply: str, user_text: str) -> str:
    parts = re.split(r"(?<=[。！？!?…~\n])", reply)
    return "".join(p for p in parts if not ooc_words(p, user_text)).strip()


# ------------------------------------------------------------------ 表情包
# 每轮先由程序抽签决定“这轮能不能带表情”；抽中了才在提示里告诉她可以写 [表情:情绪]，程序截掉标记、按情绪挑一张发出去
_STICKER_RE = re.compile(r"[\[【［]\s*(?:发了|甩了)?\s*表情\s*[:：]\s*([^\]】］（(]{1,8})[^\]】］]*[\]】］]")
TIER_EMOTIONS = {
    "disliked": {"嫌弃", "无语", "敷衍"},
    "stranger": set(EMOTIONS) - {"害羞", "委屈"},
    "friend": set(EMOTIONS) - {"委屈"},
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
    if quota_left(group_id if is_group else None, None if is_group else int(target.split("_")[1])) < 2:     # 限额只剩最后一条了：留给文字
        return False, allowed, emotions
    p = cfg.sticker_self_react_prob if self_image else cfg.sticker_prob * tier_value(cfg.sticker_tier_multiplier, fam, 1.0)
    return random.random() < p, allowed, emotions


def sticker_hint(emotions: list[str], only_ok: bool) -> str:
    h = ("【表情】这一轮你可以在回复最后加一个表情，也就是甩一张自己的小画像，格式是「[表情:情绪]」，"
         f"情绪只能从这些里选：{'、'.join(emotions)}。只在这句话确实带着这种情绪时才用，觉得不合适就不加。")
    if only_ok:
        h += "这轮也可以一个字都不说，整条回复只写一个「[表情:情绪]」，就当甩了张画像过去。"
    return h


# 模型模仿别人消息的格式，写出「[图片：表情]」「[表情]」「[动画表情]」之类：这些发出去就是一串文字，要去掉。
# 看得出情绪的（比如「[图片：嫌弃的表情]」）当成想甩表情；看不出的直接删掉
_FAKE_MEDIA_RE = re.compile(
    r"[\[【［]\s*(图片|动画表情|表情包|表情|小画像|画像|语音|视频|图)\s*(?:[:：]\s*([^\]】］]{0,40}))?\s*[\]】］]")


def split_sticker(reply: str) -> tuple[str, str | None]:
    """截掉回复里的表情标记，返回（文字, 情绪）；有好几个就用最后一个"""
    found = _STICKER_RE.findall(reply)
    text = _STICKER_RE.sub("", reply)
    fakes = _FAKE_MEDIA_RE.findall(text)
    if fakes:
        text = _FAKE_MEDIA_RE.sub("", text)
        logger.info(f"回复里写了图片/表情的占位符，已去掉：{fakes}")
        if not found:
            for kind, inner in reversed(fakes):
                if kind in ("语音", "视频") or not inner:
                    continue
                e = next((w for w in re.split(r"[、，,的\s]+", inner) if w and sticker_normalize(w)), None)
                if e:
                    found = [sticker_normalize(e)]
                    break
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text, (found[-1].strip() if found else None)


# 9/30 00:05：几档里原来都给了现成的例句（“……别突然说这种话。”“我打飞你哦。”“……我们才刚认识吧。”“谢、谢谢……”），
# 这段每轮都加在最后、离她要说的话最近，测试里被原样照抄（很熟的人告白 10 次里 8 次同一句）。现在只写态度，
# 讨厌的人那档本来就是敷衍的短句，留着。00:55 又去掉陌生人档的“结巴”（被当成“谢、谢谢……”）和“点明不熟”（被当成“还没熟到说这种话吧”）
FAMILIARITY_HINT = {
    "disliked": "（对方是你讨厌的人——之前骂过你、骚扰过你或一直惹你烦：明显不耐烦、爱答不理，回得极短，"
                "比如“哦。”“有事？”“……”“你还敢来？”。对方讨好你也不会马上改观，除非他真心道歉。）",
    "stranger": "（对方和你不太熟：客气、有分寸，话少一点、保持距离——像旅途中对初次见面的人那样用敬语、礼貌，但不热络、不主动关心、不说亲昵的话。"
                "对方正常说话、打招呼、问问题，就好好回，语气可以淡，但不要凶、不要反问“你谁啊”、不要随便说“蛤？”。"
                "对方说到你好奇的事可以问一句；被当面夸可爱会有点不好意思，反应每次不一样，不要回“我知道”；"
                "对方胡搅蛮缠、说荒唐话时，可以礼貌地损一句，但不骂人。"
                "只有对方越界（一上来就告白、调情、叫你宝宝老婆、说过分亲昵的话）时，才冷下来拒绝，说法每次不同；"
                "被骂、被恶意冒犯才毒舌回去。这些情况每次的说法都不一样，别用同一句。）",
    "friend": "（对方是和你聊过一些、印象还不错的人：不用那么客气了，语气自然些，偶尔可以吐槽一句、开个小玩笑，"
              "但还谈不上熟：不嘘寒问暖、不说亲昵的话，也不会主动问对方的私事。"
              "对方告白、调情时，冷淡地挡回去，说法每次不一样。）",
    "acquaintance": "（对方是和你说过不少话的熟人：可以随意些，偶尔挖苦对方一下，损得轻、点到为止，不揭短；但保持距离感，不黏人、不嘘寒问暖。"
                    "对方告白、调情、说些没分寸的话时，嫌弃地挡回去：嘴上威胁一句、反过来挖苦，或者干脆不接话，每次换一种。）",
    "close": "（对方是你很熟、信任的人：可以放松些，损人照样损，损里带点在意，偶尔流露关心，但嘴上不承认。"
             "对方告白时会慌一下、别扭地岔开，或者嘴硬地挖苦一句，但依旧不会答应；每次的反应都不一样。）",
}


def familiarity_of(qq: int) -> str:
    return ltm.familiarity(qq, cfg.close_friends)


def tier_value(table: dict, fam: str, default: float = 1.0) -> float:
    """按关系取配置里的数。9/29 新加了“普通朋友”，.env 里老的写法没有 friend：取陌生人和熟人的中间值"""
    if fam in table:
        return table[fam]
    if fam == "friend" and "stranger" in table and "acquaintance" in table:
        return (float(table["stranger"]) + float(table["acquaintance"])) / 2
    return default


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
    rf"|(?:给你|送你|塞给你|发给你|赏你)\s*(?:(?:发|包)了?\s*)?(?:个|一个)?\s*(?:QQ|微信|支付宝)?红包"
    rf"|(?:给你|跟你)\s*转(?:个)?账|转账给你|转给你|转你|(?:给你|跟你)\s*打钱|打钱给你"
    rf"|[\[【［]\s*(?:QQ)?红包[^\]】］]{{0,6}}[\]】］]|[\[【［][^\]】］]{{0,6}}{_FOREIGN}[^\]】］]{{0,6}}[\]】］]",
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
_private_hour: dict[int, deque] = defaultdict(deque)   # QQ -> 这一小时私聊发给这个人的条数（各人分开算）
_farewell_at: dict[str, float] = {}      # 限额范围 -> 上次说“我要上路了”的时间
_active_chats: dict[str, dict] = {}      # 会话（group_群号 / private_QQ）-> 她最近一次在这里说话的时间和对象


def _scope(group_id: int | None, user_id: int | None) -> "tuple[deque, int] | tuple[None, None]":
    """这条回复算在哪个范围的额度里：群聊按群，私聊按人；都没有（比如空间评论）就只算全局"""
    if group_id:
        return _group_hour[group_id], cfg.group_rate_per_hour
    if user_id:
        return _private_hour[user_id], cfg.private_rate_per_hour
    return None, None


_NO_LIMIT = 10 ** 6


def _rounds(money: float) -> int:
    """剩下的钱大约还够回几轮"""
    if money == float("inf"):
        return _NO_LIMIT
    return int(money / max(cfg.budget_round_estimate, 1e-6)) if money > 0 else 0


def quota_left(group_id: int | None, user_id: int | None = None) -> int:
    """还能回几轮：今天剩下的钱（和这个群 / 这个人私聊的份额）大约够几轮，再和每小时条数限额（开了的话）取小的"""
    if asleep():
        return 0                                          # 休息时间
    now = time.monotonic()
    left = _rounds(spend.chat_left())
    if cfg.global_rate_per_hour > 0:
        left = min(left, cfg.global_rate_per_hour - sum(1 for t in _hour_window if now - t <= 3600))
    q, limit = _scope(group_id, user_id)
    if q is not None and limit > 0:
        left = min(left, limit - sum(1 for t in q if now - t <= 3600))
    if group_id:
        left = min(left, _rounds(spend.group_left(group_id)))
    elif user_id:
        left = min(left, _rounds(spend.user_left(user_id)))
    return left


# 额度用完的那一轮，话还没说完（她在反问对方、或者对方还有话没回）：先不告别，给这个人最多再回 2 次，
# 把话收个尾再走；10 分钟内对方没接话就算了
WRAPUP_EXTRA = 2
WRAPUP_MINUTES = 10
_wrapup: dict[str, dict] = {}       # 会话 -> {"user": 在跟谁收尾, "left": 还能回几次, "at": 开始收尾的时间}


def in_wrapup(target: str | None, user_id: int) -> bool:
    w = _wrapup.get(target) if target else None
    if not w:
        return False
    if time.time() - w["at"] > WRAPUP_MINUTES * 60:
        _wrapup.pop(target, None)
        return False
    return w["user"] == user_id and w["left"] > 0


def rate_limited(user_id: int, group_id: int | None = None, private: bool = False, target: str | None = None) -> str | None:
    """private=True：私聊，按这个人单独算每小时额度。
    target：这个会话正在“收尾”（额度用完、话还没说完）时，每小时额度用完了也再放行这个人几次"""
    now = time.monotonic()
    if now - _last_trigger.get(user_id, -1e9) < cfg.user_cooldown:
        return "cooldown"
    while _global_window and now - _global_window[0] > 60:
        _global_window.popleft()
    if len(_global_window) >= cfg.global_rate_per_minute:
        return "global"
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.popleft()
    over = None
    if asleep():
        over = "sleep"                                # 休息时间
        _sleep_wrapup(target, user_id)
    elif spend.chat_left() <= 0:
        over = "budget"                               # 今天的钱花完了
    elif group_id and spend.group_left(group_id) <= 0:
        over = "budget_group"                         # 这个群今天的份额用完了
    elif spend.user_left(user_id) <= 0:
        over = "budget_user"                          # 这个人今天的份额用完了（群聊私聊合计）
    elif cfg.global_rate_per_hour > 0 and len(_hour_window) >= cfg.global_rate_per_hour:
        over = "hourly"
    gq, limit = _scope(group_id, user_id if private else None)
    if gq is not None:
        while gq and now - gq[0] > 3600:
            gq.popleft()
        if not over and limit > 0 and len(gq) >= limit:
            over = "group_hourly" if group_id else "private_hourly"
    if over:
        if not in_wrapup(target, user_id):
            return over
        _wrapup[target]["left"] -= 1          # 收尾：超出额度也再回这一次
        logger.info(f"额度已经用完（{over}），给 {target} 收个尾（还能回 {_wrapup[target]['left']} 次）")
    _last_trigger[user_id] = now
    _global_window.append(now)
    _hour_window.append(now)
    if gq is not None:
        gq.append(now)
    return None


def _refund(stamp: float, group_id: int | None, user_id: int | None = None) -> None:
    """占了额度最后却没说话（模型出错、她不想接、出戏句子删光了、没发出去）：把这一条退回去"""
    for dq in (_hour_window, _global_window, _scope(group_id, user_id)[0]):
        if dq is None:
            continue
        try:
            dq.remove(stamp)
        except ValueError:
            pass


def _has_quota(gid: int | None, user_id: int | None = None) -> bool:
    """还有没有额度（只看，不占）"""
    now = time.monotonic()
    if sum(1 for t in _global_window if now - t <= 60) >= cfg.global_rate_per_minute:
        return False
    return quota_left(gid, user_id) > 0


def _count_sent(group_id: int | None, user_id: int | None = None) -> None:
    """多发了一条（分条的后几条、表情、“在忙”、告别）：算进每小时的额度（全局 + 这个群 / 这个人的私聊）"""
    now = time.monotonic()
    _hour_window.append(now)
    q = _scope(group_id, user_id)[0]
    if q is not None:
        q.append(now)


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


# 她自己说了“我要去赶路了”“先走了”：接下来 20～60 分钟真的不在这个群 / 私聊（不然说完要走还接着聊，很怪）
_LEAVE_RE = re.compile(r"(我?(要|得|该|先)(去)?赶路了|赶路去了|先走了|先失陪|我?得走了|我走了|该走了|有缘再见|下次再聊|先不聊了|我先撤|"
                       r"我?(先)?去睡了|我先睡了|先下了|改天再聊)")
_away: dict[str, dict] = {}              # 会话 -> {"until": 回来的时间, "said": 说要走的时间, "line": 那句话}


def away_left(target: str) -> float:
    """她说过要走、还要几秒才回来（0 = 在）"""
    a = _away.get(target)
    return max(0.0, a["until"] - time.time()) if a else 0.0


def note_leaving(target: str, said: str) -> bool:
    """她这轮说的话里有“要走了”：记下来，接下来一阵子不在"""
    if cfg.leave_minutes_max <= 0 or not said or not _LEAVE_RE.search(said):
        return False
    lo, hi = sorted((cfg.leave_minutes_min, cfg.leave_minutes_max))
    now = time.time()
    _away[target] = {"until": now + random.uniform(lo, hi) * 60, "said": now, "line": said.split("\n")[-1]}
    logger.info(f"她说要走了（{said[-20:]}）：{target} 接下来 {(_away[target]['until'] - now) / 60:.0f} 分钟不回")
    return True


def _target_of(event: MessageEvent) -> str:
    return f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}"


async def switch_pause(target: str) -> None:
    """刚回完别人（别的群 / 别的私聊）又要回这边：像切换聊天窗口一样停一下"""
    if _last_sent["target"] not in (None, target) and time.monotonic() - _last_sent["at"] < 30:
        await asyncio.sleep(random.uniform(cfg.switch_gap_min, cfg.switch_gap_max))


def mark_sent(target: str) -> None:
    _last_sent["target"], _last_sent["at"] = target, time.monotonic()
    _chat_seq[target] += 1              # 她在这里说了话：之后再回更早的消息，就要引用原句


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
# 所有指令都只有管理员能用；别人发指令她当没看见（不回、不当成聊天）
ADMIN_COMMANDS = ("重置", "清空记忆", "reset", "重载人设", "认图", "表情", "表情包", "记忆", "查看记忆",
                  "好感", "好感度", "性别", "忘记", "删除记忆", "写信", "说说", "插话", "冒泡", "搭话", "花费", "花销")


def is_admin_command(text: str, command_start) -> bool:
    t = text.strip()
    for start in command_start or {"/"}:
        if start and t.startswith(start):
            name = t[len(start):].lstrip()
            # 只认“正好是指令”或“指令后面跟空格”：“/表情真可爱”不算指令
            return any(name == c or name.startswith(c + " ") for c in ADMIN_COMMANDS)
    return False


reset_cmd = on_command("重置", aliases={"清空记忆", "reset"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


_BULK_RESET = {"所有私聊": "private_", "全部私聊": "private_", "私聊": "private_",
               "所有群聊": "group_", "全部群聊": "group_", "群聊": "group_"}
_bulk_pending: dict[tuple[int, str], float] = {}     # (管理员QQ, 前缀) -> 第一次发的时间（等确认）


def _session_keys(prefix: str) -> list[str]:
    keys = {f.stem for f in HISTORY_DIR.glob(f"{prefix}*.json")}
    keys |= {k for k, v in _histories.items() if k.startswith(prefix) and v}
    return sorted(keys)


def clear_all(prefix: str) -> int:
    """清空所有私聊 / 所有群聊的短期记忆（连同还没整理进长期记忆的旧消息、旁听暂存）；返回清了几个"""
    keys = _session_keys(prefix)
    for k in keys:
        clear_history(k)
    if prefix == "group_":
        _passive.clear()
        _recent_chat.clear()
    return len(keys)


@reset_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    """/重置：清空当前这个群 / 这个私聊的短期记忆；/重置 QQ号（或 @某人）：清空和这个人的私聊记忆；
    /重置 所有私聊、/重置 所有群聊：全部清空（30 秒内再发一次确认）"""
    word = arg.extract_plain_text().strip()
    prefix = _BULK_RESET.get(word)
    if prefix:
        what = "私聊" if prefix == "private_" else "群聊"
        pk, now = (event.user_id, prefix), time.monotonic()
        if now - _bulk_pending.get(pk, -1e9) > 30:
            n = len(_session_keys(prefix))
            _bulk_pending[pk] = now
            await reset_cmd.finish(f"（将清空所有{what}的短期记忆，共 {n} 个{what}，清了找不回来。30 秒内再发一次“/重置 {word}”确认）")
        _bulk_pending.pop(pk, None)
        n = clear_all(prefix)
        logger.info(f"管理员 {event.user_id} 清空了所有{what}的短期记忆（{n} 个）")
        await reset_cmd.finish(f"（已清空所有{what}的短期记忆，共 {n} 个）")
    target = _parse_target(event, arg)
    if target and target[0] == "user":
        clear_history(f"private_{target[1]}")
        await reset_cmd.finish(f"（和 {target[1]} 的私聊记忆已清空）")
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
    """/好感 @某人 查看；/好感 @某人 +10 / -10 / =50 / =-20 调整（也可以用 QQ号 或 我）"""
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
        await aff_cmd.finish(f"用法：/好感 @某人（或 QQ号、我）查看；后面加 +10、-10 或 =50 调整（分数 {cfg.affection_min}～{cfg.affection_max}：讨厌 <{cfg.affection_dislike}｜普通朋友 ≥{cfg.affection_friend}｜熟人 ≥{cfg.affection_acquaintance}｜很熟 ≥{cfg.affection_close}）")
    m = re.match(r"\s*([+\-=])\s*(-?\d+(?:\.\d+)?)", rest)
    if m and (m.group(1) == "=" or not m.group(2).startswith("-")):
        op, num = m.group(1), float(m.group(2))
        if op == "=":
            ltm.adjust(qq, set_to=num)
        else:
            ltm.adjust(qq, delta=num if op == "+" else -num)
    await aff_cmd.finish(ltm.describe_affection(qq, cfg.close_friends))


cost_cmd = on_command("花费", aliases={"花销"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@cost_cmd.handle()
async def _():
    """/花费：今天（从凌晨 BUDGET_RESET_HOUR 点算起）花了多少、花在哪、谁花得最多，本月合计"""
    names = {}
    d = spend.today()
    for q in list(d["users"])[:50]:
        with contextlib.suppress(Exception):
            names[int(q)] = ltm.get_user(int(q)).get("name") or q
    text = spend.report(names)
    if spend.enabled and spend.chat_left() <= 0:
        text += f"\n今天的钱已经花完了，{spend.next_reset():%H:%M} 以后恢复"
    await cost_cmd.finish(text)


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


# ------------------------------------------------------------------ 消息计数（决定要不要用“回复”形式引用原句）
# 每个会话（群 / 私聊）里：收到一条消息 +1，她自己发完一轮也 +1。
# 她回某条消息时比较一下：这条消息到了以后，中间有别人插话、或者她自己先发了别的话（比如还在回上一个人），
# 就引用原句回复，免得大家不知道她在回哪句；否则直接发
_chat_seq: dict[str, int] = defaultdict(int)
_arrival_seq: dict[tuple[str, int], int] = {}      # (会话, 消息 id) -> 这条消息到的时候的计数


def note_arrival(target: str, message_id: int) -> None:
    _chat_seq[target] += 1
    _arrival_seq[(target, int(message_id))] = _chat_seq[target]
    if len(_arrival_seq) > 2000:                    # 只留最近的
        for k in list(_arrival_seq)[:1000]:
            del _arrival_seq[k]


async def _is_group(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


counter = on_message(rule=Rule(_is_group), priority=1, block=False)


_speakers: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))   # 群号 -> 最近说话的人（时间, QQ）
_replied_at: dict[tuple[int, int], float] = {}                        # (群号, QQ) -> 她最后一次回这个人的时间


def others_spoke_since(gid: int, uid: int, since: float) -> bool:
    return any(t > since and u != uid for t, u in _speakers.get(gid, []))


@counter.handle()
async def _(event: GroupMessageEvent):
    note_arrival(f"group_{event.group_id}", event.message_id)
    _speakers[event.group_id].append((time.monotonic(), event.user_id))
    ltm.note_name(event.group_id, event.user_id, sender_label(event))


# ------------------------------------------------------------------ 旁听（没 @ 的群消息）
passive = on_message(rule=Rule(_is_group_not_to_me) & allowed, priority=99, block=False)


@passive.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    text = message_to_text(event.get_message())
    if not text or text.startswith(tuple(bot.config.command_start or {"/"})) or is_bot_like(event):
        return
    body = message_to_text(event.get_message(), drop_at=True) or "（只@了一下）"
    line = speaker_head(event) + clean_body(clip_input(body))
    if cfg.passive_buffer > 0:
        _passive[event.group_id].append((event.user_id, sender_label(event), line, time.time()))
    _recent_chat[event.group_id].append((time.monotonic(), event.user_id, sender_label(event), line))
    if cfg.interject_enabled:
        await maybe_interject(bot, event.group_id, text)


# ------------------------------------------------------------------ 没 @ 也回复：判断是不是在跟她说话
_last_bot_msg: dict[int, tuple[float, int, str]] = {}   # 群号 -> (时间, 她回复的对象 QQ, 她说的话)
_last_smart: dict[int, float] = {}
_engaged: dict[tuple[int, int], float] = {}   # (群号, QQ) -> 这个人最近一次在跟她说话的时间

JUDGE_PROMPT = """判断群聊里的一条新消息，是不是在跟“伊蕾娜”说话。伊蕾娜是群里的角色扮演机器人，一位旅行魔女。

伊蕾娜最近在群里说的话：{last}
最近的群聊：
{recent}
新消息：{line}

格式说明：每条是「【说话人 → 对象】正文」。“→ 伊蕾娜”是在跟她说；“→ 别的名字”是在跟那个人说（名字后面标“（群友）”的是和她同名的群友，不是她；带“#2”“#3”的是和别人重名的另一位群友）；没有“→”是随口说给大家的。

如果新消息是在叫她、问她、对她说话、回应她刚才的话，或者在谈论她并明显希望她回应，回答“是”。
刚和她聊着的人接着用“你”问她问题，就是在问她，回答“是”。但群里人多时，“你”常常是在说别人，要看上下文。
替别人转述、催她回答（比如“他问你……呢”“他在问你话”），也是在跟她说话，回答“是”。
如果是群友之间在聊天，只是顺带提到这个名字（比如在聊动画、小说里的角色），不需要她回应，回答“否”。
如果是在跟别人说话、用“她”“这个机器人”之类第三人称谈论她（比如向别人介绍她、跟别人讨论她好不好用），回答“否”。
新消息是“→ 别人”的，就是在跟那个人说话，回答“否”；拿不准的时候也回答“否”。
只输出一个字：是 或 否。"""


_Q_TAILS = ("吗", "呢", "是吧", "对吧", "不是吧", "什么", "怎么", "为啥", "干嘛", "干啥", "咋样", "咋办")


def _looks_like_question(text: str) -> bool:
    # 9/28：结尾“吧”“嘛”不再一律算问句（“好吧”“行吧”“算了吧”是应一声，不是在问）；只认“是吧”“对吧”这种
    t = text.rstrip(" ~～。.!！…")
    return any(c in text for c in "?？") or t.endswith(_Q_TAILS)


def _as_third_person(text: str) -> str:
    """判断时站在旁观者角度：记录里的“→ 你”写成“→ 伊蕾娜”"""
    return re.sub(r"(→ (?:[^】]*、)?)你(?=[、】])", r"\1伊蕾娜", text)


async def _judge(event: GroupMessageEvent, line: str) -> bool:
    last = _last_bot_msg.get(event.group_id)
    # 最近的群聊：她自己的记忆里最近几条（含她说的话）+ 还没进记忆的旁听
    lines = []
    for h in get_history(f"group_{event.group_id}")[-4:]:
        c = h["content"].replace("（群聊旁听记录）\n", "")
        lines.append(f"【伊蕾娜】{c}" if h["role"] == "assistant" else _as_third_person(c))
    lines += [_as_third_person(ln) for _, _, ln, _ in list(_passive.get(event.group_id, []))[-4:]]
    recent = "\n".join(x[:120] for x in lines[-6:]) or "（无）"
    prompt = JUDGE_PROMPT.format(last=last[2][:100] if last else "（最近没说话）", recent=recent, line=line[:220])
    try:
        r = await client.chat.completions.create(
            model=cfg.deepseek_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2,
            extra_body={"thinking": {"type": "disabled"}},
        )
        budget.track(r, "judge", user=event.user_id, group=event.group_id)
        ok = (r.choices[0].message.content or "").strip().startswith("是")
        if not ok:
            logger.info(f"判断：不是在跟她说话 群{event.group_id} {line[:40]}")
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning(f"判断是否在跟她说话失败，按“否”处理：{e}")
        return False


async def _addressed(bot: Bot, event: MessageEvent) -> bool:
    """@ 她 / 回复她 / 以昵称开头 → 一定回；否则在群里看是否明显在跟她说话"""
    now = time.monotonic()
    # 不是管理员却发了指令：不回，也不当成聊天
    if is_admin_command(message_to_text(event.get_message()), bot.config.command_start) \
            and str(event.user_id) not in bot.config.superusers:
        logger.info(f"不是管理员，指令不理 user={event.user_id}：{message_to_text(event.get_message())[:20]}")
        return False
    if is_bot_like(event):                 # 别的机器人（Markdown、卡片消息）：不理，免得两个机器人对着聊
        return False
    if isinstance(event, PrivateMessageEvent) and is_friend_verify(event.user_id, message_to_text(event.get_message()), event.time):
        logger.info(f"加好友的验证消息 / 系统提示，不回 user={event.user_id}：{message_to_text(event.get_message())[:30]}")
        return False
    if quiet_left() > 0:                   # 刚被踢下线又上线：先安静一会儿（私聊之后会补回）
        if event.is_tome():
            logger.info(f"刚重新上线，还要安静 {quiet_left() / 60:.1f} 分钟，先不回 user={event.user_id}")
        return False
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
    names = [n for n in list(cfg.smart_names) + list(bot.config.nickname or []) if n]
    plain = event.get_plaintext()          # 只看文字：@别人的名字、回复的原文都不算
    mentioned = any(n.lower() in plain.lower() for n in names)   # 不区分大小写：ELAINA 也算
    # @ 了别人、或者回复的是别人的消息：在跟那个人说话，她不接（除非文字里点了她的名）
    other = talking_to_others(event)
    if other and not mentioned:
        logger.info(f"在跟别人说话（{other}），不接：群{gid} {sender_name(event)}：{text[:30]}")
        return False
    # 连续发言：她刚回过的人接着说。叫了她名字，或者这期间没别人插话（一对一在聊），才直接算在跟她说；
    # 这期间有别人说过话时，就算带“你”、是问句，也可能是对别人说的，交给下面判断（9/28 收紧）
    in_followup = now - _engaged.get((gid, event.user_id), -1e9) <= cfg.followup_window
    if in_followup:
        since = _replied_at.get((gid, event.user_id), _engaged.get((gid, event.user_id), now))
        if mentioned or not others_spoke_since(gid, event.user_id, since):
            _engaged[(gid, event.user_id)] = now
            logger.info(f"接着说，直接回：群{gid} {sender_name(event)}：{text[:30]}")
            return True
    if len(text) < 2 or in_peak():       # 高峰时段她很忙，不主动接话（也省掉判断的 token）
        return False
    last = _last_bot_msg.get(gid)
    recent_talk = bool(last) and now - last[0] <= cfg.smart_window
    continuing = in_followup or (recent_talk and (event.user_id == last[1] or "你" in plain or _looks_like_question(plain)))
    # 正在跟她聊的人（她刚回过的人，或者接着用“你”跟她说话）：不受“没 @ 也回复”间隔的限制
    in_conversation = in_followup or (recent_talk and (event.user_id == last[1] or "你" in plain))
    if not in_conversation and now - _last_smart.get(gid, -1e9) < cfg.smart_min_interval:
        if mentioned or continuing:
            logger.info(f"没 @ 的消息，离上次插话不到 {cfg.smart_min_interval:g} 秒，先不接：群{gid} {sender_name(event)}：{text[:30]}")
        return False
    if not (mentioned or continuing):
        return False                     # 大部分群消息在这里就结束了，不花 token
    line = speaker_head(event, you="伊蕾娜") + clean_body(message_to_text(event.get_message(), drop_at=True))
    if asleep():
        _sleep_wrapup(f"group_{gid}", event.user_id)     # 刚到休息时间、正在跟她聊的人接着说：先收个尾
    if cfg.smart_judge and quota_left(gid, event.user_id) <= 0 and not in_wrapup(f"group_{gid}", event.user_id):
        return False                     # 今天的钱花完了（或这个群、这个人的份额用完了）：反正不会回，也不用判断
    ok = await _judge(event, line) if cfg.smart_judge else mentioned
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
_look_later: dict[tuple[str, int], list] = defaultdict(list)   # (会话, QQ) -> [(在收件箱里的位置, 消息事件)]：有图，要回的时候再看


def has_images(event: MessageEvent) -> bool:
    """这条消息（或它引用的消息）里有没有图片"""
    if any(seg.type == "image" for seg in event.get_message()):
        return True
    reply = getattr(event, "reply", None)
    return reply is not None and any(seg.type == "image" for seg in reply.message)


def _take_inbox(ik: tuple[str, int]) -> list[str]:
    _look_later.pop(ik, None)
    return _inbox.pop(ik, [])


async def look_at_images(ik: tuple[str, int], drop_at: bool) -> None:
    """确定要回了：把收件箱里带图的几条换成看过图的说法（调模型看图）"""
    todo = _look_later.pop(ik, [])
    box = _inbox.get(ik)
    for pos, ev in todo:
        if box is None or pos >= len(box):
            continue
        try:
            box[pos] = await rich_text(ev, look=True, drop_at=drop_at) or box[pos]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"看图失败，按没看处理：{e}")
_reply_started: dict[tuple[str, int], float] = {}  # (会话, QQ) -> 她开始回这个人的时间
_reply_done: dict[tuple[str, int], float] = {}     # (会话, QQ) -> 她回完这个人的时间


def is_sticker_only(event: MessageEvent) -> bool:
    """这条消息只有表情（QQ 表情、商城表情、表情包图片），没有文字"""
    segs = [x for x in event.get_message()
            if x.type != "at" and not (x.type == "text" and not x.data.get("text", "").strip())]
    def sticker(x) -> bool:
        if x.type in ("face", "mface"):
            return True
        return x.type == "image" and (str(x.data.get("sub_type")) == "1" or "表情" in str(x.data.get("summary", "")))
    return bool(segs) and all(sticker(x) for x in segs)


def sticker_follows_reply(ik: tuple[str, int]) -> bool:
    """她正在回这个人，或者刚回完：这时单独来的表情，算对方上一条消息的一部分"""
    now = time.monotonic()
    started, done = _reply_started.get(ik, -1e9), _reply_done.get(ik, -1e9)
    replying = started > done and now - started < 120
    return replying or now - done <= cfg.sticker_follow_seconds

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


async def wait_for_more(ik: tuple[str, int], text: str, names: list[str] | tuple = (),
                        look: "MessageEvent | None" = None) -> int | None:
    """放进收件箱等一会儿（按话说完没说完决定等多久）；期间同一个人又发了新消息，返回 None（让最新那条来回复）。
    look：这条有图，确定要回的时候再看"""
    global _token_counter
    now = time.monotonic()
    if not _inbox[ik]:
        _inbox_first[ik] = now
        _look_later.pop(ik, None)
    if look is not None:
        _look_later[ik].append((len(_inbox[ik]), look))
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
    ik = (session_key(event), event.user_id)
    budget.actor.set((event.user_id, event.group_id if isinstance(event, GroupMessageEvent) else None))   # 看图这些花的钱也算到这个人 / 这个群头上
    try:
        await _converse(bot, event, catchup_age)
    finally:
        # 不管是回完了，还是中途不回（出错、她觉得没必要接话、已经告别……），都记下“这一轮结束了”
        if _reply_started.get(ik, -1e9) > _reply_done.get(ik, -1e9):
            _reply_done[ik] = time.monotonic()


async def _converse(bot: Bot, event: MessageEvent, catchup_age: float | None = None) -> None:
    global _token_counter
    # 先不调模型看图（本机角色识别照常，免费）：等确定要回了再看，被合并、限流、跳过的消息就不花这个钱
    drop_at = isinstance(event, GroupMessageEvent)
    text = await rich_text(event, look=False, drop_at=drop_at)
    look_later = has_images(event) and cfg.vision_enabled and (cfg.vision_in_peak or not in_peak())
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
    # 消息发完接一个表情：她已经在回（或者刚回完）上一条了，这个表情算同一条，不单独回
    if (catchup_age is None and cfg.sticker_follow_seconds > 0 and is_sticker_only(event)
            and not _inbox.get(ik) and sticker_follows_reply(ik)):
        async with _locks[key]:
            history = get_history(key)
            name = sender_label(event)
            history.append({"role": "user", "content": speaker_head(event) + clean_body(text) if is_group else text,
                            "uid": event.user_id, "name": name, "ts": time.time()})
            save_history(key)
        logger.info(f"跟在消息后面的表情，算同一条，不单独回 user={event.user_id}")
        return
    if catchup_age is None:
        names = list(cfg.smart_names) + list(getattr(bot.config, "nickname", None) or [])
        token = await wait_for_more(ik, text, names, event if look_later else None)
        if token is None:
            return
        if isinstance(event, PrivateMessageEvent) and len(_inbox.get(ik, [])) == 1 \
                and is_friend_verify(event.user_id, text, event.time):
            # “我是某某”这种验证消息先到、“加好友成功”的通知后到：等过这几秒再看一次
            _take_inbox(ik)
            logger.info(f"加好友的验证消息，不回 user={event.user_id}：{text[:30]}")
            return
    else:                                 # 补回未读：几条已经合在一起了，不用再等
        if not _inbox[ik]:
            _look_later.pop(ik, None)
        if look_later:
            _look_later[ik].append((len(_inbox[ik]), event))
        _inbox[ik].append(text)
        _token_counter += 1
        token = _inbox_token[ik] = _token_counter

    # 她刚说过“我要去赶路了”：这段时间真的不在，对方的话记下来，回来以后再说
    target = _target_of(event)
    if catchup_age is None and away_left(target) > 0:
        texts = _take_inbox(ik)
        if texts:
            async with _locks[key]:
                history = get_history(key)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
                                "uid": event.user_id, "name": sender_label(event), "ts": time.time()})
                save_history(key)
        logger.info(f"她说过要走了，还有 {away_left(target) / 60:.0f} 分钟才回来，先不回 {target} user={event.user_id}")
        return

    # 水话（“哈哈”“嗯”“好的”、一个表情）：按关系远近，有一定概率直接不接话（不调用模型）
    pending = _inbox.get(ik, [])
    if cfg.skip_filler and pending and all(is_filler(t) for t in pending):
        async with _locks[key]:
            history = get_history(key)
            if not she_asked(history) and random.random() < tier_value(cfg.skip_filler_prob, familiarity_of(event.user_id), 0.5):
                texts = _take_inbox(ik)
                name = sender_label(event)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
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
            texts = _take_inbox(ik)
            if recent_busy or not texts:
                logger.info(f"高峰时段，忙着没回 user={event.user_id}")
                return
            if quota_left(event.group_id if is_group else None, None if is_group else event.user_id) <= 0:     # “在忙”也算一条，额度用完就不回了
                logger.info(f"高峰时段，额度用完了，“在忙”也不回 user={event.user_id}")
                return
            _peak_last_busy[event.user_id] = now
            _count_sent(event.group_id if is_group else None, None if is_group else event.user_id)
            line = peak.busy_line()
            async with _locks[key]:
                history = get_history(key)
                name = sender_label(event)
                joined = clip_input("\n".join(texts))
                history.append({"role": "user", "content": speaker_head(event) + clean_body(joined) if is_group else joined,
                                "uid": event.user_id, "name": name, "ts": time.time()})
                history.append({"role": "assistant", "content": line, "ts": time.time()})
                save_history(key)
            await asyncio.sleep(random.uniform(cfg.peak_extra_delay_min, cfg.peak_extra_delay_max))
            async with _hands:
                await switch_pause(_target_of(event))
                try:
                    await bot.send(event, line)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"“在忙”没发出去：{e}")
                    return
                mark_sent(_target_of(event))
            return
        _peak_last_real[event.user_id] = now

    # 被讨厌的人：有一定概率直接不理（不花 token）
    if familiarity_of(event.user_id) == "disliked" and random.random() < cfg.dislike_ignore_prob:
        _take_inbox(ik)
        logger.info(f"讨厌的人，不想理 user={event.user_id}")
        return

    limited = rate_limited(event.user_id, event.group_id if is_group else None, private=not is_group, target=_target_of(event))
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
        limited = rate_limited(event.user_id, event.group_id if is_group else None, private=not is_group, target=_target_of(event))
    if limited:
        logger.info(f"限流跳过 user={event.user_id} reason={limited}")
        _take_inbox(ik)
        if limited.startswith("budget") and not is_group:
            await say_tired(bot, event)
        return
    admitted_at = time.monotonic()           # 这条从这时开始算“要回”；之后如果已经告别了，就不发了
    stamp = _hour_window[-1] if _hour_window else admitted_at      # 这次占用的额度（最后没发出去就退回）
    gid_q = event.group_id if is_group else None
    uid_q = None if is_group else event.user_id
    if _look_later.get(ik):
        await look_at_images(ik, drop_at)

    async with _locks[key]:
        texts = _take_inbox(ik)
        if not texts:                     # 已经被前一条合并回复过了
            return
        _reply_started[ik] = time.monotonic()
        text = clip_input("\n".join(texts))
        # 这条消息到的时候的计数（之后有人插话、或者她先说了别的，就引用原句）
        seq_at_trigger = _arrival_seq.pop((_target_of(event), int(event.message_id)), _chat_seq[_target_of(event)])
        history = get_history(key)
        new_entries: list[dict] = []

        name = sender_label(event)
        if is_group and _passive.get(event.group_id):
            buf = list(_passive.pop(event.group_id))
            new_entries.append({
                "role": "user",
                "content": watch_block([(ts, ln) for _, _, ln, ts in buf]),
                "speakers": {n: uid for uid, n, _, _ in buf},
                "ts": time.time(),
            })

        user_content = speaker_head(event) + clean_body(text) if is_group else text
        new_entries.append({"role": "user", "content": user_content, "uid": event.user_id, "name": name, "ts": time.time()})
        # 离上一段聊天隔得久：在这一轮前面标一行“（过了 X）”
        prev_ts = next((h["ts"] for h in reversed(history) if h.get("ts")), None)
        g = gap_line(prev_ts, time.time())
        if g:
            new_entries[0]["content"] = g + new_entries[0]["content"]

        # 检索回忆：用本条消息；找不到时带上此前最近一条用户消息再试一次（应对“那后来呢？”）
        prev_user = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
        prev_bot = next((h["content"] for h in reversed(history) if h["role"] == "assistant"), "")
        # 高峰时段不检索小说（省 token）
        # 在聊她的日记 / 说说（“有今天的旅行日记吗”“然后呢”）：给她日记的真实情况，不去翻以前的旅途故事，
        # 免得把小说里某段“日记”的故事当成今天的日记讲
        diary_force = diary_followup(key, text)
        diary_memo = qzone_diary.diary_context(text, force=diary_force)
        if diary_memo and qzone_diary.asks_today_diary(text, diary_force):
            _diary_topic[key] = time.time()
            _last_recall.pop(key, None)
            memo = ""
        else:
            memo = recall(text, f"{prev_user[-60:]} {prev_bot[-120:]}", key) if cfg.knowledge_enabled and not busy else ""
        long_memo = ltm.context_for(event.user_id, name, event.group_id if is_group else None)
        mode = "busy" if busy else reply_mode(text)
        fam = familiarity_of(event.user_id)
        length_hint = length_hint_for(mode, text, fam)
        time_memo = time_hint(history, ltm.last_seen(event.user_id), fam, ltm.get_user(event.user_id).get("last_letter"))
        if in_wrapup(target, event.user_id) or (_wrapup.get(target) or {}).get("user") == event.user_id:
            time_memo = "\n".join(x for x in (time_memo, WRAPUP_SLEEP_HINT if asleep() else WRAPUP_HINT) if x)
        else:
            time_memo = "\n".join(x for x in (time_memo, winddown_hint(event.group_id if is_group else None, event.user_id)) if x)
        back = _away.pop(target, None)                # 她之前说要走、现在回来了
        if back:
            time_memo = "\n".join(x for x in (time_memo, f"【刚回来】你 {human_gap(time.time() - back['said'])}前说了要走（“{back['line'][:20]}”），现在才回来。"
                                                            "可以自然地带一句刚回来，不要装作没说过要走；这段时间对方发的话在上面。") if x)
        gender_memo = gender_step(event.user_id, text)
        gifts = detect_gifts(text)
        bread_first = any(k == "bread" for k, _ in gifts) and not ltm.bread_given_today(event.user_id)
        gift_memo = "\n".join(gift_hint(k, snip[:20], bread_first, fam) for k, snip in gifts)
        late_memo = ""
        if catchup_age is not None:
            late_memo = f"【刚看到】对方这几条消息是你不在的时候发的，最早一条已经是 {human_gap(catchup_age)}前了，你现在才看到。" + LATE_HINT
        # 对方在反问她上一句里、对方自己没提过的词（“诶？蘑菇？哪有蘑菇”）：告诉她这个词是她先说的
        echo_memo = ""
        last_a = next((i for i in range(len(history) - 1, max(-1, len(history) - 5), -1) if history[i]["role"] == "assistant"), None)
        if last_a is not None:          # 只看她最近这条回复；对方的话只看她说这句之前的（之后的可能就是在追问）
            echo_memo = echo_hint(text, history[last_a]["content"],
                                  [h["content"] for h in history[max(0, last_a - 10):last_a] if h["role"] == "user"])
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
        extra = "\n\n".join(x for x in (long_memo, memo, time_memo, gender_memo, diary_memo, late_memo, echo_memo, gift_memo, FAMILIARITY_HINT[fam] + length_hint, sticker_memo, skip_memo) if x)
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
            budget.track(resp, "chat", user=event.user_id, group=gid_q)
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
                budget.track(resp, "chat", user=event.user_id, group=gid_q)
                choice = resp.choices[0]
                reply, emotion = split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))
                if ooc_words(reply, text):
                    reply = drop_ooc_sentences(reply, text)   # 还出戏就删掉出戏的句子；删光了就不发
            wrong = _fc.check(reply) if (_fc is not None and cfg.fact_check) else []
            if wrong:
                # 把某人和一段没有他的经历拼在一起了（比如“在梦回之城遇上艾姆妮西亚”）：告诉她哪里记混了，重说一次
                logger.info(f"回复记混了（{'、'.join(f'{m.place}+{m.person}' for m in wrong)}），重新生成：{reply[:40]}")
                resp = await client.chat.completions.create(
                    model=cfg.deepseek_model,
                    messages=messages + [
                        {"role": "assistant", "content": reply},
                        {"role": "system", "content": _fc.correction(wrong)},
                    ],
                    temperature=cfg.llm_temperature,
                    max_tokens=max_tokens_for(mode),
                    extra_body={"thinking": {"type": "enabled" if cfg.llm_thinking else "disabled"}},
                )
                budget.track(resp, "chat", user=event.user_id, group=gid_q)
                choice = resp.choices[0]
                reply, emotion = split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))
                still = _fc.check(reply)
                if still:                                 # 还是混：删掉那几句；删光了就说记不清
                    logger.info(f"重说以后还是记混（{'、'.join(f'{m.place}+{m.person}' for m in still)}），删掉这几句")
                    for m in still:
                        reply = reply.replace(m.sentence, "")
                    reply = reply.strip() or "那段我记不太清了。"
            problems = await story_check(reply, memo, event.user_id, gid_q) if not busy else None
            if problems:
                # 讲的往事和资料对不上：告诉她哪里讲错了，重说一次（不再核对第二遍，省钱）
                logger.info(f"讲的往事和资料对不上（{'；'.join(problems)[:80]}），重新生成：{reply[:40]}")
                resp = await client.chat.completions.create(
                    model=cfg.deepseek_model,
                    messages=messages + [
                        {"role": "assistant", "content": reply},
                        {"role": "system", "content": STORY_FIX_PROMPT.format(problems="\n".join(f"- {x}" for x in problems))},
                    ],
                    temperature=cfg.llm_temperature,
                    max_tokens=max_tokens_for(mode),
                    extra_body={"thinking": {"type": "enabled" if cfg.llm_thinking else "disabled"}},
                )
                budget.track(resp, "chat", user=event.user_id, group=gid_q)
                choice = resp.choices[0]
                reply, emotion = split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))
                if _fc is not None and cfg.fact_check:
                    for m in _fc.check(reply):
                        reply = reply.replace(m.sentence, "")
                    reply = reply.strip() or "那段我记不太清了。"
            context = text + "\n" + "\n".join(str(h.get("content", "")) for h in history[-6:])
            odd = unprompted_hits(reply, context)
            if odd:
                # 没人提蘑菇，她自己突然冒出一句“少拿我跟蘑菇相提并论”：删掉那几句；整条都是就重说一次
                kept = drop_sentences_with(reply, odd)
                logger.info(f"回复里突然冒出没人提过的“{'、'.join(odd)}”，删掉那几句：{reply[:40]}")
                if kept:
                    reply = kept
                else:
                    resp = await client.chat.completions.create(
                        model=cfg.deepseek_model,
                        messages=messages + [
                            {"role": "assistant", "content": reply},
                            {"role": "system", "content": f"刚才的回复突然提到了「{'、'.join(odd)}」，可对方根本没说起这个。请重新回复这条消息，只接对方说的话，不要道歉，不要解释。"},
                        ],
                        temperature=cfg.llm_temperature,
                        max_tokens=max_tokens_for(mode),
                        extra_body={"thinking": {"type": "enabled" if cfg.llm_thinking else "disabled"}},
                    )
                    budget.track(resp, "chat", user=event.user_id, group=gid_q)
                    choice = resp.choices[0]
                    reply, emotion = split_sticker(clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length", keep_sticker=True))
                    reply = drop_sentences_with(reply, unprompted_hits(reply, context))
        except Exception as e:  # noqa: BLE001
            # 出错时不在聊天里发任何东西；余额不足 / Key 失效私信管理员
            kind = classify_error(e)
            logger.error(f"DeepSeek 调用失败（{kind or '其他错误'}），本条不回复：{e}")
            _refund(stamp, gid_q, uid_q)
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
                _refund(stamp, gid_q, uid_q)
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
            _refund(stamp, gid_q, uid_q)
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
        hit = next((w for w in cfg.taboo_words if w in text), None)
        if hit:
            k = tier_value(cfg.taboo_tier_multiplier, tier_now, 1.0)
            if k > 0:
                ltm.taboo_penalty(event.user_id, cfg.taboo_penalty * k, cfg.taboo_daily_max * k,
                                  f"说她{hit}（{ltm.TIER_NAMES.get(tier_now, tier_now)}）")

    bubbles = split_bubbles(reply) if reply else []
    # 同一时间只给一个人打字：别人的回复要等这边发完
    async with _hands:
        # 排队期间已经说了“我要上路了”：这条就不发了（不然告别完还在说话），也从记忆里拿掉
        bye_at = max(_farewell_at.get("global", -1e9), _farewell_at.get(target, -1e9))
        if bye_at > admitted_at:
            logger.info(f"已经告别了，这条不发：{target} user={event.user_id}")
            _unrecord(key, record)
            return
        await switch_pause(target)
        delay = typing_delay(bubbles[0] if bubbles else "")
        if busy:
            delay += random.uniform(cfg.peak_extra_delay_min, cfg.peak_extra_delay_max)
        await asyncio.sleep(delay)

        sent = 0
        for i, bubble in enumerate(bubbles):
            if i > 0:
                await asyncio.sleep(bubble_gap(bubble))            # 后面几条：像在接着打字
                _count_sent(gid_q, uid_q)                          # 多发的每条都算进限额
            msg = bubble
            if i == 0 and _chat_seq[target] > seq_at_trigger:
                # 这条消息之后有别人说话了、或者她自己先说了别的（包括她排队的时候）：第一条引用原消息，免得大家不知道她在回哪句
                msg = MessageSegment.reply(event.message_id) + bubble
            try:
                await bot.send(event, msg)
                sent += 1
            except Exception as e:  # noqa: BLE001
                # 发不出去（被风控拦了、掉线了）：后面几条也别发了
                logger.warning(f"回复没发出去（第 {i + 1} 条，共 {len(bubbles)} 条）：{target} user={event.user_id}：{e}")
                break
        if bubbles and not sent:
            # 一条都没发出去：从短期记忆里拿掉，也不交给长期记忆
            _unrecord(key, record)
            _refund(stamp, gid_q, uid_q)
            return
        if sent < len(bubbles):
            sticker = None                                           # 文字没发全，表情也不发了
            _unrecord(key, record, keep="\n".join(bubbles[:sent]))
        if sticker:
            # 文字后面隔一两秒再甩表情；只发表情时，中间有人插话就引用原消息
            if bubbles:
                await asyncio.sleep(random.uniform(cfg.bubble_gap_min, cfg.bubble_gap_max))
                _count_sent(gid_q, uid_q)
            try:
                seg = stickers.segment(sticker, cfg.sticker_sub_type)
                if not bubbles and _chat_seq[target] > seq_at_trigger:
                    seg = MessageSegment.reply(event.message_id) + seg
                await bot.send(event, seg)
                _last_sticker[target] = time.monotonic()
                _recent_stickers[target].append(sticker["file"])
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"表情：发送失败（{sticker['no']} 号）：{e}")
                if not bubbles:
                    _unrecord(key, record)
                    _refund(stamp, gid_q, uid_q)
                    return
                # 文字发出去了、表情没发出去：聊天记录里只留文字，免得她以为自己甩过这张
                _unrecord(key, record, keep=reply)
        # 真的发出去了，才收下面包、交给长期记忆（攒够一批就在后台整理档案、评估好感）
        if bread_first:                               # 今天第一次送面包：收下（讨厌的人送的不加分）
            ltm.take_bread(event.user_id, 0 if tier_now == "disliked" else cfg.gift_bread_affection)
        # 长期记忆不需要知道她甩了哪张画像；只发了表情的，给一个简短的说法
        said = "\n".join(bubbles[:sent]) if bubbles else ""
        ltm.add_pending(key, new_entries + [{"role": "assistant", "content": said or f"（甩了一张{sticker['tags'][0]}的表情）"}])
        mark_sent(target)
        _reply_done[ik] = time.monotonic()
        _active_chats[target] = {"at": time.monotonic(), "group_id": event.group_id if is_group else None,
                                 "user_id": event.user_id}
        # 这一小时的限额用完了：补一句告别，让大家知道她接下来一段时间不会回（同一范围一小时只说一次）
        # 全局限额用完：最近在聊的群和私聊都告别一声；只是这个群的限额用完：只在这个群告别
        if cfg.farewell_on_limit and quota_left(gid_q, uid_q) <= 0:
            # 刚才这轮已经道别了（私聊里对方说了再见、她也回了再见；或者她自己说了要走）：这里就不再补一句告别
            said = "\n".join(bubbles)
            already_bye = bool(_LEAVE_RE.search(said)) or (not is_group and bool(_BYE_RE.search(text) or _BYE_RE.search(said)))
            # 话还没说完（她最后在反问对方、或者对方又发来了还没回的消息）：先不告别，等收个尾
            open_end = not already_bye and (she_asked([{"role": "assistant", "content": said}]) or bool(_inbox.get(ik)))
            w = _wrapup.get(target)
            if open_end and (w is None or (w["user"] == event.user_id and w["left"] > 0
                                           and time.time() - w["at"] <= WRAPUP_MINUTES * 60)):
                if w is None:
                    _wrapup[target] = {"user": event.user_id, "left": WRAPUP_EXTRA, "at": time.time()}
                logger.info(f"额度用完了，但和 {event.user_id} 的话还没说完，先不告别，收个尾再走：{target}")
            else:
                _wrapup.pop(target, None)
                farewell = await say_farewells(bot, event, target, already_bye=already_bye)
        mark_sent(target)
    if bubbles and cfg.knowledge_enabled:
        note_topic(key, "\n".join(bubbles))          # 她讲了哪段经历：对方接着追问时还记得
    if note_leaving(target, "\n".join(bubbles)):   # 她自己说了要走：接下来一阵子真的不在
        _nudge_due.pop(event.user_id, None)
    elif not is_group:
        arm_nudge(event.user_id, text)            # 私聊：对方一阵子没回的话，她可能自己再说一句
    if is_group:
        _last_bot_msg[event.group_id] = (time.monotonic(), event.user_id, record)
        # 刚回完这个人：从她最后一条发出算起，这个人接着说的话直接算在跟她说
        _engaged[(event.group_id, event.user_id)] = time.monotonic()
        _replied_at[(event.group_id, event.user_id)] = time.monotonic()


def _unrecord(key: str, record: str, keep: str = "") -> None:
    """这轮回复没发出去（或只发出去一部分）：把她那句从短期记忆里拿掉，或者改成真正发出去的部分；对方说的话留着。
    （不拿会话锁：这时拿着 _hands，别人可能拿着会话锁在等 _hands；这里没有 await，不会被打断）"""
    h = get_history(key)
    for i in range(len(h) - 1, -1, -1):
        if h[i].get("role") == "assistant" and h[i].get("content") == record:
            if keep:
                h[i]["content"] = keep
            else:
                h.pop(i)
            save_history(key)
            break


# ------------------------------------------------------------------ 限额用完：告别
TIRED_LINES = peak.TIRED_TIERS          # 台词按关系分档，在 peak.py 里
_tired_sent: dict[int, str] = {}          # QQ -> 哪一天（按花费的“一天”）已经回过“今天累了”


async def say_tired(bot: Bot, event: MessageEvent) -> None:
    """今天的钱花完了之后才来私聊的人：回一句固定的“今天累了”（每人每天一次；已经告别过的不再说），不调用模型"""
    if not cfg.budget_tired_reply:
        return
    target = _target_of(event)
    day = spend.day_key()
    if _tired_sent.get(event.user_id) == day or time.monotonic() - _farewell_at.get(target, -1e9) <= 3600:
        return
    _tired_sent[event.user_id] = day
    line = peak.tired_line(familiarity_of(event.user_id))
    async with _hands:
        await switch_pause(target)
        await asyncio.sleep(typing_delay(line))
        try:
            await bot.send(event, line)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"“今天累了”没发出去：{e}")
            return
        mark_sent(target)
    _farewell_at[target] = time.monotonic()
    key = session_key(event)
    get_history(key).append({"role": "assistant", "content": line, "ts": time.time()})
    save_history(key)
    logger.info(f"今天的额度用完了，对 {event.user_id} 说了句“今天累了”")


async def say_farewells(bot: Bot, event: MessageEvent | None, target: str | None, already_bye: bool = False,
                        skip=()) -> list[str]:
    """（已经拿着 _hands）限额用完或到了休息时间时告别。返回告别过的会话。
    - 今天的钱花完了 / 到了休息时间：对最近在聊的群和私聊都说一声（休息时说的是“要睡了”）；
      只是这个群 / 这个人的份额用完了：只对这里说。
    - already_bye：target 这个会话刚才已经互相道别过了，不再补一句（但照样记下“告别过了”，之后不回）
    - target 为 None：不是因为某条消息触发的（到点了，后台道晚安），只对最近在聊的说；skip 里的会话跳过"""
    sleeping = asleep()
    night = night_key()
    if quota_left(None) <= 0:
        scope = "global"
        now = time.monotonic()
        targets = [t for t, a in _active_chats.items()
                   if now - a["at"] <= cfg.farewell_active_minutes * 60 and t != target and t not in skip]
        targets = ([target] if target else []) + sorted(targets, key=lambda t: -_active_chats[t]["at"])
    else:
        scope, targets = target, [target]
    if not sleeping:
        if time.monotonic() - _farewell_at.get(scope, -1e9) <= 3600:
            return []
        _farewell_at[scope] = time.monotonic()
    done, used, sent_any = [], [], False
    for t in targets:
        if sleeping:
            if _goodnight.get(t) == night:
                continue                               # 今晚已经道过晚安了
            _goodnight[t] = night
        elif scope == "global" and time.monotonic() - _farewell_at.get(t, -1e9) <= 3600:
            continue                                   # 这个群刚因为本群限额告别过
        _farewell_at[t] = time.monotonic()
        private = t.startswith("private_")
        if t == target and already_bye:
            logger.info(f"{'要休息了' if sleeping else '额度用完'}，但刚才已经道别过了，不再补一句：{t}")
            continue
        fam = familiarity_of(_active_chats[t]["user_id"]) if private and _active_chats.get(t) else None
        line = (peak.sleep_line if sleeping else peak.farewell_line)(private=private, avoid=used, fam=fam)
        used.append(line)
        if sent_any:
            await switch_pause(t)
        await asyncio.sleep(bubble_gap(line) + (0 if sent_any else 1))
        try:
            if t == target and event is not None:
                await bot.send(event, line)
            elif private:
                await bot.send_private_msg(user_id=_active_chats[t]["user_id"], message=line)
            else:
                await bot.send_group_msg(group_id=_active_chats[t]["group_id"], message=line)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"告别没发出去（{t}）：{e}")
            continue
        sent_any = True
        mark_sent(t)
        _count_sent(None if private else _active_chats[t]["group_id"], _active_chats[t]["user_id"] if private else None)    # 告别也算一条
        # 记进这个会话的短期记忆（不去拿会话锁：这时拿着 _hands，别人可能拿着会话锁在等 _hands）
        hist_key = t if private or cfg.group_shared_memory else f"{t}_{_active_chats[t]['user_id']}"
        get_history(hist_key).append({"role": "assistant", "content": line, "ts": time.time()})
        save_history(hist_key)
        done.append(t)
    if done:
        why = "到了休息时间" if sleeping else ("今天的钱花完了" if scope == "global" else
                                          ("本群的份额用完" if scope.startswith("group_") else "这个人的份额用完"))
        logger.info(f"{why}，已告别：{'、'.join(done)}")
    return done


# ------------------------------------------------------------------ 晚上休息（9/29）
# 23:30～7:30（SLEEP_HOURS）不回消息、不插话冒泡写信搭话；到点时对正在聊的道声晚安（和限额用完的告别同一套：
# 话没说完先收尾，最多再回 2 次；刚互相道过别的不补）。休息时才来找她的，不回，像睡着了
_goodnight: dict[str, str] = {}       # 会话 -> 哪天晚上已经道过晚安
_sleep_task: "asyncio.Task | None" = None


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


# 快到限额、快到睡觉时：让话题慢慢往收尾靠，免得告别来得突兀（大概的写法，台词细节以后再打磨）
WINDDOWN_SLEEP_HINT = ("【快到休息时间了】现在快 {at} 了，你开始犯困。可以不经意地流露一点困意，但不用每句都提："
                       "怎么表现自己想（回得慢半拍、打个哈欠、说一句有点困了、吐槽自己居然困了……这些只是举例，别照搬），每次换个说法。"
                       "话题慢慢往收尾靠，别再开新的长话题；但这次还不用道晚安，对方的话照常好好回。")
WINDDOWN_TIRED_HINT = ("【有点累了】你今天已经聊了很久，有点累了。话题慢慢往收尾靠：少反问、别开新话题，回得可以比平时短一点。"
                       "可以不经意地带一句理由，理由自己想，每次换一个（赶路、天黑、找旅馆、面包已经用得太多了，少用）；"
                       "但这次还不用说再见，对方的话照常好好回。")
WRAPUP_HINT = ("【该收尾了】你今天在这里已经说了很多，马上就要走了。这次把对方的话回完、把话题收个尾，别再反问、别再开新话题；"
               "最后顺口说一句要走了（比如“我先走了”“下次再聊”）。要不要说理由、说什么理由都随你，自己想一个贴合当下的；“赶路”“天黑前要到下一个镇子”“去买面包”已经用得太多了，尽量别用；但一定要让人听得出你要走了。"
               "按你们的关系来：不熟的就客气一句；熟的可以嘴硬地流露一点不舍，但不承认。")
WRAPUP_SLEEP_HINT = ("【该睡了】已经很晚了，你困得不行，马上要去睡了。这次把对方的话回完、把话题收个尾，别再反问、别再开新话题；"
                     "最后顺口道一句晚安，或者说一句要去睡了。按你们的关系来：不熟的礼貌一句；熟的可以顺手叮嘱一句别熬夜，"
                     "或者嘴硬地开个玩笑（比如嫌对方害你熬夜）。")


def winddown_hint(group_id: int | None, user_id: int, now: datetime | None = None) -> str:
    """快到睡觉时间、或者今天的钱（这个人 / 这个群的份额）快用完时，提示她把话题慢慢往收尾靠"""
    now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    if cfg.sleep_enabled and cfg.sleep_winddown_minutes > 0 and not asleep(now):
        w = sleep_window(night_key(now))
        if w and 0 < (w[0] - now).total_seconds() <= cfg.sleep_winddown_minutes * 60:
            return WINDDOWN_SLEEP_HINT.format(at=f"{w[0].hour} 点 {w[0].minute} 分" if w[0].minute else f"{w[0].hour} 点")
    if cfg.budget_winddown_rounds > 0:
        left = min(quota_left(group_id, None if group_id else user_id), _rounds(spend.user_left(user_id)))
        if 0 < left <= cfg.budget_winddown_rounds:
            return WINDDOWN_TIRED_HINT
    return ""


def _sleep_wrapup(target: str | None, user_id: int) -> None:
    """刚到休息时间，正在跟她聊的人又说话了：给他收个尾（最多再回 2 次），说完再道晚安"""
    if not target or target in _wrapup or _goodnight.get(target) == night_key():
        return
    a = _active_chats.get(target)
    if a and a.get("user_id") == user_id and time.monotonic() - a["at"] <= cfg.farewell_active_minutes * 60:
        _wrapup[target] = {"user": user_id, "left": WRAPUP_EXTRA, "at": time.time()}
        logger.info(f"到休息时间了，{user_id} 还在跟她聊：先把话说完再道晚安（{target}）")


def _said_bye_last(t: str) -> bool:
    """这个会话里她最后一句已经在道别了（说了再见、要走了）"""
    a = _active_chats.get(t) or {}
    hist_key = t if t.startswith("private_") or cfg.group_shared_memory else f"{t}_{a.get('user_id')}"
    last = next((h for h in reversed(get_history(hist_key)) if h.get("role") == "assistant"), None)
    return bool(last) and bool(_LEAVE_RE.search(last["content"]) or _BYE_RE.search(last["content"]))


async def goodnight_now() -> list[str]:
    """到了休息时间：给最近在聊、又没在收尾、也没刚道过别的会话道声晚安"""
    bots = list(get_bots().values())
    if not bots or not cfg.farewell_on_limit:
        return []
    now = time.monotonic()
    skip = set()
    for t, a in _active_chats.items():
        if now - a["at"] > cfg.farewell_active_minutes * 60:
            continue
        pending = any(k[0] == (t if t.startswith("private_") or cfg.group_shared_memory else f"{t}_{a.get('user_id')}")
                      and v for k, v in _inbox.items())
        if t in _wrapup or pending:
            skip.add(t)                        # 话还没说完：等对方这条回完再说晚安（见 _sleep_wrapup）
        elif _said_bye_last(t):
            _goodnight[t] = night_key()        # 刚道过别了，不再补
            skip.add(t)
    async with _hands:
        return await say_farewells(bots[0], None, None, skip=skip)


async def _sleep_loop() -> None:
    done_night = None
    told = None
    while True:
        await asyncio.sleep(30)
        try:
            night = night_key()
            if told != night and (w := sleep_window(night)):
                told = night
                logger.info(f"今晚 {w[0]:%H:%M} 睡，明早 {w[1]:%H:%M} 起（SLEEP_HOURS {cfg.sleep_hours}，前后浮动 {cfg.sleep_jitter_minutes:g} 分钟）")
            if asleep() and done_night != night:
                done_night = night
                await goodnight_now()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"道晚安出错：{e}")


# ------------------------------------------------------------------ 掉线提醒：重新上线后私信管理员
OFFLINE_FILE = BOT_DIR / "data" / "offline_alert.json"
_offline_alert_task: "asyncio.Task | None" = None
_quiet_until = 0.0         # 被踢下线又上线后，这个时间（time.time()）之前先不说话


def quiet_left() -> float:
    """刚被踢下线又上线：还要安静几秒（0 = 不用安静）"""
    return max(0.0, _quiet_until - time.time())


def start_quiet() -> None:
    global _quiet_until
    if cfg.relogin_quiet_minutes > 0:
        _quiet_until = time.time() + cfg.relogin_quiet_minutes * 60
        logger.info(f"被踢下线后重新上线：先安静 {cfg.relogin_quiet_minutes:g} 分钟（不回消息、不插话、不冒泡、不写信），之后补回这段时间的私聊")


def _read_offline() -> dict:
    try:
        return json.loads(OFFLINE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_offline(data: dict | None) -> None:
    try:
        if data is None:
            OFFLINE_FILE.unlink(missing_ok=True)
            return
        OFFLINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFLINE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"记录掉线信息失败：{e}")


def note_offline(kicked: bool, reason: str) -> None:
    """记下这次下线：被踢（NapCat 报告账号下线）优先，断开连接只在没有记录时补一条"""
    old = _read_offline()
    if old.get("kicked") and not kicked:
        return
    now = time.monotonic()
    _write_offline({
        "since": old.get("since") or time.time(),      # 先断开、后收到“被下线”通知时，按最早那次算
        "kicked": kicked, "reason": reason, "pid": os.getpid(),
        "replies_hour": sum(1 for t in _hour_window if now - t <= 3600),
    })


def offline_message(rec: dict, self_id: str) -> str:
    since = datetime.fromtimestamp(float(rec["since"]), peak.BEIJING)
    mins = max(1, int((time.time() - float(rec["since"])) / 60))
    gap = f"{mins} 分钟" if mins < 60 else f"{mins // 60} 小时 {mins % 60} 分钟"
    why = (f"NapCat 报告账号被下线（{rec['reason']}）" if rec.get("kicked")
           else "机器人和 NapCat 的连接断了，可能是 NapCat 关了、崩了，或者账号被下线")
    tip = ""
    if rec.get("kicked"):
        tip = "\n如果是被踢下线，建议先少回一阵，详见《风控记录》。"
        if cfg.relogin_quiet_minutes > 0:
            tip += f"\n她会先安静 {cfg.relogin_quiet_minutes:g} 分钟，之后再补回这段时间的私聊。"
    return (f"【QQ 机器人提醒】小号 {self_id} 在 {since:%m月%d日 %H:%M} 下线了。\n原因：{why}\n"
            f"离线约 {gap}，现在已重新上线。下线前一小时回复了 {rec.get('replies_hour', 0)} 条。{tip}")


def should_alert(rec: dict) -> bool:
    """被踢下线：一定提醒。只是断开连接：机器人一直在跑（同一个进程）、断了 1 分钟以上才提醒；
    换了进程多半是你自己重开了 start.bat，不提醒"""
    if not rec or not cfg.offline_alert:
        return False
    if rec.get("kicked"):
        return True
    return rec.get("pid") == os.getpid() and time.time() - float(rec.get("since", time.time())) >= 60


async def send_offline_alert(bot: Bot, delay: float = 15.0) -> bool:
    rec = _read_offline()
    if not rec:
        return False
    ok = should_alert(rec)
    _write_offline(None)
    if not ok:
        return False
    await asyncio.sleep(delay)                          # 刚上线先缓一缓
    text = offline_message(rec, str(bot.self_id))
    sent = False
    for su in bot.config.superusers:
        try:
            await bot.send_private_msg(user_id=int(su), message=text)
            sent = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"掉线提醒没发出去（管理员 {su} 需要和小号是好友）：{e}")
    logger.info(f"已私信管理员掉线情况：{text.splitlines()[0]}")
    return sent


async def _is_offline_event(event: NoticeEvent) -> bool:
    return "offline" in str(getattr(event, "notice_type", "")).lower()


kicked_notice = on_notice(rule=_is_offline_event, priority=1, block=False)


@kicked_notice.handle()
async def _(event: NoticeEvent):
    d = event.model_dump()
    reason = str(d.get("message") or d.get("tag") or "没说原因")[:60]
    note_offline(True, reason)
    logger.warning(f"账号被下线：{reason}")
    # NapCat 有时不断开连接、自己重新登录：心跳要在这里就停，不然离线这段时间会被心跳盖掉，私聊补不回来
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
    if cfg.catchup_enabled:
        try:
            _save_online()          # 心跳停在下线这一刻
        except Exception:  # noqa: BLE001
            pass


# 被踢后 NapCat 有时不断开连接、自己重新登录：又收到消息就说明回来了，这时补发提醒
async def _back_after_kick(event: MessageEvent) -> bool:
    return bool(cfg.offline_alert and _read_offline().get("kicked")) and (_offline_alert_task is None or _offline_alert_task.done())


back_after_kick = on_message(rule=_back_after_kick, priority=0, block=False)


@back_after_kick.handle()
async def _(bot: Bot):
    global _offline_alert_task
    start_quiet()
    _offline_alert_task = asyncio.create_task(send_offline_alert(bot, delay=5))
    # 没断开连接就回来了：不会触发“连上”，这里补做一次“补回未读、再开始记心跳”
    if _heartbeat_task is None and (_catchup_task is None or _catchup_task.done()):
        start_catchup(bot)


# ------------------------------------------------------------------ 主动插话：群里聊得正热时，她偶尔自己插一句
_recent_chat: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))   # 群号 -> 最近的群聊（时间, QQ, 昵称, 内容）
INTERJECT_FILE = BOT_DIR / "data" / "interject.json"
_interject_busy: set[int] = set()
_interject_skip_until: dict[int, float] = {}

INTERJECT_PROMPT = """【插话】群里大家正在聊天，没人叫你，你在一旁看着（上面「群聊旁听记录」是最近的聊天）。
- 如果你真的有想说的——话题正好是你感兴趣的（面包、旅行、魔法、钱之类）、有人说错了你知道的事、或者有能吐槽的地方（比如有人说谁自恋、说美少女、聊魔女和扫帚，你忍不住接一句自恋的话或者吐槽回去）——就自然地插一句，10～30 字，像群友随口搭话。
- 插话只是随口一句，不因为这一句对谁更亲近或更冷淡。
- 直接接着大家的话说，不打招呼，不问“你们在聊什么”，不自我介绍，不要一次回应好几个人。
- 大家聊的你不感兴趣、插不上话，或者插了会很突兀，就只输出「[不插]」。"""
_NO_INTERJECT_RE = re.compile(r"[\[【（(［]\s*不插\s*[\]】）)］]")


def _interject_state() -> dict:
    try:
        return json.loads(INTERJECT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_interject_state(st: dict) -> None:
    try:
        INTERJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
        INTERJECT_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"记录插话次数失败：{e}")


def _in_hours(ranges: str) -> bool:
    now = datetime.now(peak.BEIJING)
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in peak.parse_ranges(ranges))


def interject_blocker(gid: int, text: str = "") -> str | None:
    """现在不能在这个群插话的原因；可以就返回 None（不含随机那一步）"""
    if away_left(f"group_{gid}") > 0:
        return "她说过要走了"
    if in_peak():
        return "高峰时段"
    if not _in_hours(cfg.interject_hours):
        return "不在插话时段"
    if gid in _interject_busy:
        return "正在看"
    now_m, now_t = time.monotonic(), time.time()
    if now_m < _interject_skip_until.get(gid, 0):
        return "刚看过，没什么想说的"
    st = _interject_state().get(str(gid), {})
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    if st.get("day") == today and st.get("count", 0) >= cfg.interject_daily_max:
        return "今天插够了"
    if now_t - float(st.get("last", 0)) < cfg.interject_min_gap_hours * 3600:
        return "离上次插话太近"
    last = _last_bot_msg.get(gid)
    if last and now_m - last[0] < 600:
        return "她刚在这个群说过话"
    if quota_left(gid) < 5:
        return "这一小时额度不多了"
    recent = [x for x in _recent_chat.get(gid, []) if now_m - x[0] <= cfg.interject_hot_minutes * 60]
    if len(recent) < cfg.interject_hot_messages or len({x[1] for x in recent}) < cfg.interject_hot_people:
        return "群里没在热聊"
    return None


def _take_group_quota(gid: int) -> bool:
    """插话也算进每分钟的限额、今天的钱和这个群的份额（开了每小时条数限额的话也算；不算个人冷却）"""
    now = time.monotonic()
    while _global_window and now - _global_window[0] > 60:
        _global_window.popleft()
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.popleft()
    gq = _group_hour[gid]
    while gq and now - gq[0] > 3600:
        gq.popleft()
    if (len(_global_window) >= cfg.global_rate_per_minute
            or 0 < cfg.global_rate_per_hour <= len(_hour_window)
            or 0 < cfg.group_rate_per_hour <= len(gq)
            or quota_left(gid) <= 0):
        return False
    _global_window.append(now)
    _hour_window.append(now)
    gq.append(now)
    return True


async def maybe_interject(bot: Bot, gid: int, text: str, force: bool = False) -> str | None:
    """看看要不要在这个群插一句；插了就返回说的话"""
    if quiet_left() > 0:
        return None
    if not force:
        if interject_blocker(gid, text):
            return None
        topical = any(w in text for w in cfg.interject_topics)
        if random.random() >= (cfg.interject_prob if topical else cfg.interject_offtopic_prob):
            return None
    if gid in _interject_busy or not cfg.deepseek_api_key:
        return None
    _interject_busy.add(gid)
    try:
        return await _interject(bot, gid, force)
    finally:
        _interject_busy.discard(gid)


async def _interject(bot: Bot, gid: int, force: bool) -> str | None:
    key = f"group_{gid}"
    if not force and not _has_quota(gid):          # 先看额度，不够就不调模型了
        logger.info(f"插话：群{gid} 额度不够，算了")
        return None
    async with _locks[key]:
        history = get_history(key)
        chat_lines = list(_recent_chat.get(gid, []))[-8:]
        if not chat_lines:
            return None
        now_m, now_t = time.monotonic(), time.time()
        watch = watch_block([(now_t - (now_m - m), ln) for m, _, _, ln in chat_lines])
        messages = api_messages(
            [{"role": "system", "content": system_prompt()}]
            + history[-6:]
            + [{"role": "user", "content": watch},
               {"role": "system", "content": INTERJECT_PROMPT + short_hint()}]
        )
        try:
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model, messages=messages, temperature=cfg.llm_temperature,
                max_tokens=cfg.short_reply_max_tokens, extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "interject", group=gid)
            choice = resp.choices[0]
            reply = clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            logger.warning(f"插话：调用失败（{kind or '其他错误'}）：{e}")
            if kind:
                await alert_admins(bot, kind)
            return None
        if not reply or _NO_INTERJECT_RE.search(reply) or ooc_words(reply, ""):
            _interject_skip_until[gid] = time.monotonic() + cfg.interject_retry_minutes * 60
            logger.info(f"插话：群{gid} 她看了看，没什么想说的")
            return None
        if not force and not _take_group_quota(gid):
            logger.info(f"插话：群{gid} 额度不够，算了")
            return None
        # 旁听记录并进短期记忆，再记她这句
        buf = list(_passive.pop(gid, []))
        if buf:
            history.append({"role": "user", "content": watch_block([(ts, ln) for _, _, ln, ts in buf]),
                            "speakers": {n: uid for uid, n, _, _ in buf}, "ts": time.time()})
        history.append({"role": "assistant", "content": reply, "ts": time.time()})
        save_history(key)
        # 插话不交给长期记忆整理：不因为这一句给谁加减好感
    bubbles = split_bubbles(reply)[:2]
    target = f"group_{gid}"
    async with _hands:
        await switch_pause(target)
        await asyncio.sleep(typing_delay(bubbles[0]))
        for i, b in enumerate(bubbles):
            if i:
                await asyncio.sleep(bubble_gap(b))
            try:
                await bot.send_group_msg(group_id=gid, message=b)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"插话：群{gid} 发送失败：{e}")
                if not i:
                    _unrecord(key, reply)
                return None
        mark_sent(target)
    now = time.monotonic()
    _last_bot_msg[gid] = (now, 0, reply)     # 有人接她的话，走“她刚说完话”的判断
    _last_smart[gid] = now
    st = _interject_state()
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    g = st.get(str(gid), {})
    count = g.get("count", 0) + 1 if g.get("day") == today else 1
    st[str(gid)] = {"day": today, "count": count, "last": time.time()}
    _save_interject_state(st)
    logger.info(f"插话：群{gid} 主动说了一句（今天第 {count} 次）：{reply[:30]}")
    return reply


interject_cmd = on_command("插话", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@interject_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """/插话：在这个群里马上让她看一眼要不要插话（测试用，不看次数和热度）；/插话 状态：看看现在能不能插"""
    if not isinstance(event, GroupMessageEvent):
        await interject_cmd.finish("（这个要在群里用）")
    gid = event.group_id
    if arg.extract_plain_text().strip() == "状态":
        st = _interject_state().get(str(gid), {})
        today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
        n = st.get("count", 0) if st.get("day") == today else 0
        why = interject_blocker(gid) or "可以，群里再有人说话就有机会插"
        b = st.get("bubble_count", 0) if st.get("bubble_day") == today else 0
        bwhy = bubble_blocker(gid) or "可以，下次检查时有机会冒泡"
        await interject_cmd.finish(f"（今天已插话 {n}/{cfg.interject_daily_max} 次｜现在：{why}）\n"
                                   f"（今天已冒泡 {b}/{cfg.bubble_daily_max} 次｜现在：{bwhy}）")
    said = await maybe_interject(bot, gid, "", force=True)
    if not said:
        await interject_cmd.finish("（她看了看最近的聊天，没什么想插的）")


# ------------------------------------------------------------------ 冒泡：群里很久没人说话时，她偶尔自己说一句
BUBBLE_PROMPT = """【冒泡】现在是{now}，这个群已经{gap}没人说话了。你正好有空，随口在群里说一句：
- 可以是旅途中刚遇到的小事、看到的风景、吃到的东西、遇到的怪人、接到的奇怪委托，或者吐槽一下天气，也可以随口问大家一句。别每次都说面包。要符合现在的时间（早上、下午、晚上）。
- 10～30 字，像随手发的一条群消息。不要说“大家好”“有人吗”“好安静啊”，也不要提“冒泡”“好久没人说话”。
- 不要搬出书里有名有姓的人物和事件，随手编一件小事就好。
- 不想说就只输出「[不说]」。"""
_NO_BUBBLE_RE = re.compile(r"[\[【（(［]\s*不说\s*[\]】）)］]")
_bubble_task: "asyncio.Task | None" = None


def _group_last_activity(gid: int) -> tuple[float | None, str | None]:
    """这个群最后一次有人（或她）说话的时间（time.time()），以及最后说话的是 user 还是 assistant"""
    h = get_history(f"group_{gid}")
    last_ts, last_role = None, None
    for e in reversed(h):
        if e.get("ts"):
            last_ts, last_role = float(e["ts"]), e["role"]
            break
    recent = _recent_chat.get(gid)
    if recent:                                        # 旁听里还有没进记忆的消息：以最新的为准
        ts = time.time() - (time.monotonic() - recent[-1][0])
        if last_ts is None or ts > last_ts:
            last_ts, last_role = ts, "user"
    return last_ts, last_role


def bubble_blocker(gid: int) -> str | None:
    """现在不能在这个群冒泡的原因；可以就返回 None（不含随机那一步）"""
    if not cfg.bubble_enabled:
        return "冒泡没开"
    if away_left(f"group_{gid}") > 0:
        return "她说过要走了"
    if in_peak():
        return "高峰时段"
    if not _in_hours(cfg.bubble_hours):
        return "不在冒泡时段"
    if gid in _interject_busy:
        return "正在想"
    st = _interject_state().get(str(gid), {})
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    if st.get("bubble_day") == today and st.get("bubble_count", 0) >= cfg.bubble_daily_max:
        return "今天冒过泡了"
    if time.time() - float(st.get("bubble_last", 0)) < cfg.bubble_min_gap_hours * 3600:
        return "离上次冒泡太近"
    all_st = _interject_state()
    today_total = sum(g.get("bubble_count", 0) for g in all_st.values() if isinstance(g, dict) and g.get("bubble_day") == today)
    if today_total >= cfg.bubble_global_daily_max:
        return "今天所有群加起来冒够了"
    last_ts, last_role = _group_last_activity(gid)
    if last_ts is None:
        return "还不知道这个群最近有没有人说话"
    if last_role == "assistant":
        return "最后一句是她说的，还没人接"
    if time.time() - last_ts < cfg.bubble_quiet_hours * 3600:
        return "群里最近有人说话"
    if quota_left(gid) < 5:
        return "这一小时额度不多了"
    return None


async def bubble(bot: Bot, gid: int, force: bool = False) -> str | None:
    """在这个群冒个泡；说了就返回说的话"""
    if gid in _interject_busy or not cfg.deepseek_api_key:
        return None
    if not force and not _has_quota(gid):          # 先看额度，不够就不调模型了
        return None
    _interject_busy.add(gid)
    try:
        key = f"group_{gid}"
        async with _locks[key]:
            history = get_history(key)
            last_ts, _ = _group_last_activity(gid)
            gap = human_gap(time.time() - last_ts) if last_ts else "好一阵子"
            now = datetime.now(peak.BEIJING)
            prompt = BUBBLE_PROMPT.format(now=f"{now.hour} 点 {now.minute} 分", gap=gap)
            messages = api_messages(
                [{"role": "system", "content": system_prompt()}]
                + history[-6:]
                + [{"role": "system", "content": prompt + short_hint()}]
            )
            try:
                resp = await client.chat.completions.create(
                    model=cfg.deepseek_model, messages=messages, temperature=cfg.llm_temperature,
                    max_tokens=cfg.short_reply_max_tokens, extra_body={"thinking": {"type": "disabled"}},
                )
                budget.track(resp, "bubble", group=gid)
                choice = resp.choices[0]
                reply = clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
            except Exception as e:  # noqa: BLE001
                kind = classify_error(e)
                logger.warning(f"冒泡：调用失败（{kind or '其他错误'}）：{e}")
                if kind:
                    await alert_admins(bot, kind)
                return None
            if not reply or _NO_BUBBLE_RE.search(reply) or ooc_words(reply, ""):
                logger.info(f"冒泡：群{gid} 她不想说话")
                return None
            if not force and not _take_group_quota(gid):
                return None
            history.append({"role": "assistant", "content": reply, "ts": time.time()})
            save_history(key)
        bubbles = split_bubbles(reply)[:2]
        async with _hands:
            await switch_pause(key)
            await asyncio.sleep(typing_delay(bubbles[0]))
            for i, b in enumerate(bubbles):
                if i:
                    await asyncio.sleep(bubble_gap(b))
                try:
                    await bot.send_group_msg(group_id=gid, message=b)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"冒泡：群{gid} 发送失败：{e}")
                    if not i:
                        _unrecord(key, reply)
                    return None
            mark_sent(key)
        now_m = time.monotonic()
        _last_bot_msg[gid] = (now_m, 0, reply)
        _last_smart[gid] = now_m
        st = _interject_state()
        today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
        g = st.get(str(gid), {})
        g["bubble_count"] = g.get("bubble_count", 0) + 1 if g.get("bubble_day") == today else 1
        g["bubble_day"], g["bubble_last"] = today, time.time()
        st[str(gid)] = g
        _save_interject_state(st)
        logger.info(f"冒泡：群{gid} 很久没人说话，她说了一句：{reply[:30]}")
        return reply
    finally:
        _interject_busy.discard(gid)


async def check_bubbles() -> int:
    """看看白名单里哪些群该冒泡了；返回冒了几个"""
    if quiet_left() > 0:
        return 0
    if not (cfg.bubble_enabled and cfg.enable_group and cfg.deepseek_api_key and cfg.group_whitelist):
        return 0
    bots = list(get_bots().values())
    if not bots:
        return 0
    n = 0
    for gid in list(cfg.group_whitelist):
        if bubble_blocker(gid) is None and random.random() < cfg.bubble_prob:
            if await bubble(bots[0], gid):
                n += 1
                await asyncio.sleep(random.uniform(60, 300))   # 好几个群一起冒泡太假，隔开一点
    return n


async def _bubble_loop() -> None:
    await asyncio.sleep(300)                                    # 刚启动先别急
    while True:
        try:
            await check_bubbles()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查冒泡出错：{e}")
        await asyncio.sleep(cfg.bubble_check_minutes * 60)


bubble_cmd = on_command("冒泡", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@bubble_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    """/冒泡：在这个群里马上让她随口说一句（测试用，不看次数和安静多久）"""
    if not isinstance(event, GroupMessageEvent):
        await bubble_cmd.finish("（这个要在群里用）")
    said = await bubble(bot, event.group_id, force=True)
    if not said:
        await bubble_cmd.finish("（她这会儿不想说话）")


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
            budget.track(resp, "letter", user=qq)
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
    _private_hour[qq].append(time.monotonic())
    _hour_window.append(time.monotonic())
    _global_window.append(time.monotonic())
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    _letters_sent[today] = _letters_sent.get(today, 0) + 1
    logger.info(f"给 {name}（{qq}）寄了一封信")
    return letter


async def check_letters() -> int:
    """看看今天要不要给谁写信；返回寄出了几封"""
    if quiet_left() > 0:
        return 0
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


# ------------------------------------------------------------------ 私聊冷场后主动搭话
# 她回完以后，对方 5～30 分钟没回：按关系远近抽签（陌生人 5%、熟人 15%、很熟 35%，讨厌的人不搭），
# 抽中了就自己再说一句——接着刚才的话题、随口问问、或者说点旅途里的小事。对方一直不回也只搭这一次。
NUDGE_PROMPT = """【主动搭话】你刚才回了对方，对方已经 {gap}没回你了。你想主动再说一句，像随手发的一条消息。
可以：接着刚才的话题追问一句、补一句刚才没说完的、吐槽一下，或者随口说一件你旅途中的小事、问问对方在干嘛。
不要：质问“怎么不理我”、说自己一直在等、重复刚才说过的话、提机器人或系统。
按你们的关系拿捏：陌生人客气、话少；熟人随意；很熟可以撒点小脾气、关心一下。
如果刚才的对话已经自然结束（对方道别、说晚安、说去忙了），或者这时候搭话很突兀，就只输出「[不说]」。"""
_NO_NUDGE_RE = re.compile(r"[\[【（(［]\s*不说\s*[\]】）)］]")
_BYE_RE = re.compile(r"(晚安|拜拜|再见|先走了|先下了|下线了|睡了|去睡|去忙|忙去了|回聊|改天聊|明天聊|88|bye|good ?night)", re.I)
_nudge_due: dict[int, dict] = {}          # QQ -> {"at": 什么时候搭话, "armed": 她那句回复的时间}
_nudge_count: dict[str, dict] = {}        # 日期 -> {QQ: 今天搭过几次, "all": 合计}
_nudge_task: "asyncio.Task | None" = None


def arm_nudge(qq: int, user_text: str = "") -> bool:
    """她刚回完这个人：抽签决定这次冷场要不要搭话，要的话定好时间"""
    _nudge_due.pop(qq, None)
    if not (cfg.nudge_enabled and cfg.enable_private):
        return False
    if _BYE_RE.search(user_text or ""):        # 对方在道别：不搭
        return False
    if random.random() >= tier_value(cfg.nudge_prob, familiarity_of(qq), 0.0):
        return False
    lo, hi = sorted((cfg.nudge_delay_min, cfg.nudge_delay_max))
    _nudge_due[qq] = {"at": time.time() + random.uniform(lo, hi) * 60, "armed": time.time()}
    return True


def nudge_blocker(qq: int) -> str | None:
    """现在不能给这个人搭话的原因；可以就返回 None"""
    if quiet_left() > 0:
        return "刚重新上线"
    if away_left(f"private_{qq}") > 0:
        return "她说过要走了"
    if in_peak():
        return "高峰时段"
    if not _in_hours(cfg.nudge_hours):
        return "不在搭话时段"
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    cnt = _nudge_count.get(today, {})
    if cnt.get(qq, 0) >= cfg.nudge_per_user_daily_max:
        return "今天已经搭过话了"
    if cnt.get("all", 0) >= cfg.nudge_daily_max:
        return "今天搭话次数用完了"
    if not _has_quota(None, qq):
        return "额度不够"
    return None


async def nudge(bot: Bot, qq: int, force: bool = False) -> str | None:
    """冷场了，主动再说一句；说了就返回说的话"""
    key = f"private_{qq}"
    if not cfg.deepseek_api_key:
        return None
    async with _locks[key]:
        history = get_history(key)
        last = history[-1] if history else None
        if not force and (not last or last.get("role") != "assistant"):
            return None                              # 对方已经回了（或者还没聊过）
        gap = human_gap(time.time() - float(last.get("ts") or time.time())) if last else "一阵子"
        fam = familiarity_of(qq)
        prof = ltm.get_user(qq)
        long_memo = ltm.context_for(qq, prof.get("name") or str(qq), None)
        extra = "\n\n".join(x for x in (long_memo, NUDGE_PROMPT.format(gap=gap), FAMILIARITY_HINT[fam] + short_hint()) if x)
        messages = api_messages([{"role": "system", "content": system_prompt()}] + history[-10:]
                                + [{"role": "system", "content": extra}])
        try:
            resp = await client.chat.completions.create(
                model=cfg.deepseek_model, messages=messages, temperature=cfg.llm_temperature,
                max_tokens=cfg.short_reply_max_tokens, extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "nudge", user=qq)
            choice = resp.choices[0]
            reply = clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            logger.warning(f"主动搭话：调用失败（{kind or '其他错误'}）：{e}")
            if kind:
                await alert_admins(bot, kind)
            return None
        if not reply or _NO_NUDGE_RE.search(reply) or ooc_words(reply, ""):
            logger.info(f"主动搭话：{qq} 她想了想，没说")
            return None
    bubbles = split_bubbles(reply)[:2]

    def replied() -> bool:                           # 她想、打字的时候，对方回消息了：这句就不发了
        h = get_history(key)
        return bool(_inbox.get((key, qq))) or (bool(h) and h[-1] is not last)
    async with _hands:
        if not force and replied():
            logger.info(f"主动搭话：{qq} 对方刚好回了，不发了")
            return None
        await switch_pause(key)
        await asyncio.sleep(typing_delay(bubbles[0]))
        if not force and replied():
            logger.info(f"主动搭话：{qq} 对方刚好回了，不发了")
            return None
        for i, b in enumerate(bubbles):
            if i:
                await asyncio.sleep(bubble_gap(b))
            try:
                await bot.send_private_msg(user_id=qq, message=b)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"主动搭话：发给 {qq} 失败：{e}")
                if not i:
                    return None
                break
            _count_sent(None, qq)
        mark_sent(key)
    said = "\n".join(bubbles)
    get_history(key).append({"role": "assistant", "content": said, "ts": time.time()})
    save_history(key)
    today = datetime.now(peak.BEIJING).strftime("%Y-%m-%d")
    for d in [d for d in _nudge_count if d != today]:
        del _nudge_count[d]
    cnt = _nudge_count.setdefault(today, {})
    cnt[qq] = cnt.get(qq, 0) + 1
    cnt["all"] = cnt.get("all", 0) + 1
    logger.info(f"主动搭话：{ltm.TIER_NAMES.get(familiarity_of(qq), '')} {prof.get('name') or qq}（{qq}）冷场 {gap}，她说：{said[:30]}")
    return said


async def check_nudges(bot: Bot | None = None) -> int:
    """到点的冷场，挨个看要不要搭话；返回搭了几次"""
    now = time.time()
    due = [qq for qq, d in _nudge_due.items() if d["at"] <= now]
    if not due:
        return 0
    bots = [bot] if bot else list(get_bots().values())
    if not bots:
        return 0
    n = 0
    for qq in due:
        _nudge_due.pop(qq, None)                     # 只搭这一次；对方回了以后才会再抽签
        why = nudge_blocker(qq)
        if why:
            logger.info(f"主动搭话：{qq} 这次不搭（{why}）")
            continue
        if await nudge(bots[0], qq):
            n += 1
    return n


async def _nudge_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await check_nudges()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查主动搭话出错：{e}")


nudge_cmd = on_command("搭话", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@nudge_cmd.handle()
async def _(bot: Bot, arg: Message = CommandArg()):
    t = arg.extract_plain_text().strip()
    if not t.isdigit():
        pending = [f"{q}（{datetime.fromtimestamp(d['at'], peak.BEIJING):%H:%M}）" for q, d in _nudge_due.items()]
        await nudge_cmd.finish("（用法：/搭话 QQ号 —— 马上让她主动跟这个人说一句。等着搭话的："
                               + ("、".join(pending) if pending else "没有") + "）")
    said = await nudge(bot, int(t), force=True)
    await nudge_cmd.finish(f"（她说了：{said}）" if said else "（她想了想，没说。或者调用失败，看日志）")


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
        n = await stickers.label_pending(can_run=lambda: not in_peak() and budget.can_background())
        if n:
            logger.info(f"表情：打好了 {n} 张的标签（{stickers.summary()}）")

    _label_task = asyncio.create_task(run())


async def _sticker_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    if stickers.unlabeled() and not in_peak():       # 上次没打完的标签：先接着打（不调 QQ 接口）
        _spawn_labeling()
    since = time.time() - stickers.last_refresh
    if since < cfg.sticker_refresh_hours * 3600:
        logger.info(f"表情：距上次拉取收藏 {since / 3600:.1f} 小时，不到 {cfg.sticker_refresh_hours:g} 小时，这次上线不拉")
    else:
        # 刚上线别马上调一串接口：随机等几分钟再拉
        await asyncio.sleep(random.uniform(120, 420))
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


# ------------------------------------------------------------------ 加好友：验证消息不回
# 加好友时对方填的验证消息（“我是某某”），通过以后会以对方的名义出现在私聊里；QQ 还会补一条“我们已成功添加为好友……”。
# 这些都不是在跟她说话，不回、不补。
FRIEND_REQ_FILE = BOT_DIR / "data" / "friend_requests.json"
_FRIEND_SYS_RE = re.compile(
    r"^(我们已成功添加为好友|我通过了你的(朋友|好友)验证请求|你已添加了|你们已成为好友|以上是打招呼的内容|现在可以开始聊天了|"
    r"我已经添加了你|我们已经是好友了)")
_friend_added: dict[int, float] = {}         # QQ -> 刚加上好友的时间（friend_add 通知）


def _friend_reqs() -> dict:
    try:
        return json.loads(FRIEND_REQ_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_friend_reqs(d: dict) -> None:
    now = time.time()
    d = {k: v for k, v in d.items() if now - float(v.get("at", 0)) <= 7 * 86400}   # 一周前的申请不留
    try:
        FRIEND_REQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FRIEND_REQ_FILE.with_name(FRIEND_REQ_FILE.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FRIEND_REQ_FILE)
    except OSError as e:
        logger.warning(f"记录好友申请失败：{e}")


def _norm(t: str) -> str:
    return re.sub(r"\s", "", t or "")


def is_friend_verify(qq: int, text: str, msg_time: float = 0) -> bool:
    """这条私聊是不是加好友带来的验证消息或系统提示"""
    t = _norm(text)
    if not t:
        return False
    if _FRIEND_SYS_RE.match(t):
        return True
    req = _friend_reqs().get(str(qq))
    # 和申请里的验证消息一字不差：加上好友 10 分钟内都算（之后对方真的再说一遍同样的话，就正常回）
    if req and _norm(req.get("comment")) == t and (not req.get("added") or (msg_time or time.time()) - req["added"] <= 600):
        return True
    # 申请发来时机器人没在线、没记下验证消息：刚加上好友前后 20 秒内的“我是……”也算
    added = _friend_added.get(int(qq))
    return bool(added and abs((msg_time or time.time()) - added) <= 20 and t.startswith("我是") and len(t) <= 30)


async def _is_friend_request(event) -> bool:
    return getattr(event, "post_type", "") == "request" and getattr(event, "request_type", "") == "friend"


friend_request = on_request(rule=_is_friend_request, priority=1, block=False)


@friend_request.handle()
async def _(event):
    d = _friend_reqs()
    d[str(event.user_id)] = {"comment": str(getattr(event, "comment", "") or "")[:100], "at": time.time()}
    _save_friend_reqs(d)


async def _is_friend_add(event: NoticeEvent) -> bool:
    return getattr(event, "notice_type", "") == "friend_add"


friend_add_notice = on_notice(rule=_is_friend_add, priority=1, block=False)


@friend_add_notice.handle()
async def _(event: NoticeEvent):
    _friend_added[int(event.user_id)] = float(event.time or time.time())
    d = _friend_reqs()
    if str(event.user_id) in d:
        d[str(event.user_id)]["added"] = float(event.time or time.time())
        _save_friend_reqs(d)
    logger.info(f"加了新好友：{event.user_id}")


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
    note_arrival(f"private_{event.user_id}", event.message_id)
    if quiet_left() > 0:        # 安静期间收到的私聊不算“看过”，安静完了会补回
        return
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
        # 加好友的验证消息、“已成功添加为好友”这种系统提示不补
        unread = [m for m in unread if not is_friend_verify(qq, message_to_text(_seg_list(m.get("message"))), m.get("time", 0))]
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


def start_catchup(bot: Bot) -> None:
    """（安静期过了之后）补回未读私聊，补完再开始记心跳（免得心跳把离线时间盖掉）"""
    global _catchup_task
    if not cfg.catchup_enabled:
        return

    async def run():
        if quiet_left() > 0:
            await asyncio.sleep(quiet_left())
        try:
            n = await catch_up(bot)
            if n:
                logger.info(f"已补回 {n} 个人的未读私聊")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"补回未读出错：{e}")
        global _heartbeat_task
        if _heartbeat_task is None or _heartbeat_task.done():
            _heartbeat_task = asyncio.create_task(_heartbeat_loop())

    _catchup_task = asyncio.create_task(run())


@get_driver().on_bot_connect
async def _on_connect(bot: Bot):
    global _sticker_task, _offline_alert_task
    if _read_offline().get("kicked"):      # 上次是被踢下线的：先安静一会儿
        start_quiet()
    if cfg.offline_alert and (_offline_alert_task is None or _offline_alert_task.done()):
        _offline_alert_task = asyncio.create_task(send_offline_alert(bot))
    if cfg.sticker_enabled:
        if _sticker_task:
            _sticker_task.cancel()
        _sticker_task = asyncio.create_task(_sticker_loop(bot))
    start_catchup(bot)


@get_driver().on_bot_disconnect
async def _(bot: Bot):
    global _heartbeat_task, _sticker_task
    if cfg.offline_alert:
        note_offline(False, "连接断开")
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


# ------------------------------------------------------------------ 长期记忆：攒够一批却没整理的（之前失败了、或者重启前没来得及），定时补做
_memory_task: "asyncio.Task | None" = None


async def _memory_retry_loop() -> None:
    await asyncio.sleep(60)                        # 开机先缓一分钟，再查一次
    while True:
        try:
            n = ltm.retry_pending()
            if n:
                logger.info(f"长期记忆：补做 {n} 个会话的整理")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查待整理的长期记忆出错：{e}")
        await asyncio.sleep(cfg.memory_retry_minutes * 60)


@get_driver().on_shutdown
async def _():
    for t in (_letter_task, _heartbeat_task, _catchup_task, _sticker_task, _label_task, _bubble_task, _memory_task, _nudge_task, _sleep_task):
        if t:
            t.cancel()



@get_driver().on_startup
async def _():
    global _kb, _fc, _letter_task, _bubble_task, _memory_task, _nudge_task, _sleep_task
    if cfg.sleep_enabled:
        _sleep_task = asyncio.create_task(_sleep_loop())
    if cfg.nudge_enabled and cfg.enable_private:
        _nudge_task = asyncio.create_task(_nudge_loop())
    if cfg.memory_enabled and cfg.memory_retry_minutes > 0 and cfg.deepseek_api_key:
        _memory_task = asyncio.create_task(_memory_retry_loop())
    if tagger:
        tagger.start()     # 后台准备角色识别模型（第一次会下载），不耽误启动
    if cfg.letter_enabled and cfg.enable_private:
        _letter_task = asyncio.create_task(_letter_loop())
    if cfg.bubble_enabled and cfg.enable_group:
        _bubble_task = asyncio.create_task(_bubble_loop())
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
    if cfg.fact_check:
        _fc = await asyncio.to_thread(
            factcheck.load, BOT_DIR / cfg.knowledge_summary_dir, BOT_DIR / cfg.knowledge_characters_file,
            BOT_DIR / cfg.knowledge_places_file, _persona,
        )
    if not cfg.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，机器人会提示未配置")
    year = str(datetime.now(peak.BEIJING).year)
    if cfg.peak_enabled and not any(d.startswith(year) for d in list(peak.HOLIDAYS_2026) + list(cfg.peak_holidays)):
        logger.warning(f"高峰时段：没有 {year} 年的法定节假日数据，节假日会被当成工作日高峰。"
                       f"请在 .env 的 PEAK_HOLIDAYS 里补上，例如 PEAK_HOLIDAYS=[\"{year}-01-01\"]")
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
