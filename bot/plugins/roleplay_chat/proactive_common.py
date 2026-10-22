"""主动说话的公用部分（10/23 从插话、冒泡、冷场搭话、写信里抽出来，只挪不改）：
调一次模型（记账、清理、出错时通知管理员）、看她是不是不想说、在群里分一两条发出去。
四个流程各自的前后（拼什么提示、怎么算次数、发之前还要看什么）还在 proactive_group.py、proactive_private.py"""
import asyncio
import re

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from . import budget, core
from .oocheck import ooc_words
from .core import _hands, alert_admins, cfg, classify_error, client, mark_sent, switch_pause
from .history import _unrecord
from .textout import CHAT_STOP, clean_reply, split_bubbles
from .quota import bubble_gap, typing_delay
from .turn_hints import life_hint_proactive


async def ask(bot: Bot, messages: list[dict], kind: str, fail_log: str, *, user: int | None = None, group: int | None = None,
              max_tokens: int | None = None, stop: bool = True, error_level: str = "warning") -> str | None:
    """调一次模型写这一句，返回清理过的文字；出错返回 None（余额不足、Key 失效私信管理员）。
    kind：记账的分类；fail_log：出错时日志的开头（“插话：调用失败”“写信失败”……）；stop=False：不用“另起一行的【就停”（写信）"""
    kw = dict(model=cfg.deepseek_model, messages=messages, temperature=cfg.llm_temperature,
              max_tokens=max_tokens or cfg.short_reply_max_tokens, extra_body={"thinking": {"type": "disabled"}})
    if stop:
        kw["stop"] = CHAT_STOP
    try:
        resp = await client.chat.completions.create(**kw)
        budget.track(resp, kind, user=user, group=group)
        choice = resp.choices[0]
        return clean_reply(choice.message.content or "", truncated=choice.finish_reason == "length")
    except Exception as e:  # noqa: BLE001
        kind_ = classify_error(e)
        getattr(logger, error_level)(f"{fail_log}（{kind_ or '其他错误'}）：{e}")
        if kind_:
            await alert_admins(bot, kind_)
        return None


def life_memo(key: str) -> str:
    """冒泡、写信、搭话的提示里带的【你的近况】（10/23）。近况出错不影响说话"""
    try:
        return life_hint_proactive(core.self_mem.life_for_proactive(key))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"近况：取用出错，这次不带：{e}")
        return ""


def note_said(text: str, source: str, key: str, qq: int | None = None) -> None:
    """冒泡、写信、冷场搭话真的发出去以后，把原话交给近况（10/23；插话不交）。
    记忆那边在后台整理：讲的是她自己的经历、说了自己在哪，才记下来"""
    try:
        core.self_mem.add_said(text, source=source, key=key, qq=qq)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"近况：交给近况出错：{e}")


def declined(reply: str, skip_re: "re.Pattern | None" = None) -> bool:
    """她不想说：空的、写了“[不插]”“[不说]”这类标记，或者出戏了（主动说话没有对方的话可对照，出戏就整条不要）"""
    return not reply or bool(skip_re and skip_re.search(reply)) or bool(ooc_words(reply, ""))


async def send_to_group(bot: Bot, gid: int, reply: str, label: str) -> bool:
    """插话、冒泡：分成最多两条发到群里，像在打字；第一条就没发出去时从聊天记录里拿掉。发出去了返回 True"""
    key = f"group_{gid}"
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
                logger.warning(f"{label}：群{gid} 发送失败：{e}")
                if not i:
                    _unrecord(key, reply)
                return False
        mark_sent(key)
    return True
