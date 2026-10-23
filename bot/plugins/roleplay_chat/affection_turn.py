"""每轮聊天里属于好感的判断（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
对方说自己的性别（识别、质疑、确认）、送面包和给钱的识别、/好感、/性别。
分数怎么存、怎么算在 affection.py；送礼时给她的提示文字（gift_hint）归聊天，在 turn_hints.py
10/23：对方是不是在说自己的性别，改由模型判断（judge_gender），不再靠 gender_claim 的关键词（问句、“没说过”认错过，见《问题排查记录-性别误判-1023.md》）"""
import re
import time

from . import budget

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import core
from .core import cfg, ltm


_F_WORDS = "女生|女孩子|女孩纸|女孩|女的|妹子|妹纸|女人|姑娘|小姐姐|女性|少女"


_M_WORDS = "男生|男孩子|男孩|男的|汉子|男人|大老爷们|老爷们|爷们|男性|小哥哥|少年"


_CLAIM_RE = re.compile(
    rf"(?:我|本人|人家|俺)(?:其实|本来|可|也|就|确实|真的)?(?:就)?是(?:个|一个|一名|一位)?\s*(?P<w>{_F_WORDS}|{_M_WORDS})(?![的们朋])"
    rf"|(?:本人|性别)[:：]?\s*(?P<c>[男女])(?![朋友孩人])"
)


_YES_RE = re.compile(r"^(?:真的|真滴|是的|是啊|是呀|是滴|对|对啊|对呀|对的|嗯|嗯嗯|当然|确定|千真万确|骗你干嘛|骗你干什么|没骗你|不骗你|如假包换)|真的是|没骗|不信拉倒")


_NO_RE = re.compile(r"骗你的|开玩笑|开个玩笑|假的|逗你的|逗你玩|才不是|不是啦")


def gender_claim(text: str) -> str | None:
    """对方说“我是女生 / 本人男”之类的，返回 female / male；没说返回 None。
    10/23 起线上不再用它判断（改成 judge_gender 问模型），留着只为兼容旧的导入"""
    for m in _CLAIM_RE.finditer(text):
        w = m.group("w") or m.group("c")
        start = m.start()
        if "不" in text[max(0, start - 1): m.end() - len(w)]:     # “我不是女生”
            continue
        if w.startswith("女") or w in _F_WORDS.split("|"):
            return "female"
        return "male"
    return None


# 10/23：先粗筛（这句话里有没有和性别沾边的字），沾边了才问模型；正在等对方确认时每句都问（“是的”“真的啦”不带性别字）
_GENDER_HINT_RE = re.compile(rf"男|女|{_F_WORDS}|{_M_WORDS}|性别|汉子|爷们|妹子|妹纸|姑娘|少年|小哥哥|小姐姐")

GENDER_JUDGE_PROMPT = """下面是 QQ 聊天里的两句话。判断「对方」最后这一句，是不是在说**自己**的性别。

伊蕾娜刚才说：{prev}
对方说：{text}

只回一个词：
女 —— 对方在说自己是女生。包括被问“真的假的”以后确认（“是的”“真的啦”“骗你干嘛”），也包括没带主语的（“人家可是女孩子”“就是女孩子啦”）
男 —— 对方在说自己是男生（同上）
否认 —— 对方在否认：说自己不是某个性别，说自己没说过是男生 / 女生，或者说刚才那句是开玩笑的
无 —— 都不是：在提问、让她猜（“你觉得我是男是女”“我是男生吗”），在说别人，假设（“如果我是女生”），反问，或者和性别无关

只认对方**直接说出来**的自己的性别，不要从别的话往回推：
- “我不是女生”“我又不是男生”只说了不是什么，答“否认”，不要推成另一个性别（除非同一句里又直接说了“我是男生 / 女生”）；
- “我要是男生就好了”“下辈子想当女孩子”都推不出对方是什么性别，答“无”；
- 提到自己的男朋友、女朋友、老公、老婆、对象（“我男朋友今天来接我”“我和我女朋友去看电影了”），也答“无”：对方没说自己是男是女，不要按“有女朋友就是男生”去猜。

拿不准就回“无”。只回这一个词。"""


def parse_gender_judge(reply: str) -> str | None:
    """模型的回答 → female / male / deny / None（无、看不懂）"""
    r = (reply or "").strip().strip("。.！!「」“”\"'*` ")
    if r.startswith("否认"):
        return "deny"
    if r.startswith("女"):
        return "female"
    if r.startswith("男"):
        return "male"
    return None


async def judge_gender(qq: int, text: str, prev: str = "") -> str | None:
    """问模型：对方这句话是不是在说自己的性别。返回 female / male / deny / None；出错按 None（这轮不管性别）"""
    try:
        r = await core.client.chat.completions.create(
            model=cfg.deepseek_model,
            messages=[{"role": "user", "content": GENDER_JUDGE_PROMPT.format(prev=(prev or "（没说话）")[-120:], text=text[:200])}],
            temperature=0,
            max_tokens=4,
            extra_body={"thinking": {"type": "disabled"}},
            timeout=15,                   # 慢了就这轮不管性别，别拖住回复
        )
        budget.track(r, "gender", user=qq)
        verdict = parse_gender_judge(r.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001   判断出错不影响回复
        logger.warning(f"性别：判断出错，这轮不管：{e}")
        return None
    if verdict:
        logger.info(f"性别：模型判断 {qq} 这句是“{verdict}”：{text[:30]}")
    return verdict


async def gender_step(qq: int, text: str, prev: str = "") -> str:
    """处理对方关于自己性别的说法，返回给模型的提示（没有就返回空）。prev：她上一句说的话（判断“是的”“真的啦”要用）"""
    prof = ltm.get_user(qq)
    pending = prof.get("gender_pending")
    if pending and time.time() - pending.get("ts", 0) > cfg.gender_confirm_window:
        prof.pop("gender_pending", None)
        pending = None
    if not pending and not _GENDER_HINT_RE.search(text):
        return ""                         # 和性别不沾边，也没在等确认：不问模型
    claim = await judge_gender(qq, text, prev)
    word = {"female": "女生", "male": "男生"}
    if pending:
        value = pending["value"]
        if claim == "deny" or (claim in word and claim != value):
            prof.pop("gender_pending", None)
            ltm.save_user(prof)
            if claim in word:             # 改口了：按新的说法重新质疑
                return _new_claim(qq, ltm.get_user(qq), claim)
            return "【性别】对方说刚才那句不是在说自己是" + word[value] + "（或者是开玩笑的）。你就当没听过，别再提。"
        if claim == value:
            ltm.claim_gender(qq, value)             # 9/30 起只算一次“自称”，要有别的证据印证才算数（对方可能在骗她）
            logger.info(f"性别：{qq} 自称{word[value]}（质疑后又确认）")
            return (f"【性别】对方又确认了一次，说自己确实是{word[value]}。你姑且信了（可以嘴上说一句“好吧，姑且信你”之类），"
                    "这一轮别再追问；心里留个问号就行。")
        return ""                         # 聊别的去了：先不管，等窗口过期
    if claim not in word or claim == prof.get("gender") or claim == ltm.last_claim(prof):   # 已经说过、质疑过的，不再每次都质疑
        return ""
    return _new_claim(qq, prof, claim)


def _new_claim(qq: int, prof: dict, claim: str) -> str:
    """对方说了自己的性别（第一次说，或者改口）：记下“待确认”，让她质疑一句"""
    word = {"female": "女生", "male": "男生"}
    prof["gender_pending"] = {"value": claim, "ts": time.time()}
    ltm.save_user(prof)
    extra = ""
    if prof.get("gender") and prof["gender"] != claim:
        extra = f"而且对方以前明明说自己是{word[prof['gender']]}。"
    elif ltm.last_claim(prof) and ltm.last_claim(prof) != claim:
        extra = f"而且对方以前明明说自己是{word[ltm.last_claim(prof)]}。"
    elif prof.get("gender_guess") and prof["gender_guess"] != claim:
        extra = f"而且你一直觉得对方更像{word[prof['gender_guess']]}。"
    return (f"【性别】对方说自己是{word[claim]}。{extra}你不会马上相信，用你的方式质疑一句，让对方再确认一次"
            "（比如“真的假的？”“嗯……我怎么有点不信呢”）。别追问细节，也别说要验证。")


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
    await gender_cmd.finish(f"（{prof.get('name') or qq}｜性别：{ltm.gender_text(prof)}｜好感 {ltm.effective_score(prof):g}｜{ltm.TIER_NAMES[core.familiarity_of(qq)]}）")
