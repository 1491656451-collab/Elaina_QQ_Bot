"""
长期记忆

- 每个人一份档案（按 QQ 号，群聊和私聊共用）：喜好、说过的事、和伊蕾娜的约定/梗……
- 每个群一份“往事”：这个群一起聊过、做过的值得记住的事
- 工作方式：每轮对话都先加进“待整理”，攒够一批（默认 8 条）就在后台调用一次模型，
  把要点合并进档案，并按对话内容调整好感度。不影响回复速度。
- 回复时：把“正在说话的人”的档案 + 本群往事，作为一小段提示带给模型。

文件都在 data/memory/ 下：
  users/<QQ号>.json      个人档案
  groups/<群号>.json     群往事 + 昵称→QQ 对照
  pending/<会话>.json    还没整理的旧消息
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from nonebot import logger

from . import budget


_DUP_MARK_RE = re.compile(r"#\d{1,2}$")     # 群里重名的记号（“小明#2”）
_PID = itertools.count(time.time_ns())      # 待整理消息的编号（启动时从当前时间起，重启后也不会重复）


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # 文件坏了（比如写到一半断电）：改名留底，不要直接当成空档案再存回去
        bad = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            path.replace(bad)
            logger.warning(f"记忆文件读不出来，已改名留底：{bad.name}（{e}）")
        except OSError:
            logger.warning(f"记忆文件读不出来：{path}（{e}）")
        return default


def _write(path: Path, data) -> None:
    """先写临时文件再替换：写到一半被打断，原文件也还是完整的"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


SUMMARIZE_PROMPT = """你是“伊蕾娜”（一个 QQ 角色扮演机器人）的记忆整理员。下面是她最近的一段聊天记录，请把值得长期记住的内容合并进档案，并评估好感变化。

今天是 {today}。

【已有的人物档案】
{profiles}

【已有的群往事】（私聊时为空）
{events}

【聊天记录】（“伊蕾娜：”开头的是她自己说的话）
{transcript}

整理规则：
1. 人物档案 facts：只记这个人自己的稳定信息（希望被怎么称呼、喜好、身份、经历、近期计划），以及他和伊蕾娜之间的约定、梗、重要互动。每条不超过 30 字。
2. 把新内容和已有档案合并：去重，过时的更新或删掉，最重要的放前面，每人最多 {max_facts} 条。这段记录里没有这个人的新内容，就原样返回他已有的 facts。
3. 群往事 group_events：这个群里大家一起聊过、玩过的值得记住的事，格式“{md}：……”，每条不超过 40 字。和已有往事合并，最多 {max_events} 条，旧的、不重要的先删。私聊时返回空列表。
4. 不要记：密码、手机号、身份证号、住址、银行卡等隐私；健康、疾病、政治、宗教等敏感信息；寒暄客套；伊蕾娜自己讲的小说故事。
5. 只根据聊天记录，不猜测、不编造。人和 QQ 号的对应以“已有的人物档案”里的列表为准，列表里没有的人不要写。
6. 好感变化 affection：站在伊蕾娜（自恋、爱钱、爱面包、讨厌蘑菇、嘴毒但重情、讨厌被冒犯）的角度，评价这段记录里每个人给她的感受，给一个整数。
   先看档案里标的【关系】——同样的话，关系不同感受完全不同。
   再分清是“玩笑”还是“恶意”：
     - 玩笑：调侃、互损、斗嘴、起外号、拿她的自恋 / 爱钱 / 贪吃 / 身材开玩笑、故意逗她生气。越熟越是日常，普通朋友扣得少，熟人、很熟之间基本不扣。
     - 恶意：辱骂、人身攻击、性骚扰、明知她在意还反复戳痛处、带着恶意的嘲讽和贬低。恶意不看关系，一律按陌生人的标准扣；很熟的人这样做，她反而更受伤，可以扣得更重。
   ● 陌生人（基准）：
     - +1～+5：聊得投机、有趣、尊重她、真诚关心她、夸得她心里舒服、陪她聊她感兴趣的事
     - 0：普通寒暄、没什么感觉
     - -1～-5：无聊纠缠、刷屏、硬要她做不想做的事、一上来就告白/叫老婆/强加关系、提蘑菇之类让她烦的事
     - -6～-15：辱骂、人身攻击、性骚扰、恶意冒犯
   ● 普通朋友：聊过一些、印象不坏。轻度调侃、开玩笑算正常，0～-1；告白、强加关系还是有点冒犯，-1～-3；恶意照样按基准扣
   ● 熟人：调侃、互损、开玩笑算正常打闹，基本不扣（0，过火了最多 -1）；告白、撒娇不算冒犯，看她心情 0～+2；恶意照样按基准扣
   ● 很熟：互损、开玩笑一律不扣（0），这是他们之间的相处方式；真心关心、陪伴、记得她的喜好可以多加 +2～+5；恶意按基准扣，可以更重
   ● 讨厌：她本来就烦这个人，冒犯按基准再重一些；想加分很难，只有特别真诚、明显改过的表现才给 +1～+2
   ● 身材梗不在这里算分：拿她的身材开玩笑（平胸、飞机场、洗衣板等）已经由系统当场按关系扣过，这里不因此再扣；只有同时还有别的恶意（辱骂、骚扰等）才按那部分扣。
   ● 送东西不在这里算分：说送面包、给钱（包括「[给面包]」「[给钱]」「转账」这类写法）已经由系统单独算过，这里不因此加分；付给她合理的报酬不加不减；拿钱引诱、无缘无故撒钱、用她不认识的钱糊弄她，不加分。
   同时写一句不超过 20 字的理由 reason。
7. 性别初判 gender_guess：根据这个人的自称、说话方式、昵称、聊的内容，初步猜一下是“男”还是“女”；拿不准就写“不确定”。这只是猜测。
8. 今日见闻 today_moments：伊蕾娜晚上会写旅行日记（会公开给很多人看）。从这段记录里挑 0～2 件她会想写进日记的事：有趣的话题、有人关心她、有人惹她烦、好笑或让她在意的事。寒暄、没内容的闲聊不算，没有就返回空列表。每件写：
   - qq：主要相关的那个人（必须是“已有的人物档案”列表里的人）
   - event：只写话题层面，不超过 25 字，不写名字、QQ 号，不写具体的私事细节（例如写“聊了工作上的烦心事”，不写“被老板骂了”）
   - mood：她的感受，一两个词（开心、得意、无语、烦、在意、好笑……）
   - keywords：2～4 个关键词，用来给日记挑配图（例如 面包、下雨、读书、猫）

只输出 JSON，格式：
{{"people": [{{"qq": 123456, "facts": ["……", "……"], "affection": 2, "reason": "……", "gender_guess": "不确定"}}], "group_events": ["……"], "today_moments": [{{"qq": 123456, "event": "……", "mood": "……", "keywords": ["……"]}}]}}"""


class LongTermMemory:
    def __init__(self, root: Path, client, model: str, *, batch: int = 8,
                 max_facts: int = 12, max_events: int = 8, enabled: bool = True, defer=None):
        self.root = root
        self.client = client
        self.model = model
        self.batch = batch
        self.max_facts = max_facts
        self.max_events = max_events
        self.enabled = enabled
        self.defer = defer or (lambda: False)   # 返回 True 时先不整理（比如 API 高峰时段）
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)   # QQ -> 整理这个人档案时的锁
        self._tasks: set[asyncio.Task] = set()
        self._running: set[str] = set()         # 正在整理的会话，避免重复开任务
        self._names: dict[int, dict[str, int]] = {}   # 群号 -> {昵称: QQ}
        self._fails: dict[str, int] = {}          # 会话 -> 连续整理失败几次
        self._retry_at: dict[str, float] = {}     # 会话 -> 失败后，这个时间之前先不再试
        # 整理出“今日见闻”后交给谁保存（QQ 空间日记用）：moment_sink(会话, 见闻列表, {QQ: 昵称})
        self.moment_sink = None

    # -------------------------------------------------------------- 存取
    def _user_path(self, qq: int) -> Path:
        return self.root / "users" / f"{qq}.json"

    def _group_path(self, gid: int) -> Path:
        return self.root / "groups" / f"{gid}.json"

    def _pending_path(self, key: str) -> Path:
        return self.root / "pending" / f"{key}.json"

    def get_user(self, qq: int) -> dict:
        prof = _read(self._user_path(qq), None)
        if prof is None:
            start = float(self.affection_cfg.get("start", 0))
            return {"qq": qq, "name": "", "facts": [], "score": start, "score_v": self.SCORE_VERSION}
        if prof.get("score_v") != self.SCORE_VERSION:      # 旧档案：分数按新范围换算一次（存盘时带上版本号）
            if "score" in prof:
                prof["score"] = self.migrate_score(prof["score"])
            prof["score_v"] = self.SCORE_VERSION
        return prof

    def save_user(self, prof: dict) -> None:
        prof["updated"] = int(time.time())
        if prof.get("name"):                      # 群里重名时聊天记录里写成“小明#2”，档案里只存原名
            prof["name"] = _DUP_MARK_RE.sub("", str(prof["name"])).strip()
        _write(self._user_path(int(prof["qq"])), prof)

    def forget_user(self, qq: int) -> bool:
        """删掉长期记忆里的印象（熟悉程度的计数保留）"""
        prof = self.get_user(qq)
        existed = bool(prof.get("facts"))
        prof["facts"] = []
        self.save_user(prof)
        return existed

    def get_group(self, gid: int) -> dict:
        return _read(self._group_path(gid), {"gid": gid, "events": [], "names": {}})

    def save_group(self, g: dict) -> None:
        _write(self._group_path(int(g["gid"])), g)

    def forget_group(self, gid: int) -> bool:
        g = self.get_group(gid)
        had = bool(g.get("events"))
        g["events"] = []
        self.save_group(g)
        return had

    def note_name(self, gid: int, qq: int, name: str) -> None:
        """记下群里 昵称→QQ 的对应，整理记忆时用来认人。每人只留最近 3 个昵称；有变化就马上存盘（整理一直失败也不丢）"""
        if not name:
            return
        m = self._names.setdefault(gid, self.get_group(gid).get("names", {}))
        if m.get(name) == qq:
            return
        m.pop(name, None)
        m[name] = qq                              # 放到最后 = 最新
        mine = [n for n, u in m.items() if u == qq]
        for n in mine[:-3]:
            del m[n]
        try:
            g = self.get_group(gid)
            g["names"] = m
            self.save_group(g)
        except OSError as e:
            logger.warning(f"群昵称对照表存盘失败：{e}")

    # -------------------------------------------------------------- 写入：攒旧消息
    def add_pending(self, key: str, entries: list[dict]) -> None:
        if not self.enabled or not entries:
            return
        path = self._pending_path(key)
        pending = _read(path, [])
        for e in entries:                         # 每条一个编号，整理完按编号删（不按位置删，免得删错）
            pending.append({**e, "_pid": next(_PID)})
        pending = pending[-self.batch * 5:]          # 防止整理一直失败时无限变大
        _write(path, pending)
        if len(pending) >= self.batch:
            self._start(key)

    def _start(self, key: str) -> bool:
        """在后台整理这个会话（高峰时段、正在整理、刚失败过还没到重试时间，就先不整理）"""
        if self.defer() or key in self._running or time.time() < self._retry_at.get(key, 0):
            return False
        self._running.add(key)
        task = asyncio.create_task(self.summarize(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _t, k=key: self._running.discard(k))
        return True

    def retry_pending(self) -> int:
        """把攒够一批、还没整理的会话补做一次（开机时和定时任务用）。返回开始整理了几个"""
        n = 0
        for key in self.pending_keys():
            if len(_read(self._pending_path(key), [])) >= self.batch and self._start(key):
                n += 1
        return n

    def _failed(self, key: str) -> None:
        """整理失败：5 分钟、15 分钟、1 小时后再试（期间来新消息也不马上重试，免得每条消息都白花一次钱）"""
        n = self._fails[key] = self._fails.get(key, 0) + 1
        wait = (300, 900, 3600)[min(n, 3) - 1]
        self._retry_at[key] = time.time() + wait
        logger.warning(f"长期记忆整理失败（{key} 连续第 {n} 次），{wait // 60} 分钟后再试")

    def pending_keys(self, since: float = 0.0) -> list[str]:
        """还有没整理的消息的会话（只看 since 之后改动过的）"""
        d = self.root / "pending"
        if not d.exists():
            return []
        return [f.stem for f in d.glob("*.json") if f.stat().st_mtime >= since]

    def drop_pending(self, key: str) -> None:
        self._pending_path(key).unlink(missing_ok=True)

    # -------------------------------------------------------------- 整理
    def _people_in(self, key: str, entries: list[dict]) -> dict[int, str]:
        people: dict[int, str] = {}
        for e in entries:
            if e.get("uid"):
                people[int(e["uid"])] = e.get("name", "")
            for name, uid in (e.get("speakers") or {}).items():
                people[int(uid)] = name
        if key.startswith("private_"):
            qq = int(key.split("_")[1])
            people.setdefault(qq, "")
        return people

    @staticmethod
    def _transcript(key: str, entries: list[dict]) -> str:
        lines = []
        for e in entries:
            if e["role"] == "assistant":
                lines.append(f"伊蕾娜：{e['content']}")
            elif key.startswith("private_"):
                lines.append(f"对方：{e['content']}")
            else:
                lines.append(e["content"])
        return "\n".join(lines)

    async def summarize(self, key: str, force: bool = False) -> None:
        """force=True：不等攒够一批，有多少整理多少（晚上写日记前的“日结”用）"""
        async with self._locks[key]:
            path = self._pending_path(key)
            entries = _read(path, [])
            if len(entries) < (2 if force else self.batch):
                return
            people = self._people_in(key, entries)
            async with contextlib.AsyncExitStack() as stack:
                for qq in sorted(people):           # 同一个人的档案同一时间只有一处在整理（按 QQ 排序加锁，不会互相卡死）
                    await stack.enter_async_context(self._user_locks[qq])
                await self._summarize(key, path, entries, people)

    async def _summarize(self, key: str, path: Path, entries: list[dict], people: dict[int, str]) -> None:
        gid = int(key.split("_")[1]) if key.startswith("group_") else None
        profiles = {qq: self.get_user(qq) for qq in people}
        group = self.get_group(gid) if gid else None
        prof_text = "\n".join(
            f"- QQ {qq}（昵称：{people[qq] or profiles[qq].get('name') or '未知'}｜关系：{self.TIER_NAMES[self.familiarity(qq, self.close_friends)]}）："
            + (json.dumps(profiles[qq].get("facts", []), ensure_ascii=False))
            for qq in people
        ) or "（无）"
        now = datetime.now()
        prompt = SUMMARIZE_PROMPT.format(
            today=now.strftime("%Y年%m月%d日"),
            md=f"{now.month}月{now.day}日",
            profiles=prof_text,
            events=json.dumps(group["events"], ensure_ascii=False) if group else "[]",
            transcript=self._transcript(key, entries),
            max_facts=self.max_facts,
            max_events=self.max_events,
        )
        if not budget.can_background():
            logger.info(f"长期记忆：今天的钱花完了，{key} 先不整理，留到明天（待整理的消息都留着）")
            return
        try:
            api = self.summary_client or self.client     # 整理单独用更长的超时，出错不自动重试（每次重试都计费）
            resp = await api.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=self.summary_max_tokens,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "memory", user=None, group=None)
            usage = getattr(resp, "usage", None)
            used = getattr(usage, "completion_tokens", None)
            if resp.choices[0].finish_reason == "length":
                logger.warning(f"长期记忆整理写到一半被截断了（写了 {used} token，MEMORY_SUMMARY_MAX_TOKENS={self.summary_max_tokens} 不够），这次作废：{key}")
                self._failed(key)
                return
            data = json.loads(resp.choices[0].message.content or "")
            if not isinstance(data, dict) or not isinstance(data.get("people"), list):
                raise ValueError(f"返回的内容不对（{str(resp.choices[0].message.content)[:60]!r}）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"长期记忆整理失败：{key}：{e}")
            self._failed(key)
            return
        self._fails.pop(key, None)
        self._retry_at.pop(key, None)

        # 先在内存里算好所有人的新档案，最后统一写入；每批记一个编号，同一批重试时不重复加减好感
        batch_id = entries[0].get("_pid") if entries else None
        changed, updated = [], []
        for p in data.get("people", []) or []:
            try:
                qq = int(p.get("qq"))
            except (TypeError, ValueError):
                continue
            if qq not in people:        # 不在这段记录里的人不许改
                continue
            facts = [str(f).strip()[:40] for f in (p.get("facts") or []) if str(f).strip()]
            prof = self.get_user(qq)    # 重新读一遍：整理期间她可能又和这个人聊过（计数、好感已变）
            if people[qq]:
                prof["name"] = people[qq]
            old = prof.get("facts") or []
            if "facts" not in p or (len(old) >= 4 and len(facts) < len(old) / 2):
                # 没给 facts，或者一下子少了一半以上：多半是模型漏写了，保留原来的
                if "facts" in p:
                    logger.warning(f"长期记忆：{qq} 的档案从 {len(old)} 条变成 {len(facts)} 条，不像正常整理，保留原来的")
            else:
                prof["facts"] = facts[: self.max_facts]
            guess = {"男": "male", "女": "female"}.get(str(p.get("gender_guess", "")).strip())
            if guess:
                prof["gender_guess"] = guess
            try:
                delta = int(p.get("affection", 0))
            except (TypeError, ValueError):
                delta = 0
            delta = max(-15, min(5, delta))
            tier = self.familiarity(qq, self.close_friends)
            if tier == "disliked" and delta > 0:     # 讨厌的人想挽回，加分减半（至少 +1）
                delta = max(1, delta // 2)
            done_batches = prof.get("batches") or []
            if batch_id is not None and batch_id in done_batches:
                delta = 0                                # 这一批上次已经算过好感（上次写到一半出错了）
            cap = self.affection_cfg.get("summary_daily_cap", 0)
            if delta > 0 and cap:                    # 每人每天靠整理最多加这么多（聊得再多也不会一天就熟起来）
                today = self._today()
                if prof.get("sum_gain_day") != today:
                    prof["sum_gain_day"], prof["sum_gain_today"] = today, 0
                room = max(0, cap - int(prof.get("sum_gain_today", 0)))
                if delta > room:
                    logger.info(f"长期记忆：{qq} 今天靠聊天已经涨了 {prof.get('sum_gain_today', 0)} 分，这次 +{delta} 只算 +{room}")
                    delta = room
                prof["sum_gain_today"] = int(prof.get("sum_gain_today", 0)) + delta
            if delta:
                self._apply_affection(prof, delta, str(p.get("reason", ""))[:30])
            if batch_id is not None:
                prof["batches"] = (done_batches + [batch_id])[-5:]
            updated.append(prof)
            changed.append(qq)
        for prof in updated:
            self.save_user(prof)
        if group is not None:
            events = [str(x).strip()[:50] for x in (data.get("group_events") or []) if str(x).strip()]
            old_events = group.get("events") or []
            if not isinstance(data.get("group_events"), list) or (len(old_events) >= 4 and len(events) < len(old_events) / 2):
                # 没给群往事，或者一下子少了一半以上：保留原来的
                if isinstance(data.get("group_events"), list):
                    logger.warning(f"长期记忆：群 {gid} 的往事从 {len(old_events)} 条变成 {len(events)} 条，不像正常整理，保留原来的")
            else:
                group["events"] = events[-self.max_events:] if len(events) > self.max_events else events
            group["names"] = self._names.get(gid, group.get("names", {}))
            self.save_group(group)
        if self.moment_sink and data.get("today_moments"):
            try:
                self.moment_sink(key, data.get("today_moments") or [],
                                 {q: _DUP_MARK_RE.sub("", n).strip() for q, n in people.items()})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"今日见闻保存失败：{e}")
        # 只删掉这次整理过的那几条；整理期间新攒的留着下次用
        done = {e["_pid"] for e in entries if e.get("_pid") is not None}
        now_list = _read(path, [])
        remain = [e for e in now_list if e.get("_pid") not in done] if done else now_list[len(entries):]
        if remain:
            _write(path, remain)
        else:
            path.unlink(missing_ok=True)
        logger.info(f"长期记忆已整理：{key}，更新了 {len(changed)} 人的档案")

    # -------------------------------------------------------------- 好感度
    # 分数 -50～150（讨厌 <0｜陌生人 0～39｜普通朋友 40～89｜熟人 90～129｜很熟 130+）。来源：① 正常聊天（默认不加分，可在配置里开）；② 长期记忆整理时按对话内容加减分；
    # ③ 管理员手动调整。新人从 20 起步；很久不聊会慢慢回落到 20（好感和讨厌都会淡去）。
    close_friends: tuple = ()
    summary_max_tokens: int = 4000           # 整理时模型最多写多少（要把每个人的整份档案重写一遍）
    summary_client = None                    # 整理专用的客户端（超时更长、不自动重试）；没设就用聊天那个
    gender_cap: bool = True                  # 只有确认是女生才能到“很熟”
    affection_cfg = {
        "base_gain": 0, "daily_cap": 5,
        "decay_after_days": 7, "decay_per_day": 2,
        "min": -50, "max": 150,
        "start": 20, "dislike": 0, "friend": 40, "acquaintance": 90, "close": 130,
        "summary_daily_cap": 10,
    }
    SCORE_VERSION = 2          # 9/29 换了分数范围：旧档案（-100～100）读的时候换算过来

    @staticmethod
    def migrate_score(old: float) -> float:
        """旧分数（-100～100：讨厌 <-20｜陌生人 -20～29｜熟人 30～69｜很熟 70+，新人 0）换成新分数，档位不变：
        讨厌 -50～0｜陌生人 0～40（新人 20）｜熟人 90～130｜很熟 130～150（新加的“普通朋友”40～90 以后慢慢聊出来）"""
        old = max(-100.0, min(100.0, float(old)))
        if old < -20:
            return round((old + 20) * 50 / 80, 1)
        if old < 0:
            return round(old + 20, 1)
        if old < 30:
            return round(20 + old * 20 / 30, 1)
        if old < 70:
            return round(90 + (old - 30), 1)
        return round(130 + (old - 70) * 20 / 30, 1)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def effective_score(self, prof: dict) -> float:
        """计算衰减后的好感：超过 decay_after_days 天没说话，每天向新人的起始分靠拢 decay_per_day（好感和讨厌都会淡去）"""
        c = self.affection_cfg
        if "score" not in prof:                 # 旧版数据：按说话次数给个初始值
            prof["score"] = self.migrate_score(min(int(prof.get("talks", 0)), 25))
        score = float(prof["score"])
        last = prof.get("last_talk")
        if last:
            idle_days = (time.time() - float(last)) / 86400 - c["decay_after_days"]
            if idle_days > 0:
                dec = idle_days * c["decay_per_day"]
                start = float(c.get("start", 0))
                score = max(start, score - dec) if score > start else min(start, score + dec)
        if self.gender_cap and prof.get("gender") != "female":
            score = min(score, c["close"] - 1)   # 男生、没确认性别的人：最高到熟人
        score = max(float(c.get("min", -50)), min(float(c.get("max", 150)), score))
        return round(score, 1)

    def _apply_affection(self, prof: dict, delta: float, reason: str = "") -> None:
        c = self.affection_cfg
        prof["score"] = max(float(c.get("min", -50)), min(float(c.get("max", 150)), self.effective_score(prof) + delta))
        prof["last_talk"] = prof.get("last_talk") or time.time()
        log = prof.setdefault("affection_log", [])
        log.append(f"{self._today()} {'+' if delta > 0 else ''}{delta:g} {reason}".strip())
        del log[:-8]

    def bump_talk(self, qq: int, name: str = "") -> int:
        """记一次正常聊天：计数 +1，记下时间；base_gain > 0 时好感按每日上限小幅增加（不受 memory_enabled 影响）"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)      # 先把衰减结算掉（旧数据也在这里按次数换算初始分）
        prof["talks"] = int(prof.get("talks", 0)) + 1
        if name:
            prof["name"] = name
        today = self._today()
        if prof.get("gain_day") != today:
            prof["gain_day"], prof["gain_today"] = today, 0
        c = self.affection_cfg
        if c["base_gain"] > 0 and score >= c["dislike"] and prof["gain_today"] < c["daily_cap"]:   # 被讨厌时光聊天不会回暖，得靠内容加分
            score = min(100.0, score + c["base_gain"])
            prof["gain_today"] += c["base_gain"]
        prof["score"] = score
        prof["last_talk"] = prof["last_msg"] = time.time()
        self.save_user(prof)
        return prof["talks"]

    def last_seen(self, qq: int) -> float | None:
        """这个人上一次找她说话的时间（群聊私聊都算）；从没说过话返回 None"""
        prof = self.get_user(qq)
        return prof.get("last_msg") or prof.get("last_talk")

    def user_ids(self) -> list[int]:
        d = self.root / "users"
        return [int(f.stem) for f in d.glob("*.json") if f.stem.isdigit()] if d.exists() else []

    def mark_letter(self, qq: int) -> None:
        prof = self.get_user(qq)
        prof["last_letter"] = time.time()
        self.save_user(prof)

    GENDER_NAMES = {"male": "男", "female": "女"}

    def bread_given_today(self, qq: int) -> bool:
        return self.get_user(qq).get("bread_day") == self._today()

    def take_bread(self, qq: int, delta: float) -> None:
        """收下今天的面包：记一笔，按需加分（每人每天只一次）"""
        prof = self.get_user(qq)
        if prof.get("bread_day") == self._today():
            return
        prof["bread_day"] = self._today()
        if delta:
            self._apply_affection(prof, delta, "送了面包")
        self.save_user(prof)

    def set_gender(self, qq: int, gender: str | None) -> None:
        """确认性别（None = 清除）。好感封顶会跟着变"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)          # 先按旧性别结算一次
        if gender:
            prof["gender"] = gender
        else:
            prof.pop("gender", None)
        prof.pop("gender_pending", None)
        prof["score"] = score
        self.save_user(prof)

    def gender_text(self, prof: dict) -> str:
        g = prof.get("gender")
        if g:
            return f"{self.GENDER_NAMES[g]}（本人确认）"
        guess = prof.get("gender_guess")
        return f"未确认（感觉像{self.GENDER_NAMES[guess]}生）" if guess else "未确认"

    def taboo_penalty(self, qq: int, penalty: float, daily_max: float, reason: str) -> float:
        """踩雷立刻扣分，每人每天有上限"""
        prof = self.get_user(qq)
        today = self._today()
        if prof.get("taboo_day") != today:
            prof["taboo_day"], prof["taboo_today"] = today, 0.0
        cut = min(penalty, max(0.0, daily_max - prof["taboo_today"]))
        if cut > 0:
            prof["taboo_today"] += cut
            self._apply_affection(prof, -cut, reason)
            self.save_user(prof)
        return prof.get("score", float(self.affection_cfg.get("start", 0)))

    def adjust(self, qq: int, delta: float | None = None, set_to: float | None = None, reason: str = "管理员调整") -> float:
        prof = self.get_user(qq)
        if set_to is not None:
            delta = set_to - self.effective_score(prof)
        self._apply_affection(prof, float(delta or 0), reason)
        prof["last_talk"] = time.time()
        self.save_user(prof)
        return prof["score"]

    def familiarity(self, qq: int, close_friends=()) -> str:
        if qq in close_friends:
            return "close"
        c = self.affection_cfg
        score = self.effective_score(self.get_user(qq))
        if score < c["dislike"]:
            return "disliked"
        if score >= c["close"]:
            return "close"
        if score >= c["acquaintance"]:
            return "acquaintance"
        if score >= c.get("friend", c["acquaintance"]):
            return "friend"
        return "stranger"

    # -------------------------------------------------------------- 读取：给模型的提示
    def context_for(self, qq: int, name: str, gid: int | None) -> str:
        if not self.enabled:
            return ""
        parts = []
        prof = self.get_user(qq)
        who = name or prof.get("name") or qq
        if prof.get("gender"):
            parts.append(f"「{who}」是{'女生' if prof['gender'] == 'female' else '男生'}（对方亲口确认过）。")
        elif prof.get("gender_guess"):
            parts.append(f"你隐约觉得「{who}」可能是{'女生' if prof['gender_guess'] == 'female' else '男生'}，但只是猜的：别说破，也别问。")
        if prof.get("facts"):
            parts.append(f"你对「{who}」的印象：" + "；".join(prof["facts"]))
        if gid:
            events = self.get_group(gid).get("events", [])
            if events:
                parts.append("这个群的往事：" + "；".join(events))
        if not parts:
            return ""
        return (
            "【长期记忆】以下是你从以前的聊天里记住的事，自然地运用，别逐条复述，"
            "也别说“我记录里写着”。记错了就以对方现在说的为准。\n" + "\n".join(parts)
        )

    # -------------------------------------------------------------- 给管理员看
    TIER_NAMES = {"disliked": "讨厌", "stranger": "陌生人", "friend": "普通朋友", "acquaintance": "熟人", "close": "很熟"}

    def describe_affection(self, qq: int, close_friends=()) -> str:
        prof = self.get_user(qq)
        tier = self.TIER_NAMES[self.familiarity(qq, close_friends)]
        log = prof.get("affection_log", [])[-5:]
        lines = "\n".join(f"· {x}" for x in log) or "· （还没有变化记录）"
        head = f"（{prof.get('name') or qq}｜好感 {self.effective_score(prof):g}｜{tier}｜说过 {int(prof.get('talks', 0))} 次话）\n性别：{self.gender_text(prof)}"
        if prof.get("last_letter"):
            days = (time.time() - prof["last_letter"]) / 86400
            head += f"\n上次给他写信：{'今天' if days < 1 else f'{int(days)} 天前'}"
        return f"{head}\n最近变化：\n{lines}"

    def describe_user(self, qq: int) -> str:
        prof = self.get_user(qq)
        talks = int(prof.get("talks", 0))
        head = f"说过 {talks} 次话｜好感 {self.effective_score(prof):g}"
        if not prof.get("facts"):
            return f"（关于 {qq} 还没有长期记忆｜{head}）"
        lines = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(prof["facts"]))
        return f"（{prof.get('name') or qq}｜QQ {qq}｜{head}）\n{lines}"

    def describe_group(self, gid: int) -> str:
        events = self.get_group(gid).get("events", [])
        if not events:
            return f"（群 {gid} 还没有往事记录）"
        return f"（群 {gid} 的往事）\n" + "\n".join(f"{i + 1}. {e}" for i, e in enumerate(events))
