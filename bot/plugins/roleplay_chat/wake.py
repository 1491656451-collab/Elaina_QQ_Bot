"""睡醒以后看到睡着时收到的消息（10/24 新加，聊天 agent；《问题排查记录-睡觉时收到的消息-1024》）。

以前休息时间里来找她的话直接扔掉：不回、不记，醒了也不知道。现在：
- 休息时间里来找她的话先记下来（存盘，夜里重启也在），不回；
- 她起床后过一会儿（像早上起来看手机），一个会话一个会话地交给回消息主流程，**照正常聊天的规则走**：
  额度、水话可能不接、她觉得没必要接话可以不回……只是她知道这些是睡着时收到的、现在才看到；
- 醒来以后对方先来说话的，睡着时那几条并进这一轮一起看，不再单独补。
"""
from __future__ import annotations

import asyncio
import json
import random
import time

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, PrivateMessageEvent

from . import sleep
from .core import BOT_DIR, cfg

BOX_FILE = BOT_DIR / "data" / "sleep_inbox.json"
MAX_PER_CHAT = 8                 # 每个会话最多记几段
_box: dict[str, dict] | None = None   # "会话|QQ" -> {"first": 第一段的时间, "items": [{"text", "kind", "event"}]}
_task: "asyncio.Task | None" = None


def _k(ik: tuple[str, int]) -> str:
    return f"{ik[0]}|{ik[1]}"


def _ik(k: str) -> tuple[str, int]:
    s, u = k.rsplit("|", 1)
    return s, int(u)


def _load() -> dict[str, dict]:
    global _box
    if _box is None:
        try:
            _box = json.loads(BOX_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _box = {}
        except (OSError, ValueError) as e:
            logger.warning(f"睡着时收到的消息：{BOX_FILE} 读不了，当成空的：{e}")
            _box = {}
    return _box


def _save() -> None:
    try:
        BOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BOX_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(BOX_FILE)
    except OSError as e:
        logger.warning(f"睡着时收到的消息：存不下来：{e}")


def note(ik: tuple[str, int], event: MessageEvent, text: str) -> None:
    """休息时间里来找她的话：记下来，醒了再看"""
    if not cfg.wake_catchup or not text:
        return
    e = _load().setdefault(_k(ik), {"first": time.time(), "items": []})
    e["items"].append({"text": text, "kind": "group" if isinstance(event, GroupMessageEvent) else "private",
                       "event": event.model_dump(mode="json")})
    e["items"] = e["items"][-MAX_PER_CHAT:]
    _save()
    logger.info(f"睡着时收到：{ik[0]} user={ik[1]}，醒了再看（这个会话记了 {len(e['items'])} 段）")


def take(ik: tuple[str, int]) -> tuple[list[str], float] | None:
    """拿走这个会话睡着时收到的话：(几段原话, 第一段的时间)；没有、或者太久以前的返回 None"""
    e = _load().pop(_k(ik), None)
    if not e:
        return None
    _save()
    if time.time() - float(e.get("first") or 0) > cfg.wake_catchup_max_hours * 3600:
        logger.info(f"睡着时收到的话太久了，不看了：{ik[0]} user={ik[1]}")
        return None
    return [i["text"] for i in e["items"]], float(e["first"])


def pending() -> list[tuple[str, int]]:
    return [_ik(k) for k in sorted(_load(), key=lambda k: _load()[k].get("first", 0))]


def _event_of(item: dict) -> MessageEvent:
    cls = GroupMessageEvent if item.get("kind") == "group" else PrivateMessageEvent
    return cls.model_validate(item["event"])


async def wake_up(bot: Bot) -> int:
    """起床后过一会儿，把睡着时收到的一个会话一个会话交给回消息主流程。返回看了几个会话"""
    from .reply import _inbox, _woke, converse      # 回消息主流程在 reply.py（它也用到这个文件，所以在这里才导入）
    lo, hi = sorted(cfg.wake_catchup_delay_minutes)
    delay = random.uniform(lo, hi) * 60
    logger.info(f"睡醒了：{len(pending())} 个会话有睡着时收到的话，{delay / 60:.0f} 分钟后再看")
    await asyncio.sleep(delay)
    done = 0
    for ik in pending():
        if sleep.asleep():
            break
        e = _load().get(_k(ik))
        if not e or not e.get("items"):
            continue                              # 醒来以后对方先来说话了，已经并进那一轮
        try:
            ev = _event_of(e["items"][-1])
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"睡着时收到的话：{ik[0]} 读不回来，不看了：{ex}")
            take(ik)
            continue
        got = take(ik)
        if not got:
            continue
        texts, first = got
        if done:
            await asyncio.sleep(random.uniform(cfg.catchup_gap_min, cfg.catchup_gap_max))
        _inbox[ik].extend(texts[:-1])               # 最后一段由回消息主流程自己放进去
        _woke[ik] = first
        logger.info(f"睡醒了看到 {ik[0]} user={ik[1]} 睡着时说的 {len(texts)} 段，照平常的规则看要不要回")
        try:
            await converse(bot, ev, catchup_age=time.time() - first)
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"睡醒了看 {ik[0]} 的消息出错：{ex}")
        done += 1
    return done


def maybe_start(bot: Bot) -> None:
    """醒着、又有睡着时收到的话、还没在看：开始（每 30 秒在道晚安的循环里看一次；夜里重启过也接得上）"""
    global _task
    if not cfg.wake_catchup or sleep.asleep() or not pending() or (_task is not None and not _task.done()):
        return
    _task = asyncio.create_task(wake_up(bot))
