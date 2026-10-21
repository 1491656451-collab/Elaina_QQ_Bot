"""大家共用的对象（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：配置、目录、模型客户端、记账、识图、表情库、长期记忆；
查档位（familiarity_of、tier_value）；她只有一双手（同一时间只给一个人打字）和会话里的消息计数；
出错通知（余额不足、Key 失效时私信管理员）、几个按时间段和次数上限判断的小工具（10/22 第 7 步搬来）"""
from __future__ import annotations

import asyncio
from datetime import datetime
import random
import time
from collections import defaultdict
from pathlib import Path

from nonebot import get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from openai import AsyncOpenAI

from . import budget, peak
from .config import Config
from .memory import LongTermMemory
from .stickers import StickerStore
from .tagger import Tagger
from .vision import Vision

cfg = get_plugin_config(Config)
# bot 目录（这个文件在 bot/plugins/roleplay_chat/ 下）：不依赖“从哪个目录启动”，服务器上用 systemd 常驻也找得到数据
BOT_DIR = Path(__file__).resolve().parents[2]
if cfg.log_file:
    try:   # 控制台的日志也写进文件（按天分），事后能查“为什么没回”
        logger.add(str(BOT_DIR / cfg.log_file), level="INFO", encoding="utf-8", rotation="00:00",
                   retention=f"{cfg.log_retention_days} days", enqueue=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"日志文件打不开，只在控制台显示：{e}")
HISTORY_DIR = BOT_DIR / cfg.history_dir
_SAME_NAMES_FILE = BOT_DIR / "data" / "same_names.json"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

client = AsyncOpenAI(
    api_key=cfg.deepseek_api_key or "missing",
    base_url=cfg.deepseek_base_url,
    timeout=cfg.llm_timeout,
)

spend = budget.setup(budget.Budget(
    BOT_DIR / "data" / "usage", enabled=cfg.budget_enabled, total=cfg.budget_daily_total, reserve=cfg.budget_reserve,
    user_share=cfg.budget_user_share, group_share=cfg.budget_group_share, reset_hour=cfg.budget_reset_hour,
    price=dict(cfg.budget_price), peak_multiplier=cfg.budget_peak_multiplier, peak_ranges=cfg.budget_peak_ranges,
    holidays=peak.HOLIDAYS_2026 + list(cfg.peak_holidays),
))

tagger = Tagger(
    BOT_DIR / cfg.vision_tagger_dir, cfg.vision_tagger_repo, list(cfg.vision_tagger_mirrors),
    threshold=cfg.vision_tagger_threshold, threads=cfg.vision_tagger_threads,
) if (cfg.vision_enabled and cfg.vision_tagger) else None
vision = Vision(client, cfg.vision_model, BOT_DIR / cfg.vision_cache, tagger=tagger,
                maybe=cfg.vision_tagger_maybe)
from .vision import sanitize as _vision_sanitize  # noqa: E402
stickers = StickerStore(BOT_DIR / cfg.sticker_dir, client, cfg.vision_model, sanitize=_vision_sanitize)


def in_peak() -> bool:
    return cfg.peak_enabled and peak.is_peak(cfg.peak_ranges, peak.HOLIDAYS_2026 + list(cfg.peak_holidays))


if (cfg.affection_dislike, cfg.affection_acquaintance, cfg.affection_close) == (-20, 30, 70):
    # 9/29 分数范围从 -100～100 换成 -50～150；.env 里还是照旧版 .env.example 抄的档位，按新档位来，不然换算后的分数全对不上
    logger.warning("好感：.env 里的 AFFECTION_DISLIKE/ACQUAINTANCE/CLOSE 还是旧的 -20/30/70，已按新档位 0/90/130 处理，"
                   "请把这三行删掉或改成新值（见 .env.example）")
    cfg.affection_dislike, cfg.affection_acquaintance, cfg.affection_close = 0, 90, 130
ltm_affection = {
    "decay_after_days": cfg.affection_decay_after_days, "decay_per_day": cfg.affection_decay_per_day,
    "min": cfg.affection_min, "max": cfg.affection_max, "start": cfg.affection_start,
    "dislike": cfg.affection_dislike, "friend": cfg.affection_friend,
    "acquaintance": cfg.affection_acquaintance, "close": cfg.affection_close,
    "summary_daily_cap": cfg.affection_summary_daily_cap, "nag_daily_cap": cfg.affection_nag_daily_cap,
}
ltm = LongTermMemory(
    BOT_DIR / cfg.memory_dir,
    client,
    cfg.deepseek_model,
    batch=cfg.memory_batch,
    max_facts=cfg.memory_max_facts,
    max_events=cfg.memory_max_events,
    enabled=cfg.memory_enabled,
    defer=in_peak,            # 高峰时段先不整理长期记忆，攒到低峰再说
)
ltm.affection_cfg = ltm_affection
ltm.facts_by_tier = {**ltm.facts_by_tier, **cfg.memory_facts_by_tier}   # 每档关系最多记几条
ltm.group_batch = cfg.memory_batch_group            # 群里攒几条整理一次
ltm.group_gap = cfg.memory_group_gap_minutes * 60    # 同一个群两次整理至少隔多久（10/03 起）
ltm.close_friends = tuple(cfg.close_friends)
ltm.gender_cap = cfg.gender_cap
ltm.summary_max_tokens = cfg.memory_summary_max_tokens
ltm.summary_client = client.with_options(timeout=cfg.memory_summary_timeout, max_retries=0)   # 整理：超时更长，出错不自动重试
ltm.idle_minutes, ltm.idle_minutes_group = cfg.memory_idle_minutes, cfg.memory_idle_minutes_group   # 聊完了再整理（10/16）
ltm.verify_enabled = cfg.memory_verify
ltm.nightly_max = cfg.memory_nightly_max
if cfg.memory_pro_api_key:                       # pro 单独一个 Key（10/16）：睡前整理默认用它，核对可选
    _pro_client = AsyncOpenAI(api_key=cfg.memory_pro_api_key, base_url=cfg.deepseek_base_url,
                              timeout=cfg.memory_summary_timeout, max_retries=0)
    if cfg.memory_nightly_use_pro:
        ltm.nightly_client, ltm.nightly_model = _pro_client, cfg.memory_pro_model
    if cfg.memory_verify_use_pro:
        ltm.verify_client, ltm.verify_model = _pro_client, cfg.memory_pro_model


# ---- 查档位（10/22 拆分第 4 步从 __init__.py 搬来）
def familiarity_of(qq: int) -> str:
    return ltm.familiarity(qq, cfg.close_friends)


def tier_value(table: dict, fam: str, default: float = 1.0) -> float:
    """按关系取配置里的数。9/29 新加了“普通朋友”，.env 里老的写法没有 friend：取陌生人和熟人的中间值"""
    if fam in table:
        return table[fam]
    if fam == "friend" and "stranger" in table and "acquaintance" in table:
        return (float(table["stranger"]) + float(table["acquaintance"])) / 2
    return default


# 她只有一双手：同一时间只给一个人打字、发消息，其他人排队
_hands = asyncio.Lock()
_last_sent: dict = {"target": None, "at": -1e9}


def _target_of(event: MessageEvent) -> str:
    return f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}"


async def switch_pause(target: str) -> None:
    """刚回完别人（别的群 / 别的私聊）又要回这边：像切换聊天窗口一样停一下"""
    if _last_sent["target"] not in (None, target) and time.monotonic() - _last_sent["at"] < 30:
        await asyncio.sleep(random.uniform(cfg.switch_gap_min, cfg.switch_gap_max))


def mark_sent(target: str) -> None:
    _last_sent["target"], _last_sent["at"] = target, time.monotonic()
    _chat_seq[target] += 1              # 她在这里说了话：之后再回更早的消息，就要引用原句


# 每个会话（群 / 私聊）里：收到一条消息 +1，她自己发完一轮也 +1。
# 她回某条消息时比较一下：这条消息到了以后，中间有别人插话、或者她自己先发了别的话（比如还在回上一个人），
# 就引用原句回复，免得大家不知道她在回哪句；否则直接发
_chat_seq: dict[str, int] = defaultdict(int)
_arrival_seq: dict[tuple[str, int], int] = {}      # (会话, 消息 id) -> 这条消息到的时候的计数


# ---- 出错通知；时间段、次数上限的小工具（10/22 拆分第 7 步从 __init__.py 搬来）
def over_cap(n: int, cap: int) -> bool:
    """每天次数上限：cap 为 0 表示不限（10/19 起插话、冒泡、搭话、写信的每日上限默认都是 0）"""
    return cap > 0 and n >= cap


def cap_text(cap: int) -> str:
    return str(cap) if cap > 0 else "不限"


_last_alert: dict[str, float] = {}


def classify_error(e: Exception) -> str | None:
    code = getattr(e, "status_code", None)
    msg = str(e)
    if code == 402 or "Insufficient Balance" in msg or "余额" in msg:
        return "balance"
    if code == 401 or "Authentication" in msg or "invalid api key" in msg.lower():
        return "auth"
    return None


async def alert_admins(bot: Bot, kind: str, detail: str = "") -> None:
    """私信所有管理员；同一类问题 admin_alert_interval 秒内只提醒一次"""
    now = time.monotonic()
    if now - _last_alert.get(kind, -1e9) < cfg.admin_alert_interval:
        return
    _last_alert[kind] = now
    text = {
        "balance": "【QQ 机器人提醒】DeepSeek 余额不足，伊蕾娜暂时无法回复。请到 https://platform.deepseek.com/ 充值，充值后不用重启。",
        "auth": "【QQ 机器人提醒】DeepSeek API Key 无效或已失效，伊蕾娜暂时无法回复。请检查 .env 里的 DEEPSEEK_API_KEY，改完重开 start.bat。",
        "nokey": "【QQ 机器人提醒】还没有配置 DEEPSEEK_API_KEY，伊蕾娜无法回复。请在 .env 里填好后重开 start.bat。",
    }.get(kind, f"【QQ 机器人提醒】{detail}")
    for su in bot.config.superusers:
        try:
            await bot.send_private_msg(user_id=int(su), message=text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"私信管理员 {su} 失败（需要和机器人是好友）：{e}")


def _in_hours(ranges: str) -> bool:
    now = datetime.now(peak.BEIJING)
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in peak.parse_ranges(ranges))
