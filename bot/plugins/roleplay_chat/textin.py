"""收到的消息怎么变成给她看的文字（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
消息段转文字、名字和重名标记、引用、旁听记录的拼法、叫她的名字；对方的话太长时截短（clip_input，10/22 第 6 步搬来）"""
import json
import re
import time

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment, PrivateMessageEvent

from .core import _SAME_NAMES_FILE, cfg, vision
from .turn_hints import human_gap


def _strip_her_names(text: str) -> str:
    """去掉话里叫她的名字（伊蕾娜小姐、魔女小姐、昵称……），剩下的才是话题"""
    names = sorted({n for n in _her_names() + ["伊蕾娜小姐", "伊蕾娜大人", "伊雷娜"] if n}, key=len, reverse=True)
    for n in names:
        if "之魔女" in n:                 # “灰之魔女”这种称号也可能是话题（“你考灰之魔女那次”），只去掉开头叫人的那种
            text = re.sub(rf"^\s*{re.escape(n)}[，,、!！\s]*", " ", text)
        else:
            text = text.replace(n, " ")
    return re.sub(r"\s+", " ", text).strip()


def seg_text(seg: MessageSegment) -> str:
    if seg.type == "text":
        return seg.data.get("text", "")
    if seg.type == "image":
        return "[图片]"
    if seg.type == "face":
        name = face_name(seg)
        return f"[表情：{name}]" if name else "[表情]"
    if seg.type == "at":
        return f"@{seg.data.get('name') or seg.data.get('qq')}"
    if seg.type in ("record", "video"):
        return "[语音]" if seg.type == "record" else "[视频]"
    if seg.type == "mface":
        return f"[表情：{seg.data.get('summary') or '表情'}]".replace("：[", "：").replace("]]", "]")
    if seg.type == "reply":
        return ""
    if seg.type in ("json", "xml", "ark"):
        kind, title = card_info(seg)
        if kind:
            return f"[{kind}：{title}]" if title else f"[{kind}]"
    if seg.type == "file":
        fname = _short(seg.data.get("name") or seg.data.get("file") or "", 30)
        return f"[文件：{fname}]" if fname and not fname.startswith(("http", "base64")) else "[文件]"
    if seg.type == "dice":
        n = str(seg.data.get("result") or "").strip()
        return f"[骰子：{n} 点]" if n.isdigit() else "[骰子]"
    if seg.type == "forward" and str(seg.data.get("id") or "") in _forward_cache:
        return _forward_cache[str(seg.data.get("id"))]          # 11/06 已经向 NapCat 要到了里面的内容
    # 9/30：别的都看不懂（聊天记录、位置……）。以前直接丢掉，她以为对方什么都没发，接着乱猜
    return f"[{unreadable_kind(seg)}（看不懂）]"


# ---- 11/06 转发的聊天记录：消息里只带一个编号，内容要向 NapCat 另外要（get_forward_msg）。
# 收到时先要来、拼成一行放进缓存，之后 seg_text 直接用；要不到就照旧写“[聊天记录（看不懂）]”
_forward_cache: dict[str, str] = {}
_FORWARD_CACHE_MAX = 300


def _node_segs(node: dict) -> list[MessageSegment]:
    """一条转发里的消息：NapCat 给 message（列表），go-cqhttp 给 content（列表或 CQ 码字符串）"""
    raw = node.get("message")
    if raw is None:
        raw = node.get("content")
    if isinstance(raw, str):
        return list(Message(raw))
    out = []
    for x in raw or []:
        if isinstance(x, dict) and x.get("type"):
            out.append(MessageSegment(x["type"], x.get("data") or {}))
    return out


def render_forward(nodes: list, max_nodes: int | None = None, max_chars: int | None = None, self_id: str = "",
                   self_names: tuple = (), said_by_her=None, name_notes: dict | None = None) -> str:
    """拼成一行：[聊天记录（共 N 条）：甲：……；乙：……]。里面的方括号换成【】，免得和外面的标记混在一起。
    她自己说的（发送人是她的 QQ 或昵称）写成“你自己”，她认得出那是自己说过的话（11/06 用户问的）"""
    max_nodes = max_nodes or cfg.forward_max_nodes
    max_chars = max_chars or cfg.forward_max_chars
    nodes = [n for n in nodes or [] if isinstance(n, dict)]
    lines = []
    for node in nodes[:max_nodes]:
        sender = node.get("sender") or {}
        uid = str(sender.get("user_id") or node.get("user_id") or "")
        nick = str(sender.get("nickname") or node.get("nickname") or "")
        parts, plain = [], []
        for seg in _node_segs(node):
            if seg.type == "forward":
                parts.append("[一段聊天记录]")
            elif seg.type == "reply":
                continue
            else:
                parts.append(seg_text(seg))
                if seg.type == "text":
                    plain.append(seg.data.get("text", ""))
        body = re.sub(r"\s+", " ", "".join(parts)).strip() or "（空）"
        # 认她自己：QQ 号对得上；或者昵称和她一样、而且这句话她确实说过（11/06：聊天记录里没有 QQ 号，
        # 只按昵称会被同名的人冒充；没有文字的（图片、表情）只能按昵称）
        said = "".join(plain).strip()
        if (self_id and uid == str(self_id)) or (nick and nick in self_names and (not said or said_by_her is None or said_by_her(said))):
            who = "你自己"          # 11/06 只写“你”时，她把这段读成转发的人说的（“里面是你自己说的那两句啊”）
        else:
            who = _short(sender.get("card") or sender.get("nickname") or node.get("nickname") or uid or "某人", 12)
            if name_notes and nick in name_notes:
                who += name_notes[nick]      # 11/06 按 QQ 昵称对上了群里的人 / 正在跟她说话的人
        body = body.replace("[", "【").replace("]", "】")
        lines.append(f"{who}：{body if len(body) <= 40 else body[:40] + '…'}")
    if not lines:
        return ""
    text = "；".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip("；") + "……"
    elif len(nodes) > max_nodes:
        text += "；……"
    return f"[聊天记录（共 {len(nodes)} 条）：{text}]"


def _nodes_of(got) -> list:
    """get_forward_msg 返回的几种样子：{"messages": [...]}、{"message": [...]}、直接一个列表"""
    if isinstance(got, list):
        return got
    if isinstance(got, dict):
        for k in ("messages", "message", "content", "nodes"):
            if isinstance(got.get(k), list):
                return got[k]
    return []


def _shape(got) -> str:
    """要不到内容时，记一下 NapCat 回的是什么样子（只记结构，不记内容）"""
    if isinstance(got, dict):
        return "{" + ", ".join(f"{k}:{type(v).__name__}" + (f"[{len(v)}]" if isinstance(v, (list, str)) else "") for k, v in list(got.items())[:20]) + "}"
    return type(got).__name__ + (f"[{len(got)}]" if isinstance(got, (list, str)) else "")


_self_nick: dict[str, str] = {}
_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm(t: str) -> str:
    return _NORM_RE.sub("", t or "")


def _her_texts():
    """她在各个会话里说过的话（先给已经读进来的，再给硬盘上的）。聊天记录只留最近一段"""
    from .history import _hist_path, _histories   # 放在这里，免得两个文件互相 import
    seen = set()
    for k, h in list(_histories.items()):
        seen.add(k)
        for m in h or []:
            if m.get("role") == "assistant":
                yield str(m.get("content") or "")
    try:
        for p in _hist_path("x").parent.glob("*.json"):
            if p.stem in seen:
                continue
            try:
                h = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for m in h or []:
                if isinstance(m, dict) and m.get("role") == "assistant":
                    yield str(m.get("content") or "")
    except Exception:  # noqa: BLE001
        return


def said_by_her(text: str) -> bool:
    """这句话她自己说过没有（转发的聊天记录用）：去掉标点空格后，是她某次说的话的一部分"""
    want = _norm(text)
    if len(want) < 2:
        return True                     # 太短（“嗯”“？”）对不出来，只能按昵称
    return any(want in _norm(t) for t in _her_texts())


# ---- 11/06（识图 agent《交接-截图对聊天记录-聊天-1106》，用户定的：对她在所有聊天里说过的话）
# 聊天截图的描述里，看图模型把每句原话抄在引号里；能对上她说过的话的，后面标“（你说过）”。
# 只在拼给她看的文字里加，不写回识图缓存
_SHOT_QUOTE_RE = re.compile(r"“([^“”]{2,80})”|「([^「」]{2,80})」|\"([^\"]{2,80})\"")
SHOT_MIN_CHARS = 4               # 去掉标点后不到 4 个字的（“有事？”“下午好”）不对，容易对错
SHOT_SIMILAR = 0.8               # 看图模型偶尔抄错一两个字：相似度到这个就算


def her_quote(q: str, texts: list[str]) -> bool:
    import difflib
    qn = _norm(q)
    if len(qn) < SHOT_MIN_CHARS:
        return False
    for t in texts:
        if qn in _norm(t):
            return True
        for line in str(t).split("\n"):
            ln = _norm(line)
            if len(ln) >= SHOT_MIN_CHARS and (ln in qn or difflib.SequenceMatcher(None, ln, qn).ratio() >= SHOT_SIMILAR):
                return True
    return False


def mark_her_lines(desc: str) -> str:
    """截图描述里能对上她说过的话的原话，后面加“（你说过）”"""
    if not desc or not _SHOT_QUOTE_RE.search(desc):
        return desc
    texts = None

    def sub(m: re.Match) -> str:
        nonlocal texts
        q = next(g for g in m.groups() if g is not None)
        if len(_norm(q)) < SHOT_MIN_CHARS:
            return m.group(0)
        if texts is None:
            texts = list(_her_texts())
        return m.group(0) + "（你说过）" if her_quote(q, texts) else m.group(0)
    return _SHOT_QUOTE_RE.sub(sub, desc)


async def self_names(bot) -> tuple:
    """她自己的 QQ 昵称（问一次 NapCat 就记住）"""
    sid = str(getattr(bot, "self_id", "") or "")
    if sid not in _self_nick:
        try:
            info = await bot.call_api("get_login_info")
            _self_nick[sid] = str((info or {}).get("nickname") or "")
        except Exception:  # noqa: BLE001
            return ()
    return (_self_nick[sid],) if _self_nick.get(sid) else ()


async def fetch_forward(bot, fid: str, outer_mid=None, name_notes: dict | None = None) -> str:
    """向 NapCat 要这段聊天记录的内容，拼好放进缓存；要不到返回空。
    先 get_forward_msg（id / message_id 两种参数）；还不行就 get_msg 拿外面那条消息，看转发段里有没有带着内容"""
    if not fid:
        return ""
    if fid in _forward_cache:
        return _forward_cache[fid]
    self_id = str(getattr(bot, "self_id", "") or "")
    nodes, shapes = [], []
    # 11/06 19:36 线上：拿转发编号去要，NapCat 回的是空列表。NapCat 的 get_forward_msg 认的多半是外面那条消息的编号，也试一下
    tries = [{"id": fid}, {"message_id": fid}]
    if outer_mid is not None:
        tries += [{"message_id": outer_mid}, {"id": str(outer_mid)}]
    for kw in tries:
        try:
            got = await bot.call_api("get_forward_msg", **kw)
        except Exception as e:  # noqa: BLE001
            shapes.append(f"get_forward_msg({kw}) 出错：{str(e)[:60]}")
            continue
        nodes = _nodes_of(got)
        if nodes:
            break
        shapes.append(f"get_forward_msg({kw}) 回的是 {_shape(got)}")
    if not nodes and outer_mid is not None:
        try:
            got = await bot.call_api("get_msg", message_id=outer_mid)
            segs = (got or {}).get("message") if isinstance(got, dict) else None
            for x in segs or []:
                if isinstance(x, dict) and x.get("type") == "forward":
                    nodes = _nodes_of((x.get("data") or {}).get("content"))
                    if nodes:
                        break
            if not nodes:
                fwd = [x for x in segs or [] if isinstance(x, dict) and x.get("type") == "forward"]
                shapes.append(f"get_msg 回的是 {_shape(got)}，消息段 {[x.get('type') for x in segs or [] if isinstance(x, dict)]}"
                              + (f"，转发段里有 {_shape(fwd[0].get('data'))}" if fwd else ""))
        except Exception as e:  # noqa: BLE001
            shapes.append(f"get_msg 出错：{str(e)[:60]}")
    text = render_forward(nodes, self_id=self_id, self_names=await self_names(bot) if nodes else (), said_by_her=said_by_her,
                          name_notes=name_notes)
    if not text:
        logger.info(f"聊天记录：要内容没要到（{fid}）：{'；'.join(shapes)[:800]}")
        return ""
    if len(_forward_cache) >= _FORWARD_CACHE_MAX:
        _forward_cache.pop(next(iter(_forward_cache)))
    _forward_cache[fid] = text
    logger.info(f"聊天记录：{text[:60]}")
    return text


# ---- 11/06 聊天记录里的人和群里的人对上（用户定的：只按 QQ 昵称对，对上了告诉她这个人在群里的群名片）
_members: dict[int, tuple[float, dict[str, list[dict]]]] = {}   # 群号 -> (什么时候要的, QQ 昵称 -> 群成员)
MEMBERS_TTL = 1800


async def _group_members(bot, gid: int) -> dict[str, list[dict]]:
    """群成员按 QQ 昵称分好（半小时内不重复要）"""
    now = time.monotonic()
    got = _members.get(gid)
    if got and now - got[0] < MEMBERS_TTL:
        return got[1]
    by_nick: dict[str, list[dict]] = {}
    try:
        for m in await bot.call_api("get_group_member_list", group_id=gid) or []:
            if isinstance(m, dict) and m.get("nickname"):
                by_nick.setdefault(str(m["nickname"]), []).append(m)
    except Exception as e:  # noqa: BLE001
        logger.info(f"聊天记录：要群成员没要到（群{gid}）：{e}")
        return {}
    _members[gid] = (now, by_nick)
    return by_nick


async def name_notes_for(bot, event: MessageEvent) -> dict[str, str]:
    """聊天记录里的名字（QQ 昵称）→ 跟在名字后面的说明：
    正在跟她说话的这个人 →“（就是正在跟你说话的这个人）”；群里的人 →“（群里的群名片是“……”）”/“（群里的人）”。
    同一个昵称群里有两个人以上，不敢认，不写"""
    notes: dict[str, str] = {}
    try:
        if isinstance(event, GroupMessageEvent):
            for nick, ms in (await _group_members(bot, event.group_id)).items():
                if len(ms) != 1 or str(ms[0].get("user_id")) == str(event.self_id):
                    continue
                card = str(ms[0].get("card") or "").strip()
                notes[nick] = f"（群里的群名片是“{_short(card, 12)}”）" if card and card != nick else "（群里的人）"
        me = str(getattr(event.sender, "nickname", "") or "").strip()
        if me:
            notes[me] = "（就是正在跟你说话的这个人）"
    except Exception as e:  # noqa: BLE001
        logger.info(f"聊天记录：对名字出错，这次不对：{e}")
    return notes


async def prefetch_forwards(bot, event: MessageEvent) -> None:
    """这条消息（和它引用的消息）里有转发的聊天记录：先把内容要来，后面拼文字时就能看到"""
    if not cfg.forward_read:
        return
    msgs = [(event.get_message(), getattr(event, "message_id", None))]
    reply = getattr(event, "reply", None)
    if reply is not None and getattr(reply, "message", None):
        msgs.append((reply.message, getattr(reply, "message_id", None)))
    if not any(seg.type == "forward" for msg, _ in msgs for seg in msg):
        return
    notes = await name_notes_for(bot, event)
    for msg, mid in msgs:
        for seg in msg:
            if seg.type != "forward":
                continue
            fid = str(seg.data.get("id") or "")
            inline = seg.data.get("content")
            if fid and fid not in _forward_cache and isinstance(inline, list) and inline:
                text = render_forward(inline, self_id=str(getattr(bot, "self_id", "") or ""),
                                      self_names=await self_names(bot), said_by_her=said_by_her,
                                      name_notes=notes)   # 有的版本直接带着内容
                if text:
                    _forward_cache[fid] = text
                    logger.info(f"聊天记录（消息里带着）：{text[:60]}")
                    continue
            try:
                await fetch_forward(bot, fid, mid, notes)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"聊天记录：出错，这次不看：{e}")


def _short(text, n: int) -> str:
    """卡片标题、文件名这些：去掉换行和格式符号，太长截断"""
    t = re.sub(r"[\s\[\]【】［］]+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def face_name(seg: MessageSegment) -> str:
    """QQ 小表情的名字（“/捂脸”→“捂脸”）；消息里没带就返回空"""
    raw = seg.data.get("raw")
    text = ""
    if isinstance(raw, dict):
        text = raw.get("faceText") or ""
    elif raw:
        m = re.search(r"faceText['\"]?\s*[:=]\s*['\"]([^'\"]+)", str(raw))
        text = m.group(1) if m else ""
    text = str(text or seg.data.get("faceText") or seg.data.get("text") or "").strip().lstrip("/").strip("[]")
    return _short(text, 8)


def card_info(seg: MessageSegment) -> tuple[str, str]:
    """卡片消息：名片 → ("名片", 昵称)；分享（视频、音乐、小程序、链接）→ ("分享", "来源「标题」")；认不出 → ("", "")。
    只取标题，不打开链接"""
    raw = seg.data.get("data")
    if seg.type == "xml":
        m = re.search(r"<title>(.*?)</title>", str(raw), re.S) or re.search(r'brief="([^"]*)"', str(raw))
        return ("分享", _short(m.group(1), 40)) if m else ("", "")
    try:
        j = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        j = None
    if not isinstance(j, dict):
        return "", ""
    meta = j.get("meta") if isinstance(j.get("meta"), dict) else {}
    inner = next((v for v in meta.values() if isinstance(v, dict)), {})
    prompt = re.sub(r"^\s*\[[^\]]*\]\s*", "", str(j.get("prompt") or "")).strip()
    if j.get("view") == "contact" or "contact" in str(j.get("app", "")):
        nick = inner.get("nickname") or inner.get("name") or ""
        if not nick and "：" in prompt:
            nick = prompt.split("：", 1)[1]
        return "名片", _short(nick, 16)
    if "detail_1" in meta:                 # 小程序（B 站、QQ 音乐小程序……）：title 是哪个小程序，desc 是内容
        src, name = inner.get("title", ""), inner.get("desc", "")
    else:                                  # 普通分享：tag 是来源，title 是标题
        src, name = inner.get("tag", "") or inner.get("source", ""), inner.get("title", "") or inner.get("desc", "")
    src, name = _short(src, 12), _short(name or prompt, 40)
    if not (src or name):
        return "", ""
    return "分享", (f"{src}「{name}」" if src and name and src != name else (name or src))


_UNREADABLE_KINDS = {"json": "卡片消息", "xml": "卡片消息", "ark": "卡片消息", "markdown": "卡片消息",
                     "forward": "聊天记录", "node": "聊天记录", "file": "文件", "contact": "名片",
                     "share": "链接", "music": "音乐分享", "location": "位置", "poke": "戳一戳",
                     "dice": "骰子", "rps": "猜拳"}


def unreadable_kind(seg: MessageSegment) -> str:
    """她看不懂的消息是什么（只说个大概，不看里面写了什么）"""
    if seg.type == "json" and re.search(r'"view"\s*:\s*"contact"|com\.tencent\.contact', str(seg.data.get("data", ""))):
        return "名片"
    return _UNREADABLE_KINDS.get(seg.type, "消息")


def message_to_text(msg: Message, drop_at: bool = False) -> str:
    """drop_at=True：去掉 @ 段（群聊里 @ 谁单独写在说话人后面，不混进正文）。
    群消息请用 group_body：只去掉开头、结尾的 @，句子中间的 @ 留在正文里"""
    return "".join(seg_text(seg) for seg in msg if not (drop_at and seg.type == "at")).strip()


def edge_at_positions(msg: Message) -> set[int]:
    """开头、结尾的 @（中间只隔着空格）在第几段：这些 @ 是“在跟谁说”；句子中间的 @ 只是提到这个人"""
    def blank(seg) -> bool:
        return seg.type == "text" and not seg.data.get("text", "").strip()
    idx: set[int] = set()
    for order in (range(len(msg)), range(len(msg) - 1, -1, -1)):
        for i in order:
            if msg[i].type == "at":
                idx.add(i)
            elif not blank(msg[i]):
                break
    return idx


def edge_ats(event: MessageEvent) -> list[MessageSegment]:
    msg = event.get_message()
    return [msg[i] for i in sorted(edge_at_positions(msg))]


def middle_at_me(event: MessageEvent) -> bool:
    """句子中间 @ 了她（“这个问问@伊蕾娜 吧”）：算点了她的名。开头结尾的 @ 框架已经认成在跟她说了"""
    msg = event.get_message()
    edge = edge_at_positions(msg)
    return any(seg.type == "at" and str(seg.data.get("qq")) == str(event.self_id) and i not in edge
               for i, seg in enumerate(msg))


def group_body(event: MessageEvent) -> Message:
    """群消息的正文：开头、结尾的 @ 去掉（写在说话人后面的“→”里），句子中间的 @ 换成“@名字”留在原处。
    9/30：以前所有 @ 都去掉、名字挪到“→”后面，“我觉得@小明 说得对”变成“【某某 → 小明】我觉得 说得对”"""
    msg = event.get_message()
    if not isinstance(event, GroupMessageEvent):
        return msg
    edge = edge_at_positions(msg)
    out = Message()
    for i, seg in enumerate(msg):
        if seg.type != "at":
            out.append(seg)
            continue
        if i in edge:
            continue
        qq = str(seg.data.get("qq"))
        if qq == str(event.self_id):
            name = (list(cfg.smart_names) or ["伊蕾娜"])[0]
        elif qq == "all":
            name = "全体成员"
        else:
            name = _display_name(name_label(event.group_id, qq, seg.data.get("name") or qq))
        out.append(MessageSegment.text(f"@{name}"))
    return out


_BOT_SEGMENTS = {"markdown", "json", "xml", "ark", "keyboard", "button"}


_BOT_SEGMENTS_PRIVATE = {"markdown", "keyboard", "button"}   # 私聊里好友转来的卡片、名片是人发的，不算机器人


def is_bot_like(event: MessageEvent, allow_cards: bool = False) -> bool:
    """别的机器人发的（Markdown、卡片消息），或者在忽略名单里的人：完全不理。
    私聊里的卡片 / 名片是好友转的：照常回（只看得到标题），9/30 起；allow_cards：群旁听里也收下卡片（只显示标题）"""
    if event.user_id in cfg.ignore_users:
        return True
    kinds = _BOT_SEGMENTS_PRIVATE if allow_cards or isinstance(event, PrivateMessageEvent) else _BOT_SEGMENTS
    return any(seg.type in kinds for seg in event.get_message())


def talking_to_others(event: MessageEvent) -> str | None:
    """群里 @ 了别人、或者回复的是别人的消息：返回那个人的名字（她只是旁观者）；否则 None"""
    if not isinstance(event, GroupMessageEvent):
        return None
    self_id = str(event.self_id)
    at_others = [seg for seg in edge_ats(event)         # 句子中间 @ 别人只是提到他，不算在跟他说
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
    for seg in edge_ats(event):           # 只有开头、结尾的 @ 算“在跟谁说”；中间的 @ 留在正文里（group_body）
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


# 叫她的只是一句“对吧，伊蕾娜小姐”“你说呢”：真正的话在同一个人紧挨着的上一两条里（9/30 13:07：
# “昨天卖惨的那个人被狠狠讨厌了”“对吧伊蕾娜小姐”，她只看到后一句，回了句不相干的）
_TAG_ONLY_RE = re.compile(r"(?:那|所以|是)?(?:对吧|是吧|对不对|对不|对吗|是不是|没错吧|是这样吧|你说呢|你说是不是|你觉得呢|你怎么看|你看呢)"
                          r"[？?！!。.~…啊呀嘛呢]*")


_HEAD_ONLY_RE = re.compile(r"^【[^】→]*】")      # 没指定对象的说话人前缀（有“→”的是在跟别人说，不算）


AT_ONLY_TEXT = "（@了你一下，没说话）"


def is_call_only(text: str) -> bool:
    """只是叫她一声：@ 了一下没说话、只有她的名字（“伊蕾娜？”）、“在吗 / 在不在”"""
    if text.strip() == AT_ONLY_TEXT:
        return True
    bare = re.sub(r"[\s，,、。.!！~～?？…@]", "", _strip_her_names(text))
    return not bare or bool(_CALL_ONLY_RE.fullmatch(bare))


def pull_own_lead(buf: list, uid: int, text: str, now: float, window: float = 60.0) -> tuple[list[str], list]:
    """这条只是“对吧 / 你说呢”这种，或者只是叫她一声（“@伊蕾娜”“伊蕾娜？”“在吗”），就把同一个人紧挨着的前几条
    （旁听里、没对别人说、60 秒内）拿出来当成这条的前文——那几句多半是说给她听的（10/01：“今天被老板骂了 / 加班到十点 / @伊蕾娜”）。
    返回（前文正文列表, 剩下的旁听）"""
    core = re.sub(r"[\s，,、]", "", _strip_her_names(text))
    if not (is_call_only(text) or (core and _TAG_ONLY_RE.fullmatch(core))):
        return [], buf
    lead = []
    i = len(buf)
    while i > 0:
        u, _, line, ts = buf[i - 1]
        m = _HEAD_ONLY_RE.match(line)
        if u != uid or now - ts > window or not m:
            break
        lead.insert(0, line[m.end():])
        i -= 1
    return lead, buf[:i]


CALLER_LEAD_MARK = "（↓ 下面几句是叫你的人刚才说的）"


WATCH_GAP_MIN = 20          # 旁听里相邻两句隔了这么多秒以上，标一下“（N 秒后）”，帮她看出谁在接谁的话（10/01）


def short_gap(sec: float) -> str:
    return f"{int(sec)} 秒" if sec < 60 else f"{int(sec // 60)} 分钟"


def watch_block(items: list[tuple[float, str]], mark_from: int | None = None, end_ts: float | None = None) -> str:
    """旁听记录：每条（时间, 已经带说话人的一行），隔得久的中间标出来。
    10/01 起：相邻两句隔了 20 秒到 30 分钟的，行首标“（N 秒后）”“（N 分钟后）”，紧挨着说的多半在对话，隔得久的多半是新话题；
    mark_from：从第几条起是叫她的人紧接着说的前文，前面加一行 CALLER_LEAD_MARK；
    end_ts：叫她的那条消息的时间，离最后一句隔得久就在末尾标一句"""
    out, prev = [], None
    for i, (ts, line) in enumerate(items):
        g = gap_line(prev, ts)
        if g:
            out.append(g.strip())
        if mark_from is not None and i == mark_from:
            out.append(CALLER_LEAD_MARK)
        if prev and not g and ts - prev >= WATCH_GAP_MIN:
            line = f"（{short_gap(ts - prev)}后）{line}"
        out.append(line)
        prev = ts
    if end_ts and prev and end_ts - prev >= WATCH_GAP_MIN:
        out.append(f"（又过了 {short_gap(end_ts - prev) if end_ts - prev < 1800 else human_gap(end_ts - prev)}，才是下面这条）")
    return "（群聊旁听记录）\n" + "\n".join(out)


def caller_lead_start(buf: list, uid: int, now: float, window: float = 60.0) -> int | None:
    """旁听的最后几句里，叫她的人自己紧挨着说的（60 秒内、没对别人说、中间没人插话）从第几条开始；没有就 None"""
    i = len(buf)
    while i > 0:
        u, _, line, ts = buf[i - 1]
        if u != uid or now - ts > window or not _HEAD_ONLY_RE.match(line):
            break
        i -= 1
    if i >= len(buf):
        return None
    # 那几句全是图片、表情（“【某某】[图片]”）：没什么可“接上”的，不标（10/01 回放 C02 里标在了一张图上）
    if all(_only_media(buf[j][2]) for j in range(i, len(buf))):
        return None
    return i


def _only_media(line: str) -> bool:
    """旁听里的一行去掉说话人前缀和「[图片]」「[表情：捂脸]」这类方括号后，没有文字"""
    body = _HEAD_ONLY_RE.sub("", line)
    body = re.sub(r"[\[［][^\]］]*[\]］]", "", body)
    return not re.sub(r"[\W_]+", "", body)


QUOTE_MAX_CHARS = 30


def quote_note(event: MessageEvent) -> str:
    """对方回复（引用）了哪句话：“（回复你说的“……”）”，截成 30 字。9/30 以前她看不到引用的原话，
    “我说这个”“对啊”这种不知道在接哪句。引用里有图，另外会看图（rich_text 里）"""
    reply = getattr(event, "reply", None)
    if reply is None or not getattr(reply, "message", None):
        return ""
    quoted = re.sub(r"\s+", " ", message_to_text(reply.message)).strip()
    if not quoted:
        return ""
    has_fwd = any(seg.type == "forward" and str(seg.data.get("id") or "") in _forward_cache for seg in reply.message)
    cap = cfg.forward_max_chars + 30 if has_fwd else QUOTE_MAX_CHARS      # 11/06 引用的是聊天记录：整段给她看，不截成 30 字
    if len(quoted) > cap:
        quoted = quoted[:cap] + "…"
    quoted = clean_body(quoted)
    who = str(getattr(reply.sender, "user_id", ""))
    if who == str(event.self_id):
        return f"（回复你说的“{quoted}”）"
    if who == str(event.user_id):
        return f"（引用了这个人自己之前发的“{quoted}”）"     # 9/30 20:08：原来写“回复自己之前说的”，她会看成“你自己”
    return f"（回复的是“{quoted}”）"      # 群里回复别人：是谁已经写在开头的“→ 某某（回复）”里


def image_datas(event: MessageEvent) -> list[dict]:
    """11/06 这条消息里会被看的那几张图（和 _rich_text 看图的顺序、张数一样：先消息里的，再引用里的）"""
    budget = cfg.vision_max_images
    out = [seg.data for seg in event.get_message() if seg.type == "image"][:budget]
    reply = getattr(event, "reply", None)
    if reply is not None and len(out) < budget:
        out += [seg.data for seg in reply.message if seg.type == "image"][:budget - len(out)]
    return out


async def rich_text(event: MessageEvent, look: bool, drop_at: bool = False) -> str:
    """和 message_to_text 一样，但会“看”图片，把 [图片] 换成 [图片：描述]；引用的消息里有图也会看。
    对方回复（引用）了某句话时，前面注一句回复的是哪句。drop_at=True：去掉 @ 段（群聊里 @ 谁写在说话人后面）"""
    text = await _rich_text(event, look, drop_at)
    note = quote_note(event)
    return f"{note}{text}" if note else text


async def _rich_text(event: MessageEvent, look: bool, drop_at: bool = False) -> str:
    msg = event.get_message()
    if drop_at:
        msg = group_body(event) if isinstance(event, GroupMessageEvent) else Message([seg for seg in msg if seg.type != "at"])
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
            desc = mark_her_lines(await vision.describe(seg.data))     # 11/06 截图里她说过的话标出来
            parts.append(f"[图片：{desc}]" if desc else "[图片]")
        else:
            parts.append(seg_text(seg))
    text = "".join(parts).strip()
    reply = getattr(event, "reply", None)
    if reply is not None and budget > 0:
        quoted = [seg for seg in reply.message if seg.type == "image"][:budget]
        descs = [mark_her_lines(await vision.describe(seg.data)) for seg in quoted]
        descs = [d for d in descs if d]
        if descs:
            text += "（引用了图片：" + "；".join(descs) + "）"
    return text


def sender_name(event: MessageEvent) -> str:
    s = event.sender
    return (getattr(s, "card", None) or s.nickname or str(event.user_id)).strip()


_CALL_ONLY_RE = re.compile(r"(?:在吗|在不在|在么|在嘛|你在吗|有人吗|喂+|诶+|哎+|欸+|嘿+|在？?)")


def clip_input(text: str) -> str:
    if len(text) > cfg.max_input_chars:
        return text[: cfg.max_input_chars] + "……（后面太长，省略了）"
    return text
