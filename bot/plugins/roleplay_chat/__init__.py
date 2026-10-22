"""
角色扮演聊天插件
- 群聊：被 @、被回复、被叫名字时回复；看得出在跟她说话或提到她时也会回复（先让模型判断一下）；紧接着回复时直接发，中间有人插话才用“回复”引用
- 私聊：直接回复（可在 .env 关闭）
- 人设：personas/*.md，支持热重载
- 记忆：每个群 / 每个私聊一份，落盘到 data/history，重启不丢
- 风控：个人冷却、全局限速、随机打字延迟
- 知识库：从小说摘要和原文中检索相关片段，作为“回忆”提供给模型
- 长期记忆：每个人的档案 + 群往事，每轮对话攒够一批就在后台整理而成
- 出错时不在群里发消息；余额不足 / Key 失效会私信管理员
- 时间感：知道对方隔了多久才来找她、上一段聊天是多久以前
- 写信：好感到“熟人”及以上的好友（很熟的人收得更勤），隔一段时间没来找她时，她偶尔会主动私聊寄一封信
- 节奏：同一时间只给一个人打字（其他人排队）；按字数算打字时间；不在线时的私聊，上线后补回
- 表情包：有一定概率用小号收藏表情里的伊蕾娜表情表达情绪（跟在文字后面，或者只发一张），见 stickers.py

文件分布（10/22 拆分后；这个文件只剩把各部分接起来：导入、再导出、启动和关机时开关后台任务）
- 共用：core.py（配置、模型客户端、记账、长期记忆对象、查档位、“一双手”、出错通知）
- 聊天 agent：prompts.py（所有给模型的文字）、history.py（聊天记录）、textin.py（收到的消息变成文字）、
  textout.py（发前清理）、turn_hints.py（每轮提示）、addressed.py（要不要回）、reply.py（回消息主流程）、
  story.py（查小说资料、核对往事）、proactive_group.py（旁听、插话、冒泡）、proactive_private.py（写信、搭话）、
  catchup.py（补回未读、上线断线）、friends.py（加好友）、commands.py（/重置、/重载人设）
- 限额 agent：quota.py（风控、额度、告别、/花费）、sleep.py（几点睡）、goodnight.py（到点道晚安）
- 好感 agent：affection.py（分数和档位，LongTermMemory 继承它）、affection_turn.py（每轮的性别、送礼判断，/好感、/性别）
- 记忆 agent：memory.py、memory_chat.py（/记忆 等指令、定时补做）
- 语音 agent：voice.py、voice_chat.py；表情 agent：stickers.py、stickers_chat.py；识图 agent：vision.py、vision_chat.py
- 部署 agent：online.py（掉线提醒）；空间 agent：qzone.py、qzone_diary.py、gallery.py
- 测试要替换的函数，调用处一律写成“文件.函数()”（比如 core.in_peak()、sleep.asleep()），测试替换那个文件里的才生效
"""
import asyncio
import contextlib
import json
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import get_bots, get_driver, logger, on_command, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me

from . import budget, factcheck, knowledge, peak, voice
from .stickers import EMOTIONS
from .stickers import normalize as sticker_normalize
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="角色扮演聊天",
    description="DeepSeek 驱动、带人设和上下文记忆的聊天机器人",
    usage="群里 @机器人 说话；/重置 清空短期记忆；管理员：/重载人设、/记忆、/忘记、/好感、/花费、/写信、/说说、/表情、/语音",
    config=Config,
)

from .core import (  # noqa: E402  共用的对象搬到了 core.py（10/22 拆分第 1 步），这里再导出一遍，别处照旧能 from . import
    BOT_DIR, HISTORY_DIR, _SAME_NAMES_FILE, _vision_sanitize, cfg, client, in_peak, ltm, ltm_affection, spend, stickers, tagger, vision,
)
from . import prompts  # noqa: E402
from .prompts import (  # noqa: E402  给模型的文字搬到了 prompts.py（10/22 拆分第 1 步）
    CHAT_RULES, LATE_HINT, RECALL_RULES, STORY_CHECK_PROMPT, STORY_FIX_PROMPT, SHORT_VARIANTS,
    LENGTH_HINT, UNPROMPTED_HINT, FAMILIARITY_HINT, VOICE_REQUEST_HINT, VOICE_REQUEST_TIER_HINT, VOICE_OFFER_HINT,
    JUDGE_PROMPT, WINDDOWN_SLEEP_HINT, WINDDOWN_TIRED_HINT, WRAPUP_HINT, WRAPUP_SLEEP_HINT, INTERJECT_PROMPT,
    BUBBLE_PROMPT, LETTER_PROMPT, NUDGE_PROMPT, VOICE_UNAVAILABLE_HINT,
    load_persona, system_prompt,
)
from . import textin, textout, turn_hints  # noqa: E402,F401  10/22 拆分第 2 步：聊天记录（history.py）、收到的消息、发前清理、每轮提示
from .history import (  # noqa: E402
    _histories, _passive, _locks, session_key, _hist_path, get_history, save_history, clear_history, api_messages,
)
from .turn_hints import (  # noqa: E402
    human_gap, _WEEKDAY, BACK_HINT_HOURS, clock_text, _birthday_md, now_hint, time_hint, _LONG_HINTS, reply_mode,
    short_hint, _VENT_HINTS, _STORY_HINTS, length_hint_for, unprompted_hits,
)
from .textin import (  # noqa: E402
    seg_text, _short, face_name, card_info, _UNREADABLE_KINDS, unreadable_kind, message_to_text, edge_at_positions,
    edge_ats, middle_at_me, group_body, _BOT_SEGMENTS, _BOT_SEGMENTS_PRIVATE, is_bot_like, talking_to_others,
    _HEAD_BAD_RE, _clean_name, _DUP_MARK_RE, SAME_NAME_ACTIVE_DAYS, _names_reg, _save_names_reg,
    name_label, sender_label, clean_body, _her_names, _display_name, speaker_head, gap_line, _TAG_ONLY_RE,
    _HEAD_ONLY_RE, AT_ONLY_TEXT, is_call_only, pull_own_lead, CALLER_LEAD_MARK, WATCH_GAP_MIN, short_gap,
    watch_block, caller_lead_start, _only_media, QUOTE_MAX_CHARS, quote_note, rich_text, _rich_text, sender_name,
    _strip_her_names, _CALL_ONLY_RE,
)
from .textout import (  # noqa: E402
    _PREFIX_RE, _VOICE_MARK_RE, _VOICE_TAG_ANY_RE, _VOICE_PAREN_RE, take_voice_marks, _PAREN_LINE_RE,
    _unwrap_paren_line, _PSEUDO_TAG_RE, clean_reply, split_bubbles, _ACTION_PAREN_RE, _META_PAREN_RE, CHAT_STOP,
    _LATIN_RE, _LATIN_OK, cut_derailed, _ONE_CHAR_OK, is_fragment, speaker_names, drop_ooc_sentences, _STICKER_RE,
    _FAKE_MEDIA_RE, split_sticker,
)
from . import core, quota, sleep  # noqa: E402,F401  10/22 拆分第 4 步：查档位和“一双手”进了 core；晚上休息、限额
from .core import (  # noqa: E402
    familiarity_of, tier_value, _hands, _last_sent, _target_of, switch_pause, mark_sent, _chat_seq, _arrival_seq,
)
from .sleep import (  # noqa: E402
    _goodnight, night_key, sleep_window, asleep,
)
from .quota import (  # noqa: E402
    _last_trigger, _global_window, _hour_window, _group_hour, _private_hour, _farewell_at, _active_chats, _scope,
    _NO_LIMIT, _rounds, quota_left, WRAPUP_EXTRA, WRAPUP_MINUTES, _wrapup, in_wrapup, rate_limited,
    _crossed_mid_chat, _refund, _has_quota, _count_sent, typing_delay, bubble_gap, TIRED_LINES, _tired_sent,
    say_tired, say_farewells, winddown_hint, _sleep_wrapup, cost_cmd,
)
from . import affection_turn, memory_chat, stickers_chat, vision_chat, voice_chat  # noqa: E402,F401  10/22 拆分第 5 步：语音、表情、识图、记忆指令、每轮好感判断
from .voice_chat import (  # noqa: E402
    VOICE_REQUEST_RE, _voice_count, voice_day, tts_ready, voice_roll, split_voice_mark, _EMOJI_RE, voice_text,
    VOICE_JA_PROMPT, translate_ja, voice_wav_seconds, voice_files, tts, interjections, voice_cmd, VOICE_USAGE,
    _send_voice,
)
from .stickers_chat import (  # noqa: E402
    TIER_EMOTIONS, _last_sticker, _recent_stickers, sticker_roll, sticker_hint, sticker_cmd, STICKER_USAGE,
    _spawn_labeling, _sticker_loop,
)
from .vision_chat import (  # noqa: E402
    see_cmd, SEE_USAGE,
)
from .memory_chat import (  # noqa: E402
    _parse_target, MEMORY_USAGE, mem_show, mem_forget, mem_edit, _NIGHTLY_FILE, _nightly_due, _run_nightly,
    _memory_retry_loop,
)
from .affection_turn import (  # noqa: E402
    _F_WORDS, _M_WORDS, _CLAIM_RE, _YES_RE, _NO_RE, gender_claim, gender_step, _BREAD, _GIVE, _TO_HER, _BREAD_RE,
    _NUM, _WORLD, _FOREIGN, _PAY, _WORLD_AMT, _FOREIGN_AMT, _REWARD, _MONEY_WORLD_RE, _MONEY_FOREIGN_RE,
    _MONEY_VAGUE_RE, _not_negated, detect_gifts, aff_cmd, gender_cmd,
)
from .turn_hints import _MONEY_RULE, gift_hint  # noqa: E402
from . import addressed, commands, online  # noqa: E402,F401  10/22 拆分第 6 步：掉线提醒、加好友、指令、要不要回
from .history import _recent_chat  # noqa: E402
from .textin import clip_input  # noqa: E402
from .online import (  # noqa: E402
    OFFLINE_FILE, quiet_left, start_quiet, _read_offline, _write_offline, note_offline, offline_message,
    should_alert, send_offline_alert, _is_offline_event,
)
from .friends import (  # noqa: E402
    FRIEND_REQ_FILE, _FRIEND_SYS_RE, _friend_added, _friend_reqs, _save_friend_reqs, _norm, is_friend_verify,
    _is_friend_request, friend_request, _is_friend_add, friend_add_notice,
)
from .commands import (  # noqa: E402
    ADMIN_COMMANDS, _SLASH_STARTS, is_slash_message, is_admin_command, reset_cmd, _BULK_RESET, _bulk_pending,
    _session_keys, clear_all, reload_cmd,
)
from .addressed import (  # noqa: E402
    _allowed, allowed, _is_group_not_to_me, note_arrival, _is_group, counter, _speakers, _replied_at,
    others_spoke_since, _last_bot_msg, _last_smart, _engaged, _Q_TAILS, _looks_like_question, _as_third_person,
    _judge, _addressed, _last_self_react, _self_react_seen, _self_image_react,
)
from . import proactive_group, proactive_private  # noqa: E402,F401  10/22 拆分第 7 步：群里、私聊主动说话
from .core import _in_hours, _last_alert, alert_admins, cap_text, classify_error, over_cap  # noqa: E402
from .addressed import _LEAVE_RE, _away, away_left, note_leaving  # noqa: E402
from .history import _inbox, _unrecord  # noqa: E402
from .proactive_group import (  # noqa: E402
    passive, INTERJECT_FILE, _interject_busy, _interject_skip_until, _NO_INTERJECT_RE, _interject_state,
    _save_interject_state, interject_blocker, _take_group_quota, maybe_interject, _interject, interject_cmd,
    _NO_BUBBLE_RE, _group_last_activity, bubble_blocker, bubble, check_bubbles, _bubble_loop, bubble_cmd,
)
from .proactive_private import (  # noqa: E402
    _letters_sent, _letter_window_now, letter_daily_prob, _letter_prob_per_check, letter_blocker, write_letter,
    check_letters, _letter_loop, _NO_NUDGE_RE, _BYE_RE, _nudge_due, _nudge_count, arm_nudge, nudge_blocker, nudge,
    check_nudges, _nudge_loop, nudge_cmd, letter_cmd,
)
from . import goodnight, reply, story  # noqa: E402,F401  10/22 拆分第 8 步：查小说资料和核对往事、到点道晚安、回消息主流程
from .story import (  # noqa: E402
    _story_evidence, _tells_story, story_check, _FOLLOWUP_WORDS, brief_profile, RECALL_CARRY_MINUTES,
    RECALL_CARRY_TURNS, _last_recall, recall, _diary_topic, diary_followup, note_topic,
)
from .goodnight import (  # noqa: E402
    _said_bye_last, goodnight_now, _sleep_loop,
)
from .reply import (  # noqa: E402
    max_tokens_for, chat, _peak_last_real, _peak_last_busy, _inbox_token, _inbox_first, _look_later, has_images,
    _take_inbox, look_at_images, _reply_started, _reply_done, is_sticker_only, sticker_follows_reply, wait_own_reply,
    _UNFINISHED_TAIL, _UNFINISHED_WORDS, merge_delay, wait_for_more, _FILLER_RE, _SKIP_RE, is_filler, she_asked,
    converse, _converse,
)


# 出戏检查：标准在 oocheck.py（线上和回归测试共用）。她自己冒出来的、或者当成懂的概念来用的，才算出戏
from .oocheck import OOC_RE as _OOC_RE, ooc_words  # noqa: E402,F401
from .echocheck import echo_hint  # noqa: E402


# ------------------------------------------------------------------ 后台任务的句柄（启动、关机时用）
# 循环本身在各自的文件里：道晚安 goodnight.py，冒泡 proactive_group.py，写信、冷场搭话 proactive_private.py，
# 补做记忆整理和睡前整理 memory_chat.py
_sleep_task: "asyncio.Task | None" = None
_bubble_task: "asyncio.Task | None" = None
_letter_task: "asyncio.Task | None" = None
_nudge_task: "asyncio.Task | None" = None
_memory_task: "asyncio.Task | None" = None


@get_driver().on_shutdown
async def _():
    for t in (_letter_task, catchup._heartbeat_task, catchup._catchup_task, catchup._sticker_task, stickers_chat._label_task, _bubble_task, _memory_task, _nudge_task, _sleep_task):
        if t:
            t.cancel()


@get_driver().on_startup
async def _():
    global _letter_task, _bubble_task, _memory_task, _nudge_task, _sleep_task
    if cfg.sleep_enabled:
        _sleep_task = asyncio.create_task(_sleep_loop())
    if cfg.nudge_enabled and cfg.enable_private:
        _nudge_task = asyncio.create_task(_nudge_loop())
    if cfg.memory_enabled and cfg.deepseek_api_key:     # 10/16 起这个循环还管“聊完了再整理”和睡前整理，不再只看 MEMORY_RETRY_MINUTES
        _memory_task = asyncio.create_task(_memory_retry_loop())
    if tagger:
        tagger.start()     # 后台准备角色识别模型（第一次会下载），不耽误启动
    if cfg.letter_enabled and cfg.enable_private:
        _letter_task = asyncio.create_task(_letter_loop())
    if cfg.bubble_enabled and cfg.enable_group:
        _bubble_task = asyncio.create_task(_bubble_loop())
    if cfg.knowledge_enabled:
        try:
            story._kb = await asyncio.to_thread(
                knowledge.build_or_load,
                BOT_DIR / cfg.knowledge_summary_dir,
                BOT_DIR / cfg.knowledge_novel_dir,
                BOT_DIR / cfg.knowledge_cache,
                BOT_DIR / cfg.knowledge_characters_file,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"知识库加载失败，将不使用小说检索：{e}")
    if cfg.fact_check:
        story._fc = await asyncio.to_thread(
            factcheck.load, BOT_DIR / cfg.knowledge_summary_dir, BOT_DIR / cfg.knowledge_characters_file,
            BOT_DIR / cfg.knowledge_places_file, prompts._persona,
        )
    if not cfg.deepseek_api_key:
        logger.warning("未配置 DEEPSEEK_API_KEY，机器人会提示未配置")
    year = str(datetime.now(peak.BEIJING).year)
    if cfg.peak_enabled and not any(d.startswith(year) for d in list(peak.HOLIDAYS_2026) + list(cfg.peak_holidays)):
        logger.warning(f"高峰时段：没有 {year} 年的法定节假日数据，节假日会被当成工作日高峰。"
                       f"请在 .env 的 PEAK_HOLIDAYS 里补上，例如 PEAK_HOLIDAYS=[\"{year}-01-01\"]")
    logger.info(
        f"角色扮演聊天已启动：model={cfg.deepseek_model} 记忆=群聊{cfg.history_max_turns_group}条/私聊{cfg.history_max_turns}条 "
        f"群白名单={cfg.group_whitelist or '全部'} 长期记忆={'开' if cfg.memory_enabled else '关'} "
        f"高峰时段模式={'开' if cfg.peak_enabled else '关'}（现在{'是' if core.in_peak() else '不是'}高峰）"
    )
    logger.info(
        f"表情包：{'开' if cfg.sticker_enabled else '关'}（{stickers.summary()}）" + ("，连上 QQ 后会自动拉取收藏表情" if cfg.sticker_enabled else "")
    )


# 补回未读、上线 / 断线时要做的事 → catchup.py（10/22 拆分第 6 步）：它要用到上面的回消息主流程，所以放在最后导入
from . import catchup  # noqa: E402
from .catchup import (  # noqa: E402
    kicked_notice, _back_after_kick, back_after_kick, ONLINE_FILE, _seen_ids, _load_online, _save_online,
    _is_private, seen_private, _seg_list, catchup_event, find_unread, catch_up, _heartbeat_loop, start_catchup,
    _on_connect,
)


# ------------------------------------------------------------------ QQ 空间日记（放在最后导入：它要用到上面定义的东西）
from . import qzone_diary  # noqa: E402
reply.qzone_diary = qzone_diary   # 回消息主流程里要用（见 reply.py 开头）
