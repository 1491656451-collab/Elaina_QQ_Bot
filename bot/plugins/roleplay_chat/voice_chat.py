"""聊天时发语音（10/22 从 __init__.py 拆出来，代码原样搬过来，没改；见 docs 里的《语音发送规则方案》）：
这轮给不给【传声】、抽签、认她写的传声标记、要念的文字、译成日语、/语音。和语音服务通信的部分还在 voice.py"""
import asyncio
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import budget, voice
from .core import BOT_DIR, cfg, client, tier_value
from .prompts import VOICE_OFFER_HINT, VOICE_REQUEST_HINT, VOICE_REQUEST_TIER_HINT
from .textout import _VOICE_MARK_RE


# 声音在你电脑上合成（voice_service），连不上就当没有这回事。三种情况会发：
#   1. 抽签：每次回复按 VOICE_PROB_PRIVATE / VOICE_PROB_GROUP × 关系倍数抽，抽中就念
#   2. 对方要她发语音：她愿意才在开头写「[传声]」（不限次数，看她自己愿不愿意）。
#      怎么知道对方在要：VOICE_REQUEST_BY_MODEL 开着（10/20 起默认）时不认关键词，每轮都告诉她“对方想听你的声音时可以用”，
#      由她自己判断（“再来一条”“想听听你的嗓音”也认得出）；关掉就回到认关键词（VOICE_REQUEST_RE），认到了才给提示
#   3. 她只回一个语气词（哈？嗯。）：按 VOICE_INTERJECTION_PROB 用原声
# 唯一的上限：所有人加起来每天 VOICE_DAILY_MAX 条（10/20 定，见 docs\语音发送规则方案.md）
VOICE_REQUEST_RE = re.compile(
    r"(发|来|整|说|录)(个|段|条|句|一段|一条|一句)?语音|语音(条|发|说|来|回)|用语音|听听(你的)?声音|听一?下(你的)?声音|"
    r"(你的|你)声音|说句话(给我)?听|开口说(句)?话|念(给我|出来)听|让我听听")


_voice_count: dict[str, int] = defaultdict(int)       # 日期 -> 今天发了几条语音（重启清零）


_tts_refresh: "asyncio.Task | None" = None


def voice_day() -> str:
    """一天从凌晨 BUDGET_RESET_HOUR 点算起，和花钱的账一致"""
    return (datetime.now() - timedelta(hours=cfg.budget_reset_hour)).strftime("%Y-%m-%d")


def tts_ready() -> bool:
    """电脑上的语音服务在不在：只看缓存的结果，过期了在后台再问一次，不让这条消息等"""
    global _tts_refresh
    if not (cfg.voice_enabled and tts.configured):
        return False
    if time.monotonic() - tts._checked >= tts.check_interval and (_tts_refresh is None or _tts_refresh.done()):
        _tts_refresh = asyncio.create_task(tts.available())
    return tts._ok


def voice_roll(fam: str, is_group: bool, text: str) -> tuple[bool, str]:
    """这一轮的语音打算。返回（抽中了随机语音, 给她的提示）"""
    if not tts_ready() or _voice_count[voice_day()] >= cfg.voice_daily_max:
        return False, ""
    p = (cfg.voice_prob_group if is_group else cfg.voice_prob_private) * tier_value(cfg.voice_tier_multiplier, fam, 1.0)
    if cfg.voice_request_by_model:      # 每轮都告诉她可以用，对方在不在要由她判断；抽签照旧
        tier = VOICE_REQUEST_TIER_HINT.get(fam, "")
        return random.random() < p, ("" if fam == "disliked" else VOICE_OFFER_HINT + (f"对方要的话：{tier}" if tier else ""))
    if VOICE_REQUEST_RE.search(text):
        return False, ("" if fam == "disliked" else VOICE_REQUEST_HINT + VOICE_REQUEST_TIER_HINT.get(fam, ""))
    return random.random() < p, ""


def split_voice_mark(reply: str) -> tuple[str, bool]:
    """截掉开头的「[传声]」：返回（文字, 她想用语音）"""
    m = _VOICE_MARK_RE.match(reply)
    return (reply[m.end():].strip(), True) if m else (reply, False)


_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]|[（(][^（）()]{0,8}[)）]|[~～]+|[＾^][_＿]?[＾^]|[TＴ][_＿][TＴ]|ww+$")


def voice_text(reply: str, limit: int) -> str | None:
    """要念的文字：去掉颜文字、括号、波浪号，换行改成逗号；有英文字母、太长、没字了就不念"""
    t = _EMOJI_RE.sub("", reply)
    t = re.sub(r"\s*\n+\s*", "，", t)
    t = re.sub(r"([，,。！？!?…])，+", r"\1", t).strip("，, ")
    if not t or len(t) > limit or re.search(r"[A-Za-z]", t) or not re.search(r"[\u4e00-\u9fff]", t):
        return None
    return t


# 10/21：她的声音只用日语素材训练过，念中文时卷舌音、鼻音念不好（见 voice_data\样音对比报告-第9轮.md），
# 所以 VOICE_LANG=ja 时：她要说的中文先译成日语，用日语念，语音后面再把中文作为文字发出去
VOICE_JA_PROMPT = """把下面这句中文译成日语口语。说这句话的是伊蕾娜（《魔女之旅》的主角，旅行中的魔女，“灰之魔女”），这是她用自己的声音说出来的话。
要求：
- 照她在日文原作里的说话方式：平时是带“です・ます”的礼貌说法，语气里带点自信、得意、毒舌；对很熟的人可以随意一点。自称“私”。
- 保留原句的语气（反问、吐槽、撒娇、不耐烦），意思别增别减，别加原句没有的话。
- 人名用原作写法：伊蕾娜→イレイナ、沙耶→サヤ、芙兰→フラン、希拉→シーラ、妮可→ニケ。别的中文名字、网名照写汉字或用片假名音译。
- 她的口头禅照原作原话，不要自己另译：没错，就是我→そう、私です；原来如此→なるほど；可喜可贺→めでたしめでたし；顺带一提→ちなみに。
- 只输出译好的那一句日语，不要引号、注释、罗马音、中文。

中文：{text}"""


async def translate_ja(text: str, user_id: int | None = None, group_id: int | None = None) -> str | None:
    """把要念的中文译成日语；失败、超时、译出来不像日语都返回 None（这条就只发文字）"""
    try:
        r = await asyncio.wait_for(client.chat.completions.create(
            model=cfg.deepseek_model,
            messages=[{"role": "user", "content": VOICE_JA_PROMPT.format(text=text)}],
            temperature=0.3,
            max_tokens=200,
            extra_body={"thinking": {"type": "disabled"}},
        ), timeout=cfg.voice_translate_timeout)
        budget.track(r, "voice_ja", user=user_id, group=group_id)
        ja = (r.choices[0].message.content or "").strip().strip("「」『』\"' ")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"语音：译成日语失败：{e!r}")
        return None
    ja = re.sub(r"\s*\n+\s*", "", ja)
    if not ja or not voice.has_kana(ja) or len(ja) > cfg.voice_text_max_chars or re.search(r"[A-Za-z]", ja):
        logger.warning(f"语音：译出来的不像能念的日语，不念：{ja[:60]!r}")
        return None
    return ja


def voice_wav_seconds(data: bytes) -> float:
    try:
        import io
        import wave
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return 2.0


voice_files = voice.VoiceFiles(BOT_DIR / "data" / "voice")


tts = voice.TTSClient(cfg.voice_tts_url, cfg.voice_tts_token, cfg.voice_tts_timeout)


interjections = voice.Interjections(BOT_DIR / "data" / "voice" / "interjections")


voice_cmd = on_command("语音", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


VOICE_USAGE = ("用法：/语音 说 文字 → 用她的声音念这句（单个语气词用原声；写日语就直接念日语，写中文时 VOICE_LANG=ja 会先译成日语再念）；"
               "/语音 说中文 文字 → 不翻译、直接念中文；/语音 状态 → 电脑上的语音服务连没连上；"
               "/语音 测试 → 发一段“叮咚”测试音；/语音 文件名 → 发 data/voice/test/ 里的音频；/语音 列表 → 看有哪些测试音频")


@voice_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    words = arg.extract_plain_text().split()
    if not words:
        await voice_cmd.finish(VOICE_USAGE)
    op = words[0]
    if op == "测试":
        await voice_cmd.finish(await _send_voice(bot, event, voice.test_tone(), "测试音"))
    if op in ("状态", "status"):
        if not tts.configured:
            await voice_cmd.finish("（没配置语音合成：服务器 .env 里要有 VOICE_TTS_TOKEN）")
        ok = await tts.available(force=True)
        n = interjections.load()
        if ok:
            i = tts.info
            msg = f"（语音服务在线：{i.get('gpt', '?')} + {i.get('sovits', '?')}，电脑开机以来念了 {i.get('count', 0)} 句"
        else:
            msg = f"（语音服务连不上：{tts.last_error or '未知原因'}。电脑上的 启动语音服务.bat 开着吗？"
        auto = ("开" if cfg.voice_enabled else "关") + ("，念日语、附中文" if cfg.voice_lang == "ja" else "，念中文")
        await voice_cmd.finish(msg + f"；原声语气词 {n} 种写法；聊天时自己发语音：{auto}，今天发了 {_voice_count[voice_day()]}/{cfg.voice_daily_max} 条）")
    if op in ("说", "念", "说中文", "念中文"):
        text = arg.extract_plain_text().strip()[len(op):].strip()
        if not text:
            await voice_cmd.finish("用法：/语音 说 你又在胡说什么呀，我可是很忙的。")
        clip = interjections.match(text)
        if clip:
            await voice_cmd.finish(await _send_voice(bot, event, clip.read_bytes(), f"原声语气词 {clip.name}"))
        lang, ja_note = "zh", ""
        if voice.has_kana(text):
            lang = "ja"
        elif cfg.voice_lang == "ja" and not op.endswith("中文"):
            ja = await translate_ja(text, event.user_id)
            if ja is None:
                await voice_cmd.finish("（没译成日语，看日志；想直接念中文用 /语音 说中文 文字）")
            text, lang, ja_note = ja, "ja", f"日语：{ja}；"
        data, note = await tts.speak(text, lang=lang)
        if data is None:
            await voice_cmd.finish(f"（没念成：{note}）")
        logger.info(f"语音：/语音 说「{text}」{note}")
        await voice_cmd.finish((await _send_voice(bot, event, data, "合成语音")) + f"（{ja_note}{note}）")
    if op in ("列表", "list"):
        names = voice_files.list_test()
        if not names:
            await voice_cmd.finish(f"（data/voice/test/ 里还没有测试音频。放 mp3、wav 进去再发 /语音 文件名）")
        await voice_cmd.finish("（测试音频：" + "、".join(names) + "）")
    path = voice_files.find_test(" ".join(words))
    if not path:
        await voice_cmd.finish(f"（data/voice/test/ 里没找到“{' '.join(words)}”，或者对上了好几个）\n" + VOICE_USAGE)
    await voice_cmd.finish(await _send_voice(bot, event, path.read_bytes(), path.name))



async def _send_voice(bot: Bot, event: MessageEvent, data: bytes, label: str) -> str:
    """发一条语音，返回给管理员看的结果说明"""
    if len(data) > voice.MAX_BYTES:
        return f"（{label} 太大了：{len(data) / 1024 / 1024:.1f} MB，没发）"
    info = voice.wav_info(data)
    t0 = time.monotonic()
    try:
        await bot.send(event, voice.segment(data))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"语音：发送失败（{label}）：{e!r}")
        return f"（{label} 发送失败：{e}）"
    cost = time.monotonic() - t0
    logger.info(f"语音：已发送（{label}，{len(data) // 1024} KB，{info or '非 wav'}，用时 {cost:.1f} 秒）")
    return f"（{label} 已交给 NapCat：{len(data) // 1024} KB{'，' + info if info else ''}，用时 {cost:.1f} 秒。看看上面是不是语音条、能不能点开听）"
