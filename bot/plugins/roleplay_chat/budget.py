"""按钱限额（9/29）：每次调用模型都按 DeepSeek 返回的用量记账，一天花到上限就不再说话。

- 用量分三种算钱：输入命中缓存、输入没命中缓存、输出；DeepSeek 高峰时段（工作日 9-12、14-18，节假日不算）按两倍价。
- “一天”从北京时间凌晨 reset_hour 点算起（默认 5 点：0～3 点常有人聊天，按 0 点算的话她 23:50 刚告别，0 点又回来了）。
- 聊天类（回消息、判断是不是在跟她说话、看图、插话、冒泡、写信、搭话、回空间评论）：今天**所有调用合计**（聊天 + 后台）
  花到 total - reserve 就停；后台类（长期记忆整理、写说说、给图库和表情写描述）可以用到 total。
  这样聊天停下时，最后 reserve 这笔一定还在，只给记忆整理、20:00 日结和写说说用（9/30 改：以前只算聊天自己花的，
  后台白天花超了预留，聊天就会一直说到 total 花光，最后几段的记忆和当晚的说说都没钱）。
- 每个人、每个群各自最多占多少（防止一个人刷屏把一天的钱花光）。
- 账记在 data/usage/日期.json，重启不清零。
"""
from __future__ import annotations

import contextvars
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import logger

from . import peak

# 聊天类：受 chat_limit 限制；其余（memory、qzone_post、gallery、sticker……）是后台类，可以用到 total
CHAT_KINDS = {"chat", "judge", "verify", "vision", "interject", "bubble", "letter", "nudge", "qzone_reply", "qzone_friend"}
KIND_NAMES = {
    "chat": "回消息", "judge": "判断是不是在跟她说话", "verify": "核对讲的往事", "vision": "看图", "interject": "插话", "bubble": "冒泡",
    "letter": "写信", "nudge": "冷场搭话", "qzone_reply": "回空间评论", "qzone_friend": "评论好友说说",
    "memory": "长期记忆整理", "qzone_post": "写说说", "gallery": "给说说配图写描述", "sticker": "给表情写描述",
}

# 这次调用是替谁花的（在处理某个人的消息时设置；看图、判断这些都算到这个人 / 这个群头上）
actor: contextvars.ContextVar[tuple[int | None, int | None]] = contextvars.ContextVar("budget_actor", default=(None, None))


def _usage_numbers(usage) -> tuple[int, int, int] | None:
    """（命中缓存的输入, 没命中的输入, 输出）；拿不到用量返回 None"""
    if usage is None:
        return None
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    out = get("completion_tokens")
    hit, miss = get("prompt_cache_hit_tokens"), get("prompt_cache_miss_tokens")
    if hit is None and miss is None:                 # 不是 DeepSeek 的写法：按 OpenAI 的 cached_tokens 算
        prompt = get("prompt_tokens")
        if prompt is None and out is None:
            return None
        details = get("prompt_tokens_details")
        cached = (details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", None)) or 0
        hit, miss = cached, max(0, int(prompt or 0) - int(cached))
    try:
        return int(hit or 0), int(miss or 0), int(out or 0)
    except (TypeError, ValueError):
        return None


class Budget:
    def __init__(self, root: Path, *, enabled: bool = True, total: float = 1.5, reserve: float = 0.15,
                 user_share: float = 0.375, group_share: float = 0.75, reset_hour: int = 5,
                 price: dict | None = None, peak_multiplier: float = 2.0,
                 peak_ranges: str = "09:00-12:00,14:00-18:00", holidays=()):
        self.root = Path(root)
        self.enabled = enabled
        self.total = float(total)
        self.reserve = float(reserve)
        self.user_share = float(user_share)
        self.group_share = float(group_share)
        self.reset_hour = int(reset_hour)
        self.price = {"hit": 0.02, "miss": 1.0, "out": 4.0, **(price or {})}     # 元 / 百万 token（空闲价）
        self.peak_multiplier = float(peak_multiplier)
        self.peak_ranges = peak_ranges
        self.holidays = list(holidays)
        self._day: str | None = None
        self._data: dict = {}

    # ------------------------------------------------------------------ 日期
    def day_key(self, now: datetime | None = None) -> str:
        now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
        return (now - timedelta(hours=self.reset_hour)).strftime("%Y-%m-%d")

    def next_reset(self, now: datetime | None = None) -> datetime:
        now = (now or datetime.now(peak.BEIJING)).astimezone(peak.BEIJING)
        r = now.replace(hour=self.reset_hour, minute=0, second=0, microsecond=0)
        return r if r > now else r + timedelta(days=1)

    def _path(self, day: str) -> Path:
        return self.root / f"{day}.json"

    @staticmethod
    def _empty() -> dict:
        return {"total": 0.0, "chat": 0.0, "kinds": {}, "users": {}, "groups": {}, "unknown_calls": 0}

    def _load_day(self, day: str) -> dict:
        try:
            d = json.loads(self._path(day).read_text(encoding="utf-8"))
            return {**self._empty(), **d} if isinstance(d, dict) else self._empty()
        except (OSError, ValueError):
            return self._empty()

    def today(self) -> dict:
        day = self.day_key()
        if day != self._day:
            self._day, self._data = day, self._load_day(day)
        return self._data

    def _save(self) -> None:
        path = self._path(self._day)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"花费：记账写文件失败（这次只记在内存里）：{e}")

    def reset(self) -> None:
        """测试用：清掉内存里的账"""
        self._day, self._data = None, {}

    # ------------------------------------------------------------------ 记账
    def cost_of(self, hit: int, miss: int, out: int, now: datetime | None = None) -> float:
        k = self.peak_multiplier if peak.is_peak(self.peak_ranges, self.holidays, now) else 1.0
        p = self.price
        return (hit * p["hit"] + miss * p["miss"] + out * p["out"]) * k / 1e6

    def track(self, resp, kind: str, user: int | None = None, group: int | None = None) -> float:
        """记一次调用；返回这次花了多少元。拿不到用量（比如接口没返回）只记次数"""
        d = self.today()
        if user is None and group is None:
            user, group = actor.get()
        nums = _usage_numbers(getattr(resp, "usage", None) if resp is not None else None)
        k = d["kinds"].setdefault(kind, {"cost": 0.0, "calls": 0, "hit": 0, "miss": 0, "out": 0})
        k["calls"] += 1
        if nums is None:
            d["unknown_calls"] = int(d.get("unknown_calls", 0)) + 1
            self._save()
            return 0.0
        hit, miss, out = nums
        cost = self.cost_of(hit, miss, out)
        k["cost"] += cost
        k["hit"] += hit
        k["miss"] += miss
        k["out"] += out
        d["total"] += cost
        if kind in CHAT_KINDS:
            d["chat"] += cost
            if user is not None and not group:
                d["users"][str(user)] = d["users"].get(str(user), 0.0) + cost
            if group:
                d["groups"][str(group)] = d["groups"].get(str(group), 0.0) + cost
                if user is not None:          # 群里的也算到这个人头上（每人上限是群聊私聊合计）
                    d["users"][str(user)] = d["users"].get(str(user), 0.0) + cost
        self._save()
        return cost

    # ------------------------------------------------------------------ 还剩多少
    @property
    def chat_limit(self) -> float:
        return max(0.0, self.total - self.reserve)

    def chat_left(self) -> float:
        """聊天还能花多少：今天所有调用合计（聊天 + 后台）花到 total - reserve 就停，最后 reserve 留给后台"""
        if not self.enabled:
            return float("inf")
        return self.chat_limit - self.today()["total"]

    def total_left(self) -> float:
        if not self.enabled:
            return float("inf")
        return self.total - self.today()["total"]

    def user_left(self, qq: int | None) -> float:
        if not self.enabled or qq is None or self.user_share <= 0:
            return float("inf")
        return self.user_share - self.today()["users"].get(str(qq), 0.0)

    def group_left(self, gid: int | None) -> float:
        if not self.enabled or not gid or self.group_share <= 0:
            return float("inf")
        return self.group_share - self.today()["groups"].get(str(gid), 0.0)

    def can_background(self) -> bool:
        return self.total_left() > 0

    # ------------------------------------------------------------------ 给管理员看
    def month_total(self) -> float:
        day = self.day_key()
        prefix = day[:7]
        s = 0.0
        if self.root.exists():
            for f in self.root.glob(f"{prefix}-*.json"):
                if f.stem == day:
                    continue
                s += float(self._load_day(f.stem).get("total", 0.0))
        return s + self.today()["total"]

    def report(self, names: dict | None = None) -> str:
        d = self.today()
        names = names or {}
        lines = [f"今天（{self.day_key()} {self.reset_hour}:00 起）花了 {d['total']:.3f} 元"
                 + (f" / 上限 {self.total:g} 元（合计到 {self.chat_limit:g} 元就不再聊天，最后 {self.reserve:g} 元只给记忆整理和写说说）"
                    if self.enabled else "（没开限额）")]
        lines.append(f"聊天类 {d['chat']:.3f} 元，后台类 {d['total'] - d['chat']:.3f} 元")
        for kind, k in sorted(d["kinds"].items(), key=lambda x: -x[1]["cost"]):
            lines.append(f"· {KIND_NAMES.get(kind, kind)}：{k['cost']:.3f} 元，{k['calls']} 次"
                         f"（输入 {k['hit'] + k['miss']} token，命中缓存 {k['hit']}；输出 {k['out']}）")
        if d.get("unknown_calls"):
            lines.append(f"· 有 {d['unknown_calls']} 次调用接口没返回用量，没算钱")
        top_u = sorted(d["users"].items(), key=lambda x: -x[1])[:5]
        if top_u:
            lines.append("花得最多的人：" + "、".join(f"{names.get(int(q), q)} {c:.3f}" for q, c in top_u)
                         + (f"（每人上限 {self.user_share:g}）" if self.enabled and self.user_share > 0 else ""))
        top_g = sorted(d["groups"].items(), key=lambda x: -x[1])[:5]
        if top_g:
            lines.append("花得最多的群：" + "、".join(f"{g} {c:.3f}" for g, c in top_g)
                         + (f"（每群上限 {self.group_share:g}）" if self.enabled and self.group_share > 0 else ""))
        lines.append(f"本月合计 {self.month_total():.2f} 元（以 DeepSeek 后台的账单为准）")
        return "\n".join(lines)


_budget: Budget | None = None


def setup(b: Budget) -> Budget:
    global _budget
    _budget = b
    return b


def get() -> Budget | None:
    return _budget


def track(resp, kind: str, user: int | None = None, group: int | None = None) -> float:
    """各处调完模型都调这个；没初始化（单独测某个模块时）就什么都不做"""
    if _budget is None:
        return 0.0
    try:
        return _budget.track(resp, kind, user, group)
    except Exception as e:  # noqa: BLE001   记账出错不能影响回复
        logger.warning(f"花费：记账出错：{e}")
        return 0.0


def can_background() -> bool:
    return _budget is None or _budget.can_background()
