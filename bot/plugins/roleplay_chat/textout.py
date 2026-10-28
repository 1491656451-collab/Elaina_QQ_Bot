"""发出去之前的清理（10/22 从 __init__.py 拆出来，代码原样搬过来，没改；见《发前清理清单》）：
去掉标记和提示说明、括号动作、替别人说话和冒出来的外文、半截话、出戏的句子、表情和语音标记、拆成几条"""
import random
import re

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent

from . import textin
from .core import cfg, ltm
from .oocheck import ooc_words
from .stickers import normalize as sticker_normalize
from .textin import sender_label


_PREFIX_RE = re.compile(r"^\s*(?:【[^】]{1,40}】|[^\s【】]{1,16}\s*→\s*[^\s：:]{1,16}\s*[:：])\s*[:：]?\s*")


# 语音标记（10/20）：她可能写「【传声】」（照着提示的标题写）、「[发了语音] ……」（照着聊天记录写）、
# 「（发了一段语音）」「（用传声魔法）」这类旁白，或者把「[传声]」写在第二行。这些都不能当文字发出去：
# 一律去掉；聊天时（keep_sticker）统一换成开头的「[传声]」，算她想用语音（只有本轮给了【传声】提示才真的念）
# 10/22：外面可能还套一层「」『』（照人设里“写「[传声]」”的写法原样抄），一起去掉，免得剩下空的「」
_VOICE_TAG_ANY_RE = re.compile(r"(?:[「『]\s*)?[\[【［]\s*(?:传声|发了?一?[段条句]?语音)\s*[\]】］](?:\s*[」』])?\s*")
# 10/22 23:13 测试里写成了「传声」（只有直角引号、没有方括号）。只认行首的：句子中间的“叫「传声」的魔法”是正常说话，不动
_VOICE_CORNER_RE = re.compile(r"(?m)^[ \t]*[「『]\s*传声\s*[」』][ \t]*")


_VOICE_PAREN_RE = re.compile(r"[（(]\s*(?:发了?|用了?|传了?|开始)?\s*(?:一[段条句]|个)?\s*(?:语音|传声(?:魔法)?)\s*(?:消息|中|过去)?\s*[）)]\s*")

# 10/23（语音 agent 同意，用户定的）：只有「[传声]」这类写法算她想发语音；“[发了语音] ……”“（发了一段语音）”“（用传声魔法）”
# 是照着聊天记录抄的格式或旁白，只去掉、照常发文字。依据：回归_1023_0139 里这种写法 8 条，全在“刚发过语音”的两道题（V04、V14），
# 大多是在推掉或吐槽（“够了吧，剩下的留到我下次心情好的时候”），当成想发就成了用声音说“不给”；10/22 的 320 条里答应时一次都没这么写
_VOICE_WISH_TAG_RE = re.compile(r"(?:[「『]\s*)?[\[【［]\s*传声\s*[\]】］](?:\s*[」』])?\s*")
_VOICE_NARR_TAG_RE = re.compile(r"(?:[「『]\s*)?[\[【［]\s*发了?一?[段条句]?语音\s*[\]】］](?:\s*[」』])?\s*")


def take_voice_marks(text: str) -> tuple[str, bool]:
    """去掉回复里所有的语音标记，返回（文字, 她想用语音）。
    算想发的：「[传声]」【传声】［传声］，开头的「传声」『传声』；只去掉、不算想发的：“[发了语音]”“（发了一段语音）”“（用传声魔法）”、开头的「[语音]」"""
    wish = False
    m = _VOICE_MARK_RE.match(text)
    if m:
        text, wish = text[m.end():], True
    for rx in (_VOICE_WISH_TAG_RE, _VOICE_CORNER_RE):
        text, n = rx.subn("", text)
        wish = wish or n > 0
    for rx in (_VOICE_NARR_TAG_RE, _VOICE_PAREN_RE):
        text = rx.sub("", text)
    return text.strip(), wish


# 10/20 13:19 线上：她把一整句话写在括号里发出去（“（说起来，隔了两周，你又画了张什么送来？）”），
# 像是学长期记忆里“（2 周前的事）”这种括号注释。括号里的心声（“（这人还真敢说啊。）”，9/29 定的）照旧留着；
# 只有整行都在括号里、而且是在跟对方说话的（有“你”，又是问句或“说起来/对了/话说”开头）才去掉括号照常发。
# 句子中间的小括号（“好吧（小声）”）、颜文字、纯动作（上面另外去掉）不动
_PAREN_LINE_RE = re.compile(r"^([ \t]*)[（(]([^（）()\n]+)[）)][ \t]*$", re.M)


def _unwrap_paren_line(m: "re.Match") -> str:
    inner = m.group(2).strip()
    to_you = "你" in inner and (re.search(r"[？?]", inner) or re.match(r"[…\s]*(说起来|对了|话说|顺便)", inner))
    return inner if to_you and len(re.findall(r"[\u4e00-\u9fff]", inner)) >= 4 else m.group(0)


# 10/20 13:26 线上：她写了「[传声][name=伊蕾娜]厉害啊。……」，“name”被当成没人提过的外文，从第一句起截掉，整条没发。
# 这种英文键名的伪标签（[name=…]、[at:qq=…]、[CQ:…]）不是她要说的话，先去掉。中文的「[表情:情绪]」不在此列
_PSEUDO_TAG_RE = re.compile(r"[\[［](?:CQ:)?[A-Za-z_]{1,16}\s*[=:：][^\]］\n]{0,80}[\]］]")


# 10/23 传声测试：她回完“嗯，晚安。”又另起一行写“（过了 X）今天做了什么事情呀，想听”——照着聊天记录里的时间标记
# （聊天规则里写着“「（过了 X）」表示中间隔了这么久”）替对方写了下一句。另起一行、用这种时间标记开头的，从那行起全截掉。
# 群里旁听记录的“（N 秒后）”同理。第一行不管（那是她整条回复的开头，交给下面的动作括号处理）
# 只截“标记后面同一行还接着写话”的（聊天记录里对方的话就是这个格式）；单独一行的“（过了十几秒）”是她自己的停顿，照旧当动作括号去掉（10/20）
_TIME_MARK_LINE_RE = re.compile(r"\n[ \t]*[（(]\s*(?:过了|隔了|\d+\s*(?:秒|分钟?|小时|天)后)[^（）()\n]{0,12}[）)][ \t]*(?=[^\s])")


def clean_reply(text: str, truncated: bool = False, keep_sticker: bool = False) -> str:
    done: list[str] = []                  # 10/23：这次清理改了什么，最后汇总写一行日志（没改就不写）

    def step(name: str, before: str, after: str) -> str:
        if after != before:
            done.append(name)
        return after

    raw = text.strip()
    m = _TIME_MARK_LINE_RE.search(raw)
    text = step("替对方续写（时间标记）", raw, raw[:m.start()].strip() if m else raw)
    text = step("伪标签", text, _PSEUDO_TAG_RE.sub("", text))
    before = text
    text, voice_wish = take_voice_marks(text)
    if voice_wish or text != before.strip():
        done.append("语音标记")
    text = step("开头前缀", text, _PREFIX_RE.sub("", text))
    text = step("重名记号", text, re.sub(r"(?<=[^\s\d#])#\d{1,2}(?![\d])", "", text))     # 重名记号“小明#2”：她说出来时去掉
    text = step("提示抄进括号", text, _META_PAREN_RE.sub("", text).strip())                   # 抄进来的提示说明
    no_action = re.sub(r"\n{3,}", "\n\n", _ACTION_PAREN_RE.sub("", text)).strip()
    if no_action:                         # 括号里写的纯动作（“（过了两秒）”“（打了个哈欠）”）：去掉；整条只有动作就不动
        text = step("动作括号", text, no_action)
    text = step("整句括号", text, _PAREN_LINE_RE.sub(_unwrap_paren_line, text))   # 整行写在括号里、其实是在问对方的话：去掉括号照常说
    if not keep_sticker:                  # 写信、空间评论这些地方发不了表情：模型写了表情标记就去掉
        text = step("表情/占位符", text, _FAKE_MEDIA_RE.sub("", _STICKER_RE.sub("", text)).strip())
    if len(text) >= 2 and text[0] in "“\"" and text[-1] in "”\"":
        text = step("整句引号", text, text[1:-1])
    if truncated:
        # 回复被长度上限截断了：切到最后一个完整句子，免得发出半句话
        cut = max(text.rfind(c) for c in "。！？!?…~）」")
        if cut >= len(text) // 3:
            text = step("截断补句", text, text[: cut + 1])
    text = text.strip()
    if keep_sticker and voice_wish and text:
        text = "[传声]" + text            # 交给 split_voice_mark 去认
    if done:
        logger.info(f"发前清理：{'、'.join(done)}（{raw[:30]!r} → {text[:30]!r}）")
    return text


_PUNCT_ONLY_RE = re.compile(r"[…。！？!?~，,\s]+")


def _merge_punct(parts: list[str]) -> list[str]:
    """只剩标点的一截（“是喔。……你那语气”拆出来的“……”）并到下一截开头；最后一截只剩标点就并到上一截（10/28）"""
    out: list[str] = []
    for x in parts:
        if out and _PUNCT_ONLY_RE.fullmatch(out[-1]):
            out[-1] += x
        else:
            out.append(x)
    if len(out) > 1 and _PUNCT_ONLY_RE.fullmatch(out[-1]):
        tail = out.pop()
        out[-1] += tail
    return out


def split_bubbles(reply: str) -> list[str]:
    """把一次回复拆成几条消息：优先按换行；没换行时按句子随机拆；最多 max_bubbles 条"""
    if not cfg.multi_message:
        return [reply]
    parts = [p.strip() for p in reply.split("\n") if p.strip()]
    if len(parts) == 1 and random.random() < cfg.split_prob:
        # 10/28：省略号“……”是两个“…”，以前在两个“…”中间也会断开，多出一条只有“…”的消息（10/27、10/28 线上都有）；
        # 连着的“…”算一个整体，只在最后一个后面断
        sents = _merge_punct([s.strip() for s in re.split(r"(?<=[。！？!?~])|(?<=…)(?!…)", reply) if s.strip()])
        # 太短的碎句（比如“嗯。”后面紧跟正文）有一半概率单独成条，其余合并
        if 1 < len(sents) <= 6:
            parts = sents
    parts = _merge_punct(parts)                # 她自己换行写的“……”单独一行也并到下一行（10/28 16:17 连着两条“…”）
    if len(parts) > cfg.max_bubbles:           # 太多就把后面的合并
        parts = parts[: cfg.max_bubbles - 1] + ["".join(parts[cfg.max_bubbles - 1 :])]
    out = []
    for p in parts:
        if p.endswith("。") and not p.endswith("……。") and random.random() < cfg.drop_period_prob:
            p = p[:-1]
        if p:
            out.append(p)
    return out or [reply]


# 模型偶尔把提示里的说明抄进回复，比如“（对方和你不太熟，礼貌打个招呼就行。）你好。”：这种括号整段去掉
# 括号里写的纯动作、舞台说明（9/29 23:44）：“（过了两秒）”“（停顿了一下）”“（打了个哈欠）”“（不接话，甩一张……）”。
# 括号心声（“（这人还真敢说啊。）”“（悄悄心动了一下）”）是她的说话方式，不动：只去掉以动作开头、不带“我 / 你”的短括号
_ACTION_PAREN_RE = re.compile(r"[（(][…\s]*(?:过了\s*(?:[一两三几半十\d]+\s*(?:秒钟?|分钟?|个?小时|个?钟头)|好?一会儿?|一阵子?|很久)|停顿|顿了|沉默|打了?个?哈欠|叹了?口?气|耸了?耸?肩|别开[眼脸视头]|"
                              r"扭过头|转过[头身]|笑了笑|不接话|甩了?一张|挑了?挑?眉|眨了?眨?眼|揉了?揉?眼)[^（）()我你]{0,14}[）)]")


_META_PAREN_RE = re.compile(r"[（(][^（）()]{0,60}(?:对方|这轮|回复|提示|系统|规则|人设)[^（）()]{0,60}[）)]")


# 调模型时一写到“【”就停：她的回复里本来不该有“【名字】”，写出来多半是开始照着聊天记录的格式替别人说话了（10/01）
# 10/23 改成“另起一行的【”才停：以前她要是照着每轮提示的标题用“【传声】”开头，第一个字就停住，整条回复是空的，
# 对方要语音时她一声不吭（《发前清理清单》5.1）。开头的“【传声】”“【名字】”交给 clean_reply 去掉；
# 说完另起一行替对方说“【名字】……”照样停住；一行中间冒出来的“【名字】”由 cut_derailed 从那句起截掉
CHAT_STOP = ["\n【"]


_LATIN_RE = re.compile(r"[A-Za-z]{2,}")


_LATIN_OK = {"ok", "orz"}


def cut_derailed(reply: str, context: str, names=()) -> str:
    """模型写着写着开始替对方说话：9/30 22:44 她回完“先去看看书吧”，接着写了“AAA记忆回收 / 好 谢谢”——
    “AAA记忆回收”是对方的群昵称，后面是替对方写的下一句。从这一句起全部截掉。认两种：
    ① 照聊天记录的格式另起一段：“名字：……”“【名字】……”（单独叫一声对方的名字不算，那是她记得对方；10/01 用户说可以）；
    ② 冒出对方和最近聊天都没出现过的外文（她只说中文；对方的名字里有字母的不算）。
    10/23：外文那句要是正常的中文句子里夹了个外文词（中文 6 个字以上，比如“这趟还挺 worthwhile 的”），只删这一句、后面照留；
    中文很少的（“AAA记忆回收”这种像名字的一行）还是从这句起全截掉，后面多半是在替人说话"""
    names = [n.strip() for n in names if n and len(n.strip()) >= 2]
    seen = {w.lower() for w in _LATIN_RE.findall(context + "\n" + "\n".join(names))} | _LATIN_OK | \
        {w.lower() for n in cfg.smart_names for w in _LATIN_RE.findall(n)}
    parts = re.split(r"(?<=[。！？!?…~\n])", reply)
    keep = []
    for i, part in enumerate(parts):
        head = part.strip()
        speaker = any(re.match(rf"^[【\[［]{re.escape(n)}[】\]］]|^{re.escape(n)}\s*[:：]", head) for n in names)
        # 10/23：停止符改成“另起一行的【”以后，同一行里句子后面冒出的“【群友 → 你】……”不会再被停住，在这里截掉
        speaker = speaker or (i > 0 and bool(re.match(r"^【[^】\n]{1,40}】", head)))
        if speaker:
            return "".join(keep).strip()
        if any(w.lower() not in seen for w in _LATIN_RE.findall(part)):
            if len(re.findall(r"[\u4e00-\u9fff]", part)) < 6:
                return "".join(keep).strip()      # 像名字的一行：后面多半是在替人说话，全截掉
            logger.info(f"回复里夹了没人说过的外文，只删这一句：{head[:40]}")
            continue                              # 正常句子里夹了个外文词：只删这一句
        keep.append(part)
    return reply if len(keep) == len(parts) else "".join(keep).strip()


_ONE_CHAR_OK = set("嗯哦喔噢蛤啊诶欸好行在早对是不哈呵嘿切哼咦唔呃额嗷喵噗")


def is_fragment(reply: str) -> bool:
    """只剩一个字、又不是“嗯 / 哦 / 蛤 / 好”这种正常短回：多半是被“写到【就停”截成了半截（10/01 回归里有一次只回了“中”）"""
    core = re.sub(r"[\s\W_]+", "", reply)
    return len(core) == 1 and core not in _ONE_CHAR_OK


def speaker_names(event: MessageEvent) -> list[str]:
    """这个人可能出现的名字（群名片、昵称、档案里记的名字）和她自己的名字：模型写出这些名字另起一行，多半是在替人说话"""
    s = event.sender
    names = {sender_label(event), getattr(s, "card", None) or "", s.nickname or ""}
    try:
        names.add(str(ltm.get_user(event.user_id).get("name") or ""))
    except Exception:  # noqa: BLE001
        pass
    return [n for n in names | set(textin._her_names()) if n]


def drop_ooc_sentences(reply: str, user_text: str) -> str:
    parts = re.split(r"(?<=[。！？!?…~\n])", reply)
    return "".join(p for p in parts if not ooc_words(p, user_text)).strip()


# 每轮先由程序抽签决定“这轮能不能带表情”；抽中了才在提示里告诉她可以写 [表情:情绪]，程序截掉标记、按情绪挑一张发出去
_STICKER_RE = re.compile(r"[\[【［]\s*(?:发了|甩了)?\s*表情\s*[:：]\s*([^\]】］（(]{1,8})[^\]】］]*[\]】］]")


# 模型模仿别人消息的格式，写出「[图片：表情]」「[表情]」「[动画表情]」之类：这些发出去就是一串文字，要去掉。
# 看得出情绪的（比如「[图片：嫌弃的表情]」）当成想甩表情；看不出的直接删掉
_FAKE_MEDIA_RE = re.compile(
    r"[\[【［]\s*(图片|动画表情|表情包|表情|小画像|画像|语音|视频|图|卡片消息|名片|聊天记录|文件|链接|分享|骰子)\s*(?:[:：]\s*([^\]】］]{0,40}))?\s*(?:（看不懂）)?\s*[\]】］]")


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
                if kind in ("语音", "视频", "卡片消息", "名片", "聊天记录", "文件", "链接", "分享", "骰子") or not inner:
                    continue
                e = next((w for w in re.split(r"[、，,的\s]+", inner) if w and sticker_normalize(w)), None)
                if e:
                    found = [sticker_normalize(e)]
                    break
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text, (found[-1].strip() if found else None)


# 开头的标记（发语音时 split_voice_mark 也用它认）：10/23 起只认“传声”，开头的「[语音]」不算想发（交给占位符清理去掉）
_VOICE_MARK_RE = re.compile(r"^\s*(?:(?:[「『]\s*)?[\[【［]\s*传声\s*[\]】］](?:\s*[」』])?|[「『]\s*传声\s*[」』])\s*")
