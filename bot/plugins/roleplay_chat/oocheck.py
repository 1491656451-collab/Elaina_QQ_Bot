"""出戏检查（9/29 起线上和回归测试共用这一份，改标准只改这里）。

哪些算出戏：
1. 这些词她自己冒出来的（对方没说过）——她不该知道这些东西。
2. 对方说过这个词，她拿来用，而且是当成懂的概念来用：“我是 AI”“我才不是机器人”“你要是找 AI 的话”。
不算出戏：对方说过的词，她拿来反问、表示不懂——“AI？那是什么”“「bug」是什么，外国话吗”“动漫是哪国的词”，
加不加引号都一样。只重复一遍、看不出懂不懂的（“别光喊 bug”），按懂的算，照样算出戏。
问号要紧跟在这个词后面才算反问（“AI？”）；“AI很厉害吧？”这种句尾的问号是当成懂的在问，算出戏（9/29 20:20）。

这个文件只用标准库，回归测试（tools\\regression.py）直接读它，不用启动机器人。
"""
from __future__ import annotations

import re

OOC_RE = re.compile(
    r"服务器|人工智能|(?<![A-Za-z])AI(?![A-Za-z])|机器人|计算机|电脑程序|程序员|代码|大模型|语言模型|数据库|"
    r"系统提示|提示词|人设|DeepSeek|ChatGPT|GPT|OpenAI|动漫|动画|番剧|轻小说|原作|声优|二次元|虚拟角色|角色扮演|OOC|"
    r"死机|宕机|掉线|重启|(?<![A-Za-z])bug(?![A-Za-z])|开发者|人工智障|(?<![A-Za-z])prompt(?![A-Za-z])|"
    r"第[0-9一二三四五六七八九十]+[卷集]",
    re.I,
)

# 这一句里她表现得不懂这个词：问是什么、说没听过……（光有问号不算，见 _echo_question）
_UNKNOWN_RE = re.compile(r"是什么|是啥|什么东西|什么意思|啥意思|没听说|没听过|听不懂|不懂|不知道|不认识|"
                         r"外国话|哪国|哪里的话|某种|能吃吗")


def _echo_question(sent: str, w: str) -> bool:
    """把这个词原样反问回去：“AI？”“「bug」？”“AI……？”。问号要紧跟在词后面；
    “AI很厉害吧？”这种句尾的问号不算（那是当成懂的在问）"""
    return bool(re.search(rf"{re.escape(w)}[」”\"』’')）\s…。.]*[？?]", sent, re.I))
_SENT_RE = re.compile(r"[^。！!\n]+[。！!]*")


def _as_known(sent: str, w: str) -> bool:
    """把这个词当成懂的东西在用：我是 / 我不是 / 找 / 当成 / 像……，或者“AI 的话”“AI 那种”"""
    e = re.escape(w)
    return bool(re.search(rf"我(?:就|也|才|又|可)?(?:是|不是|算|当|像)[^，。！？!?]{{0,4}}{e}"
                          rf"|(?:找|当成|变成|做成|像|比|叫)[^，。！？!?]{{0,3}}{e}"
                          rf"|{e}(?:的话|那种|这种|一样|之类的?东西)", sent, re.I))


def ooc_words(reply: str, user_text: str = "") -> list[str]:
    """回复里出戏的词（去重、排序）"""
    said = user_text.lower()
    bad = set()
    for w in {m.group(0) for m in OOC_RE.finditer(reply)}:
        if w.lower() not in said:
            bad.add(w)                     # 对方没说过，她自己冒出来的
            continue
        for sent in _SENT_RE.findall(reply):
            if w.lower() not in sent.lower():
                continue
            if _as_known(sent, w) or not (_UNKNOWN_RE.search(sent) or _echo_question(sent, w)):
                bad.add(w)                 # 当成懂的概念来用，或者只是顺着用
                break
    return sorted(bad)
