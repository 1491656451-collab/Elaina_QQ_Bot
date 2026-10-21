"""聊天时甩表情（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：
这轮能不能带表情、按关系能用哪些情绪、【表情】提示、/表情、定时拉收藏和后台打标签。表情库本身还在 stickers.py"""
import asyncio
import random
import time
from collections import defaultdict, deque

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from . import budget, core
from .core import cfg, stickers, tier_value
from .quota import quota_left
from .stickers import EMOTIONS


TIER_EMOTIONS = {
    "disliked": {"嫌弃", "无语", "敷衍"},
    "stranger": set(EMOTIONS) - {"害羞", "委屈"},
    "friend": set(EMOTIONS) - {"委屈"},
}


_last_sticker: dict[str, float] = {}                  # 会话 -> 上次发表情的时间


_recent_stickers: dict[str, deque] = defaultdict(lambda: deque(maxlen=max(cfg.sticker_recent_avoid, 1)))


def sticker_roll(target: str, fam: str, is_group: bool, group_id: int | None, self_image: bool) -> tuple[bool, set[str] | None, list[str]]:
    """这轮能不能带表情。返回（能不能, 这个人能用的情绪, 库里现在能用的情绪）"""
    if not cfg.sticker_enabled or (not is_group and not cfg.sticker_private):
        return False, None, []
    allowed = TIER_EMOTIONS.get(fam)
    emotions = stickers.emotions(allowed)
    if not emotions:
        return False, allowed, []
    if time.monotonic() - _last_sticker.get(target, -1e9) < cfg.sticker_min_interval:
        return False, allowed, emotions
    if quota_left(group_id if is_group else None, None if is_group else int(target.split("_")[1])) < 2:     # 限额只剩最后一条了：留给文字
        return False, allowed, emotions
    p = cfg.sticker_self_react_prob if self_image else cfg.sticker_prob * tier_value(cfg.sticker_tier_multiplier, fam, 1.0)
    return random.random() < p, allowed, emotions


def sticker_hint(emotions: list[str], only_ok: bool) -> str:
    h = ("【表情】这一轮你可以在回复最后加一个表情，也就是甩一张自己的小画像，格式是「[表情:情绪]」，"
         f"情绪只能从这些里选：{'、'.join(emotions)}。只在这句话确实带着这种情绪时才用，觉得不合适就不加。")
    if only_ok:
        h += "这轮也可以一个字都不说，整条回复只写一个「[表情:情绪]」，就当甩了张画像过去。"
    return h


sticker_cmd = on_command("表情", aliases={"表情包"}, rule=to_me(), permission=SUPERUSER, priority=5, block=True)


STICKER_USAGE = ("用法：/表情 → 概况；/表情 列表；/表情 3 → 发出 3 号看看；/表情 3 嫌弃 无语 → 改标签；"
                 "/表情 3 禁用 / 启用；/表情 刷新 → 重新拉收藏；/表情 测试 嫌弃 → 按情绪挑一张\n"
                 "情绪词表：" + "、".join(EMOTIONS))


@sticker_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    words = arg.extract_plain_text().split()
    if not words:
        head = f"（{stickers.summary()}｜发表情{'开' if cfg.sticker_enabled else '关'}，概率 {cfg.sticker_prob:g}）"
        await sticker_cmd.finish(head + "\n" + STICKER_USAGE)
    op = words[0]
    if op in ("刷新", "更新", "同步"):
        try:
            added, removed, _n = await stickers.refresh(bot, cfg.sticker_fetch_count)
        except Exception as e:  # noqa: BLE001
            await sticker_cmd.finish(f"（拉取收藏表情失败：{e}）")
        pending = len(stickers.unlabeled())
        if pending:
            _spawn_labeling()
        note = f"，{pending} 张正在后台打标签（{'高峰时段先不打，低峰再说' if core.in_peak() else '大约每张几秒'}），过一会儿再看 /表情 列表" if pending else ""
        await sticker_cmd.finish(f"（新增 {added} 张，取消收藏 {removed} 张{note}）\n（{stickers.summary()}）")
    if op in ("列表", "全部", "list"):
        lines = stickers.list_lines()
        if not lines:
            await sticker_cmd.finish("（表情库是空的：先在小号收藏里存表情，再发 /表情 刷新）")
        for i in range(0, len(lines), 15):               # 太长的消息分几条发
            await bot.send(event, "\n".join(lines[i:i + 15]))
            await asyncio.sleep(1.0)
        await sticker_cmd.finish()
    if op == "测试":
        if len(words) < 2:
            await sticker_cmd.finish("用法：/表情 测试 嫌弃")
        it = stickers.pick(words[1])
        if not it:
            await sticker_cmd.finish(f"（库里没有“{words[1]}”或相近情绪的表情）")
        await bot.send(event, f"（{it['no']} 号：{'、'.join(it['tags'])}｜{it.get('desc') or ''}）")
        await sticker_cmd.finish(stickers.segment(it, cfg.sticker_sub_type))
    if op.isdigit():
        no = int(op)
        it = stickers.by_no(no)
        if not it:
            await sticker_cmd.finish(f"（没有 {no} 号表情）")
        rest = words[1:]
        if not rest:
            await bot.send(event, f"（{no} 号：{'、'.join(it.get('tags') or []) or '无情绪'}｜{it.get('desc') or ''}）")
            await sticker_cmd.finish(stickers.segment(it, cfg.sticker_sub_type))
        if rest[0] in ("禁用", "停用", "不用"):
            stickers.set_disabled(no, True)
            await sticker_cmd.finish(f"（{no} 号已禁用）")
        if rest[0] in ("启用", "恢复"):
            stickers.set_disabled(no, False)
            await sticker_cmd.finish(f"（{no} 号已启用）")
        tags = stickers.set_tags(no, rest)
        if not tags:
            await sticker_cmd.finish("（这些词对不上情绪词表：" + "、".join(EMOTIONS) + "）")
        await sticker_cmd.finish(f"（{no} 号的标签改成了：{'、'.join(tags)}；以后刷新也不会覆盖）")
    await sticker_cmd.finish(STICKER_USAGE)



_label_task: "asyncio.Task | None" = None


def _spawn_labeling() -> None:
    """后台给新表情打标签（高峰时段先停，等 _sticker_loop 低峰时再叫起来）"""
    global _label_task
    if not cfg.deepseek_api_key or (_label_task and not _label_task.done()):
        return

    async def run():
        n = await stickers.label_pending(can_run=lambda: not core.in_peak() and budget.can_background())
        if n:
            logger.info(f"表情：打好了 {n} 张的标签（{stickers.summary()}）")

    _label_task = asyncio.create_task(run())


async def _sticker_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    if stickers.unlabeled() and not core.in_peak():       # 上次没打完的标签：先接着打（不调 QQ 接口）
        _spawn_labeling()
    since = time.time() - stickers.last_refresh
    if since < cfg.sticker_refresh_hours * 3600:
        logger.info(f"表情：距上次拉取收藏 {since / 3600:.1f} 小时，不到 {cfg.sticker_refresh_hours:g} 小时，这次上线不拉")
    else:
        # 刚上线别马上调一串接口：随机等几分钟再拉
        await asyncio.sleep(random.uniform(120, 420))
    while True:
        try:
            if time.time() - stickers.last_refresh >= cfg.sticker_refresh_hours * 3600:
                await stickers.refresh(bot, cfg.sticker_fetch_count)
            if stickers.unlabeled() and not core.in_peak():
                _spawn_labeling()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"表情：拉取收藏表情失败（{e}），稍后再试")
            stickers.last_refresh = time.time() - cfg.sticker_refresh_hours * 3600 + 600   # 10 分钟后重试
        await asyncio.sleep(600)
