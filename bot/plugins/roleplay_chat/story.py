"""查小说资料、核对她讲的往事（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
知识库和记混检查的对象（启动时加载）、按对方的话查回忆、讲往事前后的核对、问“今天的日记”时接上话题。
小说资料本身的检索在 knowledge.py，记混检查在 factcheck.py"""
import json
import re
import time

from nonebot import logger

from . import budget, factcheck, knowledge
from .core import cfg, client
from .prompts import RECALL_RULES, STORY_CHECK_PROMPT
from .textin import _strip_her_names


_kb: "knowledge.KnowledgeBase | None" = None


_fc: "factcheck.FactChecker | None" = None     # 记混检查（人和地方对不对得上）


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
    short = len(q_topic) < 10 and not followup
    chunk_min = cfg.knowledge_min_chunk_score * (1.25 if short else 1.0)
    # 摘要也一样（10/18）：短篇集拆成一篇一条以后，短摘要更容易被一句闲聊碰上（“只是怕你太无聊了”→某个短篇），短句也要更像才带
    sum_min = cfg.knowledge_min_summary_score * (1.25 if short else 1.0)

    def search(q: str) -> list[tuple[float, tuple[str, str, str]]]:
        out = [(s / cfg.knowledge_min_summary_score, (d.label, "摘要", d.content[: cfg.knowledge_summary_chars]))
               for s, d in _kb.search(q, "summary", cfg.knowledge_top_summaries) if s >= sum_min]
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
