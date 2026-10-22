"""她自己的近况（10/23，《方案-她的生活-近况与随机事件-1023》《交接-近况接口-聊天与记忆-1023》）

长期记忆原来只有“每个人一份档案”和“每个群一份往事”，没有她自己的：冒泡、写信里讲的旅途小事挤出聊天记录就忘了，
被问“你上次说的猫呢”接不上；不同地方还会讲出几个版本。这里只记**她自己说过的**（《方案-她的近况-1023》，取代随机事件那版）：

- 她说过的“自己最近的经历”（items）：来源是她主动说的话（冒泡、写信、搭话），以后还有整理聊天时收的（self_events）；
- 她说自己现在在哪（place）、之前在哪（trail）：也从她的话里来，说了“到了……”就换，程序不替她排路线；
- 给聊天用的两个取用函数：每轮回消息（life_for_chat）、主动说话（life_for_proactive）。只返回内容，提示文字归聊天（prompts.py）。

数据：data/memory/self.json。写文件沿用 memory.py 的 _write（先写临时文件再替换），读坏了改名留底（_read）。
/忘记、/重置 都不碰这份。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime
from pathlib import Path

from nonebot import logger

from . import budget
from .memory import _clean_text, _grams, _her_topics, _norm, _read, _related, _similar, _write
from .peak import BEIJING

TRAIL_MAX = 10                 # 最近走过的地方留几段
SAME_MIN_GRAMS = 2               # “讲的就是已有的那件”：原话和那件事至少有几个两字词对得上
TOLD_HERE_MAX = 3              # 主动说话时，“这里已经听过的”给几条
TOPIC_CAP_STORE = 3            # 面包、钱、蘑菇这类：两周内最多存几条（从源头防滚雪球，10/03 面包的教训）
CHAT_CHARS = 100               # 每轮【你的近况】大概多少字
SAID_RETRY_SECONDS = 600       # 主动说的话整理失败：隔多久再试
SAID_MAX_PENDING = 20          # 没整理完的最多攒几段（再多丢最早的）
KINDS = ("城镇", "野外", "小说地点")
_SHOP_RE = re.compile(r"(店|馆|堂|铺|屋|房间|集市|市场|摊)$")               # 只是一家店、一间屋子，不算“在哪”
_PLACE_CHARS = "镇城国村林山谷湖河岛"
_PLACE_WORD_RE = re.compile(r"到了|来到|回到|落脚|住下|镇|城|国|村|森林|林子|山|谷|湖|草原|荒野|路上|赶路")   # 原话里有这些才可能是在说在哪

SAID_PROMPT = """你是“伊蕾娜”（一个 QQ 角色扮演机器人，正在独自旅行的魔女）的记忆整理员。她刚刚主动说了一段话（冒泡、写信或者搭话），请从里面找出她讲成“自己最近经历”的事，和她说自己现在在哪。用户消息里有：她之前说过自己在哪、她最近说过的经历（#编号）、她刚说的原话。只输出 JSON。

规则：
1. 原话讲的就是“最近说过的经历”里的某一条（同一件事，换了说法也算），把编号写进 same，不要再写进 new。
2. 原话里她讲成自己最近真的经历过的事（到了哪里、遇到什么、接了什么委托、看到吃到什么；很小的事也算，比如被路人问了句什么、排了半天队），而“最近说过的经历”里没有的，写进 new：每件一句，不超过 40 字，省略主语，只写发生了什么（比如“在……被……”），不写她说话的语气；mood 写她讲这件事时的心情，两三个字，看不出就空着。
3. 这些不算，不要写：问对方的话、打招呼、只吐槽天气这类没有具体事情的话；玩笑、比喻、假设（“要是……”）；小说里以前的旅途往事（以前遇到的人、很久以前去过的地方）；别人的事；她以后打算做的事。分不清是不是真经历的，不写。
4. place：只有她在这段原话里**明说了自己现在在哪**（“到了……”“在……落脚”“这几天都在……赶路”“又回到了……”）才写，写 {"name": "地名，没有名字就写“某片森林”“往东的山路”这样的说法，不超过 15 字", "kind": "城镇、野外、小说地点 三选一（小说地点是她以前在旅途里去过、这次又回去的地方），判断不出来就空着", "moved": true 或 false}。moved：她说的是到了新地方、换了地方写 true；只是提到现在待的地方、没换写 false。没说自己在哪就不写 place。这些都不算说了在哪，不要写 place：
   - 只提到一家店、一间屋子（面包店、旅店、澡堂、集市），地方要是城镇、国家、森林、山路这一级；
   - 从她做的事、天气推出来的（“找棵树歇会儿”不等于说在树林里）；
   - 以前、小说里的往事里的地方（“以前在……的时候”）；
   - 用户消息里给你的“她之前说自己在哪”：原话里没再提，就不要照抄过来。
5. fits：new 里每件事和她现在在哪对不对得上。她这次说了换地方的，按新的地方看；没说换地方，人在野外、森林里却讲“刚在镇上的店里……”，就是对不上，写 false。说的是前几天在上一个地方的事，也算对得上。
6. 什么都没有就输出空的。

格式：{"same": [3], "new": [{"text": "……", "mood": "好笑", "fits": true}], "place": {"name": "……", "kind": "城镇", "moved": true}}"""


def _now() -> float:
    return time.time()


def _bj(ts: float) -> datetime:
    """一律按北京时间算“今天”“上午”（10/23 聊天 agent 提的：服务器不一定是北京时间）"""
    return datetime.fromtimestamp(ts, BEIJING)


def _day_of(ts: float) -> str:
    return _bj(ts).date().isoformat()


def _days_between(day: str | None, now: float) -> int:
    try:
        return (_bj(now).date() - date.fromisoformat(str(day)[:10])).days
    except (TypeError, ValueError):
        return 0


def _ago(day: str | None, now: float | None = None) -> str:
    """“今天”“昨天”“3 天前”“2 周前”（按北京时间）"""
    n = _days_between(day, now or _now())
    if n <= 0:
        return "今天"
    if n == 1:
        return "昨天"
    if n < 7:
        return f"{n} 天前"
    return f"{n // 7} 周前" if n < 30 else f"{n // 30} 个多月前"


def _slot(ts: float) -> str:
    h = _bj(ts).hour
    if h < 5:
        return "深夜"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    return "晚上"


def _as_list_of_dict(v) -> list[dict]:
    return [x for x in (v if isinstance(v, list) else []) if isinstance(x, dict)]


def _when_of(it: dict, now: float) -> str:
    """“今天下午”“昨天上午”“3 天前”"""
    at = float(it.get("at") or 0)
    if not at:
        return ""
    d = (_bj(now).date() - _bj(at).date()).days
    if d <= 0:
        return f"今天{_slot(at)}"
    if d == 1:
        return f"昨天{_slot(at)}"
    return _ago(_day_of(at), now)


class SelfMemory:
    def __init__(self, path: Path, client=None, model: str = "", *, enabled: bool = True,
                 max_items: int = 30, keep_days: int = 14, place_stale_days: int = 5):
        self.path = Path(path)
        self.client = client
        self.model = model
        self.enabled = enabled
        self.max_items = max_items
        self.keep_days = keep_days
        self.place_stale_days = place_stale_days   # 说在某处以后这么多天没再提：给她看“前几天说的，不一定还在”
        self.ltm = None                      # 写信时往收信人档案的“她说过”里记一条（core.py 里接上）
        self._data: dict | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._retry_at = 0.0

    # -------------------------------------------------------------- 读写
    def _load(self) -> dict:
        if self._data is None:
            d = _read(self.path, {})
            d = d if isinstance(d, dict) else {}
            d.setdefault("place", {})
            d.setdefault("trail", [])
            d.setdefault("items", [])
            d.setdefault("said", [])          # 主动说的、还没整理的原话
            d.setdefault("next_id", 1)
            self._data = d
        return self._data

    def _save(self) -> None:
        d = self._load()
        self._trim(d)
        _write(self.path, d)

    def _items(self) -> list[dict]:
        return [it for it in self._load()["items"] if isinstance(it, dict)]

    def _trim(self, d: dict, now: float | None = None) -> None:
        """超过两周的忘掉；多了先忘不重要的、讲过的地方少的、旧的。还没到发生时间的不动"""
        now = now or _now()
        cutoff = now - self.keep_days * 86400
        items = [it for it in d["items"] if isinstance(it, dict) and float(it.get("at") or 0) >= cutoff]
        if len(items) > self.max_items:
            future = [it for it in items if float(it.get("at") or 0) > now]
            past = [it for it in items if float(it.get("at") or 0) <= now]
            past.sort(key=lambda it: (int(it.get("weight") or 2), len(it.get("told_in") or []), float(it.get("at") or 0)))
            past = past[max(0, len(past) + len(future) - self.max_items):]
            keep = {id(x) for x in past + future}
            items = [it for it in items if id(it) in keep]
        d["items"] = items
        d["trail"] = [t for t in d["trail"] if isinstance(t, dict)][-TRAIL_MAX:]
        d["said"] = [s for s in d["said"] if isinstance(s, dict)][-SAID_MAX_PENDING:]

    def _new_id(self) -> str:
        d = self._load()
        n = int(d.get("next_id") or 1)
        d["next_id"] = n + 1
        return f"e{n}"

    def _blocked(self, text: str, now: float) -> str:
        """收不收：和最近的重复、她爱提的话题两周内已经够多。返回挡下的原因，收就返回空"""
        for it in self._items():
            if _norm(it.get("text", "")) == _norm(text) or _similar(text, it.get("text", "")):
                return f"和 {it.get('id')} 重复"
        tops = _her_topics(text)
        for t in tops:
            n = sum(1 for it in self._items() if t in _her_topics(it.get("text", "")))
            if n >= TOPIC_CAP_STORE:
                return f"“{t}”这类已经有 {n} 条"
        return ""

    def _add(self, text: str, *, source: str, at: float, where: str | None = None, where_kind: str | None = None,
             mood: str = "", weight: int = 2, told_in=None) -> dict | None:
        text = _clean_text(text, 60)
        if not text:
            return None
        why = self._blocked(text, at)
        if why:
            logger.info(f"近况：没收“{text}”（{why}）")
            return None
        place = self._load()["place"]
        it = {"id": self._new_id(), "text": text,
              "where": where or place.get("name") or "", "where_kind": where_kind or (place.get("kind") if not where else "") or "",
              "date": _day_of(at), "time": _slot(at), "at": float(at), "source": source,
              "mood": _clean_text(mood, 8), "weight": int(weight), "told_in": list(told_in or [])}
        self._load()["items"].append(it)
        logger.info(f"近况：记下 {it['id']}（{source}，{it['date']} {it['time']}）{text}")
        return it

    # -------------------------------------------------------------- 现在在哪（从她的话里来）
    @staticmethod
    def _same_place(a: str, b: str) -> bool:
        a, b = _norm(a), _norm(b)
        return bool(a and b) and (a == b or a in b or b in a)

    def _move_to(self, name: str, kind: str = "") -> None:
        """她说到了新地方：换 place，旧的进 trail"""
        d = self._load()
        today = _day_of(_now())
        old = d.get("place") or {}
        if old.get("name"):
            d["trail"].append({"name": old.get("name"), "kind": old.get("kind") or "", "from": old.get("since"), "to": today})
        d["place"] = {"name": _clean_text(name, 15), "kind": kind if kind in KINDS else "", "since": today}
        logger.info(f"近况：她说现在在 {d['place']['name']}" + (f"（{d['place']['kind']}）" if d["place"]["kind"] else ""))

    def _apply_place(self, pl, said: str = "") -> None:
        """模型认出的“她说自己在哪”：说了换地方就换；没说换、又和现在的对不上，不更新（日志记一行）。
        10/23 回放：模型会把“面包店该出炉了”认成在面包店、把给它看的“之前在哪”照抄回来，程序再挡两道"""
        if not isinstance(pl, dict) or not _clean_text(pl.get("name"), 15):
            return
        name, kind = _clean_text(pl.get("name"), 15), str(pl.get("kind") or "")
        if _SHOP_RE.search(name):
            logger.info(f"近况：“{name}”只是一家店、一间屋子，不当成她在哪")
            return
        if said and not (_grams(name) & _grams(said)) and not _PLACE_WORD_RE.search(said):
            logger.info(f"近况：原话里没提到“{name}”，不当成她说了在哪")
            return
        cur = self._load().get("place") or {}
        if not cur.get("name"):
            self._move_to(name, kind)
        elif self._same_place(name, cur["name"]):
            if kind in KINDS and not cur.get("kind"):
                cur["kind"] = kind
            # 又提到了：“前几天说的”从这天重新算。要原话里真有这个地方的字（10/23 第二次回放：只说了“明天要赶路”，
            # 模型照抄了“之前在某国”，不能算又提到）
            if (_grams(name) & _grams(said)) or (set(name) & set(said) & set(_PLACE_CHARS)):
                cur["seen"] = _day_of(_now())
        elif pl.get("moved") is True:
            self._move_to(name, kind)
        else:
            logger.info(f"近况：她说在“{name}”，可之前说在“{cur['name']}”，也没说换了地方，先不改")

    def get_place(self) -> dict:
        return dict(self._load().get("place") or {})

    # -------------------------------------------------------------- 写入（聊天调用）
    def add_said(self, text: str, *, source: str, key: str, qq: int | None = None) -> None:
        """冒泡、写信、冷场搭话发出去以后交过来（插话不交）。先挂着（取用时算“这里刚说过”），马上在后台整理：
        讲的是已有的事就只记“这里听过了”，讲了新的经历就记一条；写信还要在收信人的“她说过”里记一条"""
        text = str(text or "").strip()
        if not self.enabled or not text:
            return
        self._load()["said"].append({"text": text[:300], "source": source, "key": key, "qq": qq, "at": _now()})
        self._save()
        self.kick()

    def kick(self) -> None:
        """在后台整理挂着的原话（没有事件循环时什么都不做，等 retry 补）"""
        if self._task and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self.digest_said())
        except RuntimeError:
            pass

    def retry(self) -> None:
        """定时补做（长期记忆的定时检查里调）：上次失败过的，过了间隔再试"""
        if self._load()["said"] and _now() >= self._retry_at:
            self.kick()

    async def digest_said(self) -> int:
        """把挂着的原话一段一段整理掉；返回整理了几段。出错的留着，隔一阵再试"""
        done = 0
        async with self._lock:
            while self._load()["said"]:
                s = self._load()["said"][0]
                if not budget.can_background():
                    logger.info("近况：今天的钱花完了，主动说的话先不整理")
                    self._retry_at = _now() + SAID_RETRY_SECONDS
                    break
                try:
                    got = await self._ask(s)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"近况：整理主动说的话出错，{SAID_RETRY_SECONDS // 60} 分钟后再试：{type(e).__name__}: {e}")
                    self._retry_at = _now() + SAID_RETRY_SECONDS
                    break
                self._apply_said(s, got)
                self._load()["said"].pop(0)
                self._save()
                done += 1
        return done

    def summary_block(self, now: float | None = None) -> tuple[str, dict[int, dict]]:
        """给模型看的“她之前说自己在哪 + 最近说过的经历（#编号）”；主动说的话整理、聊天整理（memory.py）共用"""
        now = now or _now()
        place = self._load().get("place") or {}
        items = [it for it in self._items() if float(it.get("at") or 0) <= now][-10:]
        nums = {n: it for n, it in enumerate(items, 1)}
        where = (f"{place.get('name')}（" + (f"{place['kind']}，" if place.get("kind") else "")
                 + f"{_ago(place.get('since'), now)}说的）") if place.get("name") else "（还没说过）"
        lines = [f"她之前说自己在：{where}", "【最近说过的经历】"]
        lines += [f"#{n} {_when_of(it, now)} {it.get('text', '')}" for n, it in nums.items()] or ["（还没有）"]
        return "\n".join(lines), nums

    def _ask_input(self, s: dict) -> tuple[str, dict[int, dict]]:
        head, nums = self.summary_block()
        return head + f"\n【她刚{s.get('source') or '主动'}说的】\n" + str(s.get("text") or ""), nums

    def apply_from_chat(self, key: str, got, nums: dict[int, dict], her_text: str, at: float | None = None) -> None:
        """整理聊天时（memory.py 的 _summarize）模型给的 self：她在这段聊天里讲的经历、说自己在哪（10/23 C）。
        her_text 是这段里她自己说的话：认地方时程序拿它再挡一道（原话里没提到的地方不算）"""
        if not self.enabled or not isinstance(got, dict):
            return
        g = {**got, "_nums": nums, "new": _as_list_of_dict(got.get("new"))[:3]}   # 一段聊天最多收 3 件
        self._apply_said({"text": her_text, "source": "聊天", "key": key, "at": at or _now()}, g)
        self._save()

    async def _ask(self, s: dict) -> dict:
        user_msg, nums = self._ask_input(s)
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": SAID_PROMPT}, {"role": "user", "content": user_msg}],
            temperature=0, max_tokens=400, response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        budget.track(resp, "memory_self", user=None, group=None)
        d = json.loads(resp.choices[0].message.content or "")
        if not isinstance(d, dict):
            raise ValueError("返回的不是 JSON 对象")
        d["_nums"] = nums
        return d

    def _apply_said(self, s: dict, got: dict) -> None:
        nums: dict[int, dict] = got.get("_nums") or {}
        key = str(s.get("key") or "")
        told_texts = []
        before = (self._load().get("place") or {}).get("name")
        self._apply_place(got.get("place"), str(s.get("text") or ""))
        said_text = str(s.get("text") or "")
        if _PLACE_WORD_RE.search(said_text) and (self._load().get("place") or {}).get("name") == before:
            # 10/23《近况上线前检查》3.2：先只记日志，一两周后数一数“现在在哪”漏得多不多，再决定要不要认得松一点
            logger.info(f"近况：提到地点词但没换地方：{said_text.replace(chr(10), ' ')[:40]}")
        for x in got.get("same") or []:
            try:
                it = nums.get(int(x))
            except (TypeError, ValueError):
                continue
            said_text = str(s.get("text") or "")
            if it is not None and said_text and _related(_grams(said_text), it.get("text", "")) < SAME_MIN_GRAMS:
                # 10/23 第三次回放：整理聊天时模型常把毫不相干的会话也写成“讲的就是 #3”，
                # 记上以后她会以为在那里讲过（“这里已经听你讲过”），原话里得真有这件事的字才算
                logger.info(f"近况：原话里看不出讲的是“{it.get('text')}”，不算在 {key} 讲过")
                continue
            if it is not None:
                if key and key not in it.setdefault("told_in", []):
                    it["told_in"].append(key)
                told_texts.append(it.get("text", ""))
        for x in got.get("new") or []:
            if not isinstance(x, dict) or not str(x.get("text") or "").strip():
                continue
            if x.get("fits") is False:
                logger.info(f"近况：没收“{x.get('text')}”（和现在在哪对不上）")
                continue
            it = self._add(str(x["text"]), source=str(s.get("source") or "主动"), at=float(s.get("at") or _now()),
                           mood=str(x.get("mood") or ""), told_in=[key] if key else [])
            if it:
                told_texts.append(it["text"])
        if s.get("source") == "写信" and s.get("qq") and self.ltm is not None:
            self._letter_told(int(s["qq"]), told_texts)

    def _letter_told(self, qq: int, texts: list[str]) -> None:
        """写信：收信人档案的“她说过”里记一条，对方回信、聊起信里的事时她接得上（方案 3.2）"""
        body = "；".join(t for t in texts if t)
        text = _clean_text(("写信跟他讲过：" + body) if body else "给他写过一封信", 30)
        prof = self.ltm.get_user(qq)
        self.ltm._merge_told(prof, {"told": [text]}, "private")
        self.ltm.save_user(prof)

    # -------------------------------------------------------------- 取用（聊天调用）
    @staticmethod
    def _days_here(place: dict, now: float) -> int:
        try:
            return (_bj(now).date() - date.fromisoformat(str(place.get("since"))[:10])).days + 1
        except (TypeError, ValueError):
            return 1

    def _place_out(self, now: float) -> dict:
        """她说过自己在哪。status：还没说过 / 现在（最近说的）/ 前几天说的（超过 place_stale_days 天没再提，不一定还在）。
        给她看的文字由聊天 agent 按 status 写（《方案-她的近况-1023》第 2 节“会漏掉怎么办”）"""
        p = self._load().get("place") or {}
        if not p.get("name"):
            return {"status": "还没说过"}
        last = p.get("seen") or p.get("since")
        try:
            quiet = (_bj(now).date() - date.fromisoformat(str(last)[:10])).days
        except ValueError:
            quiet = 0
        return {"name": p.get("name"), "kind": p.get("kind") or "", "days_here": self._days_here(p, now),
                "said": _ago(last, now), "status": "前几天说的" if quiet > self.place_stale_days else "现在"}

    @staticmethod
    def _out(it: dict, now: float) -> dict:
        return {"text": it.get("text", ""), "when": _when_of(it, now), "mood": it.get("mood") or ""}

    def _visible(self, now: float) -> list[dict]:
        return sorted((it for it in self._items() if float(it.get("at") or 0) <= now), key=lambda it: float(it.get("at") or 0))

    @staticmethod
    def _cap_topics(items: list[dict], talked: set[str], other_max: int = 1) -> list[dict]:
        """面包、钱、蘑菇：对方没提的，合起来最多 other_max 条；提了的每样最多 2 条"""
        out, other, used = [], 0, {}
        for it in items:
            tops = _her_topics(it.get("text", ""))
            if tops - talked:
                if other >= other_max:
                    continue
                other += 1
            if any(used.get(t, 0) >= 2 for t in tops & talked):
                continue
            for t in tops & talked:
                used[t] = used.get(t, 0) + 1
            out.append(it)
        return out

    def life_for_chat(self, key: str, text: str, recent: str = "", now: float | None = None) -> dict:
        """每轮回消息：她说过自己在哪（带状态）+ 今天说过的 + 按这句话（和最近几句）挑的一两条，合起来 100 字左右"""
        if not self.enabled:
            return {}
        now = now or _now()
        vis = self._visible(now)
        today_day = _day_of(now)
        today = [it for it in vis if it.get("date") == today_day]
        q = _grams(f"{text} {recent}")
        scored = [(_related(q, it.get("text", "")), float(it.get("at") or 0), it) for it in vis if it not in today]
        related = [it for s, _, it in sorted(scored, key=lambda x: (-x[0], -x[1])) if s > 0][:2]
        talked = _her_topics(f"{text} {recent}")
        kept = self._cap_topics(today + related, talked, other_max=0)   # 每轮回消息：对方没提就一条都不带（近况测试 J08：带 1 条她就会提面包）
        today = [it for it in today if it in kept]
        related = [it for it in related if it in kept]

        def size() -> int:
            return sum(len(it.get("text", "")) + 6 for it in today + related)
        while related and size() > CHAT_CHARS:
            related.pop()
        while len(today) > 1 and size() > CHAT_CHARS:
            today.pop(0)                          # 先丢今天早些的
        place = self._place_out(now)
        return {"place": place, "today": [self._out(it, now) for it in today],
                "related": [self._out(it, now) for it in related]}

    def life_for_proactive(self, key: str, n: int = 3, now: float | None = None) -> dict:
        """冒泡、写信、搭话：最近几条这个会话还没听过的（今天的排前面）+ 这里已经听过的（别再讲）"""
        if not self.enabled:
            return {}
        now = now or _now()
        vis = self._visible(now)
        today_day = _day_of(now)
        fresh = [it for it in vis if key not in (it.get("told_in") or [])]
        fresh.sort(key=lambda it: (it.get("date") == today_day, float(it.get("at") or 0)), reverse=True)
        fresh = self._cap_topics(fresh, set())[:n]
        told = [it for it in vis if key in (it.get("told_in") or [])][-TOLD_HERE_MAX:]
        told_out = [self._out(it, now) for it in told]
        for s in self._load()["said"]:            # 刚说过、还没整理完的：原话先算“这里听过了”
            if s.get("key") == key:
                told_out.append({"text": _clean_text(s.get("text"), 40), "when": "刚才", "mood": ""})
        place = self._place_out(now)
        return {"place": place, "fresh": [self._out(it, now) for it in fresh], "told_here": told_out[-TOLD_HERE_MAX:]}

    # -------------------------------------------------------------- 管理员：/近况
    def _listed(self) -> list[dict]:
        return sorted(self._items(), key=lambda it: float(it.get("at") or 0))

    def describe(self) -> str:
        now = _now()
        p = self._place_out(now)
        lines = [f"她说现在在：{p['name']}（" + (f"{p['kind']}，" if p.get("kind") else "") + f"第 {p['days_here']} 天，{p['said']}说的"
                 + ("，之后没再提，给她看的是“不一定还在”" if p["status"] == "前几天说的" else "") + "）"
                 if p.get("name") else "她说现在在：（还没说过）"]
        d = self._load()
        if d["trail"]:
            lines.append("之前：" + " → ".join(str(t.get("name")) for t in d["trail"][-5:]))
        items = self._listed()
        if not items:
            lines.append("近况：（还没有）")
        for n, it in enumerate(items, 1):
            at = float(it.get("at") or 0)
            when = f"{_bj(at):%m-%d %H:%M}" + ("（还没到）" if at > now else "")
            told = len(it.get("told_in") or [])
            lines.append(f"{n}. [{it.get('source')}｜{when}｜{it.get('where') or '?'}"
                         + (f"｜{it['mood']}" if it.get("mood") else "") + (f"｜讲过 {told} 处" if told else "")
                         + f"] {it.get('text', '')}")
        if d["said"]:
            lines.append(f"（还有 {len(d['said'])} 段主动说的话没整理完）")
        return "\n".join(lines)

    def delete(self, n: int) -> str | None:
        items = self._listed()
        if not 1 <= n <= len(items):
            return None
        it = items[n - 1]
        self._load()["items"] = [x for x in self._items() if x is not it]
        self._save()
        return it.get("text", "")

    def set_place_by_admin(self, name: str) -> dict:
        """管理员改“她说现在在哪”（/近况 在 ……）：她说错了、记错了时用"""
        self._move_to(name)
        self._save()
        return self.get_place()

    def edit(self, n: int, text: str) -> tuple[str, str] | None:
        items = self._listed()
        new = _clean_text(text, 60)
        if not new or not 1 <= n <= len(items):
            return None
        it = items[n - 1]
        old = it.get("text", "")
        it["text"] = new
        self._save()
        return old, new
