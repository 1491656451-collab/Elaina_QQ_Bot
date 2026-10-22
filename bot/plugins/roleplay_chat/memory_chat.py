"""长期记忆的指令和定时补做（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
/记忆、/忘记、/改记忆，攒够一批却没整理的定时补做、睡前整理。整理本身在 memory.py"""
import asyncio
import json
import re
import time
from datetime import datetime

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import peak, sleep
from .core import BOT_DIR, cfg, ltm, self_mem
from .sleep import night_key


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


MEMORY_USAGE = "用法：/记忆 @某人（或 QQ号、我、本群）；/忘记 @某人（或 QQ号、我、本群）；/忘记 @某人 第3条（只删 /记忆 里的第 3 条，“她说过”接着往下编号，也能这样删）；/改记忆 @某人 第3条 新内容（开头可以先写类型，如“第3条 经历 兑现过……”）"


mem_show = on_command("记忆", aliases={"查看记忆"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@mem_show.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    target = _parse_target(event, arg)
    if not target:
        await mem_show.finish(MEMORY_USAGE)
    kind, tid = target
    await mem_show.finish(ltm.describe_group(tid) if kind == "group" else ltm.describe_user(tid))



mem_forget = on_command("忘记", aliases={"删除记忆"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@mem_forget.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    nth = re.search(r"第\s*(\d{1,3})\s*条\s*$", arg.extract_plain_text())
    if nth:                                   # 只删一条：/忘记 @某人 第3条
        rest = Message([seg for seg in arg if seg.type != "text"])
        head = arg.extract_plain_text()[:nth.start()].strip()
        if head:
            rest += MessageSegment.text(head)
        target = _parse_target(event, rest)
        if not target or target[0] != "user":
            await mem_forget.finish(MEMORY_USAGE)
        gone = ltm.forget_fact(target[1], int(nth.group(1)))
        await mem_forget.finish(f"（已删掉第 {nth.group(1)} 条：{gone}）" if gone is not None else f"（没有第 {nth.group(1)} 条，先用 /记忆 看看）")
    target = _parse_target(event, arg)
    if not target:
        await mem_forget.finish(MEMORY_USAGE)
    kind, tid = target
    if kind == "group":
        ok = ltm.forget_group(tid)
        await mem_forget.finish("（本群的往事已清空）" if ok else "（本群本来就没有往事记录）")
    ok = ltm.forget_user(tid)
    await mem_forget.finish(f"（已删除关于 {tid} 的长期记忆）" if ok else f"（关于 {tid} 本来就没有长期记忆）")



mem_edit = on_command("改记忆", aliases={"修改记忆"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


@mem_edit.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    """/改记忆 @某人 第3条 新内容：改 /记忆 里的第 3 条（“她说过”接着往下编号）"""
    plain = arg.extract_plain_text()
    m = re.search(r"(?:第\s*(\d{1,3})\s*条|印象)\s*[:：]?\s*(.+)$", plain, re.S)
    if not m or not m.group(2).strip():
        await mem_edit.finish("用法：/改记忆 @某人 第3条 新内容（序号看 /记忆；要换类型就在内容前写类型加空格，如“经历 兑现过……”）；/改记忆 @某人 印象 新内容（写“无”就清掉）")
    rest = Message([seg for seg in arg if seg.type != "text"])
    head = plain[:m.start()].strip()
    if head:
        rest += MessageSegment.text(head)
    target = _parse_target(event, rest)
    if not target or target[0] != "user":
        await mem_edit.finish("用法：/改记忆 @某人 第3条 新内容（序号看 /记忆；要换类型就在内容前写类型加空格，如“经历 兑现过……”）；/改记忆 @某人 印象 新内容（写“无”就清掉）")
    if m.group(1) is None:                    # /改记忆 @某人 印象 新内容
        done = ltm.edit_impression(target[1], m.group(2).strip())
        await mem_edit.finish(f"（印象：{done[0]} → {done[1]}）" if done else "（新印象是空的，没改）")
    done = ltm.edit_fact(target[1], int(m.group(1)), m.group(2).strip())
    if done is None:
        await mem_edit.finish(f"（没有第 {m.group(1)} 条，先用 /记忆 看看）")
    await mem_edit.finish(f"（第 {m.group(1)} 条：{done[0]} → {done[1]}）")



self_show = on_command("近况", rule=to_me(), permission=SUPERUSER, priority=5, block=True)
SELF_USAGE = "用法：/近况（看她说过自己在哪、记了哪些近况）；/近况 删 3；/近况 改 3 新内容（序号看 /近况）；/近况 在 某地（她说的地方记错了时改）"


@self_show.handle()
async def _(arg: Message = CommandArg()):
    """她自己的近况（10/23）：/近况、/近况 删 N、/近况 改 N 新内容"""
    t = arg.extract_plain_text().strip()
    if not t:
        await self_show.finish(self_mem.describe())
    m = re.match(r"(删|删除)\s*第?\s*(\d{1,3})\s*条?$", t)
    if m:
        gone = self_mem.delete(int(m.group(2)))
        await self_show.finish(f"（已删掉第 {m.group(2)} 条：{gone}）" if gone is not None else f"（没有第 {m.group(2)} 条，先用 /近况 看看）")
    m = re.match(r"(改|修改)\s*第?\s*(\d{1,3})\s*条?\s*[:：]?\s*(.+)$", t, re.S)
    if m:
        done = self_mem.edit(int(m.group(2)), m.group(3).strip())
        await self_show.finish(f"（第 {m.group(2)} 条：{done[0]} → {done[1]}）" if done else f"（没有第 {m.group(2)} 条，先用 /近况 看看）")
    m = re.match(r"在\s*[:：]?\s*(.+)$", t)
    if m:
        p = self_mem.set_place_by_admin(m.group(1).strip())
        await self_show.finish(f"（现在记成她在：{p.get('name')}）")
    await self_show.finish(SELF_USAGE)



_NIGHTLY_FILE = BOT_DIR / cfg.memory_dir / "nightly.json"     # 哪天晚上已经做过睡前整理（重启也不重做）


def _nightly_due(now: datetime | None = None) -> str | None:
    """该做睡前整理了：她睡着了（没开休息就是凌晨 3～6 点），而且今晚还没做。返回今晚的日期"""
    if not (cfg.memory_enabled and cfg.memory_nightly):
        return None
    now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
    sleeping = sleep.asleep(now) if cfg.sleep_enabled else 3 <= now.hour < 6
    night = night_key(now)
    if not sleeping:
        return None
    try:
        done = json.loads(_NIGHTLY_FILE.read_text(encoding="utf-8")).get("night")
    except (OSError, ValueError, AttributeError):
        done = None
    return None if done == night else night


async def _run_nightly(night: str) -> None:
    try:
        _NIGHTLY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _NIGHTLY_FILE.write_text(json.dumps({"night": night}), encoding="utf-8")   # 先记下：出错也不在同一晚反复重来
        await ltm.nightly()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"睡前整理出错：{e}")


async def _memory_retry_loop() -> None:
    """每 2 分钟看一次：聊完了的会话整理掉、该做睡前整理了就做；每 MEMORY_RETRY_MINUTES 补做攒够一批却没整理的"""
    await asyncio.sleep(60)                        # 开机先缓一分钟，再查一次
    last_retry = 0.0
    while True:
        try:
            if cfg.memory_retry_minutes and time.time() - last_retry >= cfg.memory_retry_minutes * 60:
                last_retry = time.time()
                n = ltm.retry_pending()
                if n:
                    logger.info(f"长期记忆：补做 {n} 个会话的整理")
            ltm.idle_sweep()
            self_mem.retry()                       # 近况：主动说的话上次没整理成的，补做（10/23）
            night = _nightly_due()
            if night:
                asyncio.create_task(_run_nightly(night))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检查待整理的长期记忆出错：{e}")
        await asyncio.sleep(120)
