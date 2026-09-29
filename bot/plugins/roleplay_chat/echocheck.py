"""对方反问她上一句里的词（9/29 21:48）

她上一句莫名其妙扯到了蘑菇（“少拿我跟蘑菇相提并论”），对方问“诶？蘑菇？哪有蘑菇”。规则和人设都写了
“自己说岔了就认”，她还是常常说成“你把我和蘑菇放一起比”——她上一句读起来像在回应对方，模型就当成对方真提过。
缺的是一个事实：这个词是她自己先说的。这里不调模型，只比字，满足下面三条就在本轮提示里告诉她：

- 对方这条很短，而且是在问她上一句里的某个词：“蘑菇？”“哪有蘑菇”“什么蘑菇”“蘑菇是什么”“怎么突然说蘑菇”；
- 这个词（两个字以上）出现在她上一条回复里；
- 这个词在对方前面几条消息里都没出现过。

她正常讲往事时对方也会这样反问（“梦回之城？那是哪”），所以提示里写了“是正经的话就解释一下”，
不能让她把对的话也收回去。只用标准库，回归测试也直接读这个文件。
"""
from __future__ import annotations

import re

# 22:00 改过说法：原来写“「X」是你自己先说的”，讲往事被问“梦回之城？那是哪”时，有一次她回“是我自己起的叫法”，
# 把真名也收回了。现在只说“对方前面没提过、是你先说起的”，并写明真有的事就照实解释
ECHO_HINT = ("【刚看到】对方在问你上一句里说到的「{w}」。对方前面没提过它，是你先说起的。"
             "对方可能是没听懂，也可能是觉得你扯远了：如果是你想到别处去了，就认（可以嘴硬，不用道歉）；"
             "如果是你真去过的地方、真遇到过的事、真想说的话，就照实解释，别收回。"
             "不管哪种，都别说成是对方提的，也别装作没说过。")

_ASK_RE = re.compile(r"[？?]|哪有|哪来|哪里来|什么|啥|怎么突然|为什么突然")
_HEAD_RE = re.compile(r"^【[^】]*】")           # 群聊的「【说话人 → 你】」前缀
_GAP_RE = re.compile(r"^（过了[^）]*）")
# 这些词两边都常说，不算“她先说的”
_STOP = set("""什么 怎么 这个 那个 这样 那样 我们 你们 他们 一下 没有 不是 就是 还是 可以 知道 刚才 刚刚 哪有 哪来
真的 为什么 突然 小姐 伊蕾娜 魔女 一个 这么 那么 怎样 是不 不会 好像 说的 你说 我说 哪里 那里 这里 时候 东西
然后 所以 但是 因为 如果 已经 还有 一样 其实 自己 意思""".split())
_CJK_RE = re.compile(r"[一-鿿]+")


def _asked(t: str, w: str) -> bool:
    """对方是在问这个词本身（而不是一句话里碰巧有这两个字）"""
    e = re.escape(w)
    return bool(re.search(rf"{e}[」”\"'’）)\s…。.]*[？?]"
                          rf"|(?:哪有|哪来的?|哪里来的?|哪里有|什么|啥|哪个|哪位|哪门子的?)[「“\"']?{e}"
                          rf"|{e}[」”\"']?(?:是什么|是啥|是谁|是哪|在哪|又是|是怎么)"
                          rf"|(?:说|提|扯到?|讲)[「“\"']?{e}", t))


def _body(s: str) -> str:
    return _GAP_RE.sub("", _HEAD_RE.sub("", (s or "").strip()))


def echoed_word(text: str, last_reply: str, earlier_user: list[str], max_len: int = 30) -> str:
    """对方这条在反问她上一句里、对方自己之前没说过的词；没有就返回空串"""
    t = _body(text)
    if not t or len(t) > max_len or not last_reply or not _ASK_RE.search(t):
        return ""
    before = "\n".join(_body(x) for x in earlier_user)
    found = []
    for run in _CJK_RE.findall(t):
        n = len(run)
        for i in range(n):
            for j in range(n, i + 1, -1):          # 从长到短，找到一个就不再往短里找
                w = run[i:j]
                if w in last_reply:
                    found.append(w)
                    break
    words = [w for w in found if len(w) >= 2 and w not in _STOP and not any(w != o and w in o for o in found)]
    # 去掉词头词尾的“哪有”“什么”这类（“哪有蘑菇”→“蘑菇”）
    cleaned = []
    for w in words:
        for s in sorted(_STOP, key=len, reverse=True):
            if w.startswith(s) and len(w) - len(s) >= 2:
                w = w[len(s):]
            if w.endswith(s) and len(w) - len(s) >= 2:
                w = w[:-len(s)]
        if w not in _STOP and w not in before and w not in cleaned and _asked(t, w):
            cleaned.append(w)
    return max(cleaned, key=len) if cleaned else ""


def echo_hint(text: str, last_reply: str, earlier_user: list[str]) -> str:
    w = echoed_word(text, last_reply, earlier_user)
    return ECHO_HINT.format(w=w) if w else ""
