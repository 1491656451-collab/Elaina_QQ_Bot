"""好感度（10/22 从 memory.py 拆出来，代码原样搬过来，没改）

分数、档位、回落、每天上限、纠缠扣分、性别证据和“很熟”的性别上限、送面包、雷点扣分、管理员 /好感 的显示。
做成 LongTermMemory 继承的一个类：ltm.adjust()、ltm.familiarity()、ltm.effective_score() 这些调用写法不变。
档案的读写（get_user / save_user）还在 memory.py；整理记忆时按对话内容加减分的那段也还在 memory.py 的 _summarize 里。
"""
import time
from datetime import datetime

from nonebot import logger


# 这两个小工具在 memory.py 里定义、整理记忆也在用；这里转一下，免得两个文件在加载时互相导入
def _clean_text(*args, **kwargs):
    from .memory import _clean_text as f
    return f(*args, **kwargs)


def _today_str():
    from .memory import _today_str as f
    return f()


class AffectionMixin:
    # -------------------------------------------------------------- 好感度
    # 分数 -50～150（讨厌 <0｜陌生人 0～39｜普通朋友 40～89｜熟人 90～129｜很熟 130+）。来源：① 长期记忆整理时按对话内容加减分；
    # ② 送面包、踩雷；③ 管理员手动调整（光聊天不加分：9/30 删掉了 AFFECTION_CHAT_GAIN）。新人从 20 起步；
    # 很久不聊会慢慢向 20 回落，但回落不改变档位（9/30 起）：很熟最多落到 130，讨厌的人最多回升到 -1。
    close_friends: tuple = ()

    gender_cap: bool = True                  # 只有确认是女生（或有证据地猜了两次都是女生）才能到“很熟”
    affection_cfg = {
        "decay_after_days": 7, "decay_per_day": 2,
        "min": -50, "max": 150,
        "start": 20, "dislike": 0, "friend": 40, "acquaintance": 90, "close": 130,
        "summary_daily_cap": 10, "nag_daily_cap": 10,
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

    def _nag_capped(self, prof: dict, qq: int, delta: int, kind: str) -> int:
        """整理时的扣分：“纠缠”（烦人但没恶意）每人每天合计最多扣 nag_daily_cap 分；“恶意”不限。
        没标类别的：-6 及以下按恶意、其余按纠缠；标了“正常”却扣分的，按纠缠算"""
        if kind != "恶意" and not (kind not in ("正常", "纠缠") and delta <= -6):
            if delta < -5:                        # 规则写的纠缠是 -1～-5；pro 常给 -8（10/16 对比），按 -5 算
                logger.info(f"长期记忆：{qq} 纠缠扣 {delta} 超出了 -5，按 -5 算")
                delta = -5
            cap = float(self.affection_cfg.get("nag_daily_cap", 0) or 0)
            if cap:
                today = self._today()
                if prof.get("nag_day") != today:
                    prof["nag_day"], prof["nag_today"] = today, 0
                room = max(0.0, cap - float(prof.get("nag_today", 0)))
                if -delta > room:
                    logger.info(f"长期记忆：{qq} 今天因为纠缠已经扣了 {prof.get('nag_today', 0):g} 分，这次 {delta} 只算 -{room:g}")
                    delta = -int(room)
                prof["nag_today"] = float(prof.get("nag_today", 0)) - delta
        return delta

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _tier_bounds(self, score: float) -> tuple[float, float]:
        """回落时分数能到的范围：不跨出现在的档位（9/30 用户定：回落不改变熟悉程度，其他加减分照常）。
        高于起步分的只往下落到本档下限（很熟 130、熟人 90、普通朋友 40、陌生人落到起步分）；
        低于起步分的只往上回到本档上限（陌生人回到起步分，讨厌的人最多回到讨厌线下 1 分）"""
        c = self.affection_cfg
        start = float(c.get("start", 0))
        lines = sorted(float(c[k]) for k in ("dislike", "friend", "acquaintance", "close") if k in c)
        floor = max([start] + [b for b in lines if b <= score])
        above = [b for b in lines if b > score]
        ceil = min([start] + ([above[0] - 1] if above else []))
        return floor, ceil

    def effective_score(self, prof: dict) -> float:
        """计算衰减后的好感：超过 decay_after_days 天没说话，每天向新人的起始分靠拢 decay_per_day，
        但不跨出现在的档位（见 _tier_bounds）"""
        c = self.affection_cfg
        if "score" not in prof:                 # 旧版数据：按说话次数给个初始值
            prof["score"] = self.migrate_score(min(int(prof.get("talks", 0)), 25))
        score = float(prof["score"])
        if self.gender_cap and not self.counts_as_female(prof):
            score = min(score, c["close"] - 1)   # 先按性别封顶，再算回落：男生存的分再高，也按熟人那档算下限
        last = prof.get("last_talk")
        if last:
            # 从“最后说话 + 宽限期”开始回落；已经结算进 score 的那段（decay_settled 之前）不再重复算（9/30 修：
            # 以前别的加减分把回落后的分写回去以后，下次又从 last_talk 起整段再落一遍）
            begin = max(float(last) + c["decay_after_days"] * 86400, float(prof.get("decay_settled") or 0))
            idle_days = (time.time() - begin) / 86400
            if idle_days > 0:
                dec = idle_days * c["decay_per_day"]
                start = float(c.get("start", 0))
                floor, ceil = self._tier_bounds(score)
                if score > start:
                    score = max(min(floor, score), score - dec)
                elif score < start:
                    score = min(max(ceil, score), score + dec)
        if self.gender_cap and not self.counts_as_female(prof):
            score = min(score, c["close"] - 1)   # 男生、看不出性别的人：最高到熟人
        score = max(float(c.get("min", -50)), min(float(c.get("max", 150)), score))
        return round(score, 1)

    def _apply_affection(self, prof: dict, delta: float, reason: str = "") -> None:
        c = self.affection_cfg
        prof["score"] = max(float(c.get("min", -50)), min(float(c.get("max", 150)), self.effective_score(prof) + delta))
        prof["decay_settled"] = time.time()      # 到现在为止的回落已经算进 score 了
        prof["last_talk"] = prof.get("last_talk") or time.time()
        log = prof.setdefault("affection_log", [])
        log.append(f"{self._today()} {'+' if delta > 0 else ''}{delta:g} {reason}".strip())
        del log[:-8]

    def bump_talk(self, qq: int, name: str = "") -> int:
        """记一次正常聊天：计数 +1，记下时间（光聊天不加好感；不受 memory_enabled 影响）"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)      # 先把衰减结算掉（旧数据也在这里按次数换算初始分）
        prof["talks"] = int(prof.get("talks", 0)) + 1
        if name:
            prof["name"] = name
        prof.pop("gain_day", None); prof.pop("gain_today", None)    # 9/30 删掉“光聊天加分”后留下的旧字段
        prof["score"] = score
        prof["last_talk"] = prof["last_msg"] = prof["decay_settled"] = time.time()
        self.save_user(prof)
        return prof["talks"]

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
        """管理员设定性别（None = 清除）：最权威，之后的证据都不改它。好感封顶会跟着变"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)          # 先按旧性别结算一次
        if gender:
            prof["gender"], prof["gender_src"] = gender, "admin"
            prof.pop("gender_doubt", None)
            prof.pop("gender_lied", None)
        else:
            prof.pop("gender", None)
            prof.pop("gender_src", None)
        prof.pop("gender_pending", None)
        prof["score"] = score
        prof["decay_settled"] = time.time()          # 回落已经结算进 score
        self.save_user(prof)

    GENDER_GUESS_V = 2        # 9/30 改了猜性别的规则：旧的猜测（没证据也猜、多半猜成男生）读到时清掉

    GENDER_KINDS = ("自称", "别人称呼", "昵称", "拆穿", "承认骗人")

    def gender_evidence(self, prof: dict, g: str, kind: str, ev: str = "", day: str | None = None) -> str | None:
        """记一条性别证据，重新判断。返回“发现被骗了”的一句记录（没有就返回 None）。
        - 管理员 /性别 设的最权威，证据不改它；
        - 只有“自称”的时候最多算半信半疑：要有别的来源印证（别人称呼、昵称），或者不同的日子自称过两次，才算“猜得稳”；
        - 出现相反的证据就退回看不出（带着怀疑）；相反的证据也攒够了，就改判——以前确认过的（聊天里质疑后又确认的）也能推翻。"""
        if g not in ("male", "female"):
            return None
        kind = kind if kind in self.GENDER_KINDS else "自称"
        log = [e for e in prof.get("gender_log") or [] if isinstance(e, dict)]
        log.append({"g": g, "kind": kind, "ev": _clean_text(ev, 30), "day": day or _today_str()})
        prof["gender_log"] = log[-6:]
        return self._judge_gender(prof)

    def _judge_gender(self, prof: dict) -> str | None:
        if prof.get("gender_src") == "admin":
            return None
        log = prof.get("gender_log") or []
        if not log:
            return None
        before = prof.get("gender") or prof.get("gender_guess") or (prof.get("gender_doubt") or {}).get("before")
        last = log[-1]["g"]
        run = []                                   # 最近一段说法一致的证据
        for e in reversed(log):
            if e.get("g") != last:
                break
            run.append(e)
        conflict = len(run) < len(log)
        kinds = {e.get("kind") for e in run}
        self_days = {e.get("day") for e in run if e.get("kind") == "自称"}
        stable = "承认骗人" in kinds or (len(run) >= 2 and (kinds - {"自称"} or len(self_days) >= 2))
        note = None
        if stable:
            prof["gender_guess"] = last
            prof.pop("gender_doubt", None)
            if prof.get("gender") and prof["gender"] != last:
                prof.pop("gender", None)           # 以前“确认”过的被推翻了
                prof.pop("gender_src", None)
            if before and before != last and any(e.get("g") == before and e.get("kind") == "自称" for e in log):
                note = f"说过自己是{'女生' if before == 'female' else '男生'}，后来发现多半是骗伊蕾娜的"
                prof["gender_lied"] = before
        else:
            if prof.get("gender_guess") != last:
                prof.pop("gender_guess", None)
            opposite = next((e for e in reversed(log) if e.get("g") != last), None)
            if conflict or (prof.get("gender") and prof["gender"] != last):
                prof["gender_doubt"] = {"now": last, "ev": run[0].get("ev", ""), "before": (opposite or {}).get("g") or prof.get("gender"),
                                        "before_kind": (opposite or {}).get("kind") or ("自称" if prof.get("gender") else "")}
            else:
                prof.pop("gender_doubt", None)
        return note

    def claim_gender(self, qq: int, gender: str) -> None:
        """聊天里对方自称性别、被她质疑后又确认了一次：只算一次“自称”，不再直接定死（9/30 起）"""
        prof = self.get_user(qq)
        score = self.effective_score(prof)
        prof.pop("gender_pending", None)
        self.gender_evidence(prof, gender, "自称", "被质疑后又确认了一次")
        prof["score"] = score
        prof["decay_settled"] = time.time()
        self.save_user(prof)

    @staticmethod
    def last_claim(prof: dict) -> str | None:
        """对方最近一次自称的性别"""
        return next((e.get("g") for e in reversed(prof.get("gender_log") or []) if e.get("kind") == "自称"), None)

    def counts_as_female(self, prof: dict) -> bool:
        """能不能到“很熟”：确认是女生（管理员设的，或者 9/30 以前聊天里确认的，没被推翻）；
        或者有证据地判断是女生，而且不只是她自己说的（9/30 用户定的 B 方案，加上“对方可能骗人”的怀疑）"""
        if prof.get("gender"):
            return prof["gender"] == "female"
        return prof.get("gender_guess") == "female"

    def gender_hint(self, prof: dict, who) -> str:
        """给她看的一句性别提示：确定的、猜得稳的、半信半疑的、有点怀疑被骗的"""
        w = lambda g: "女生" if g == "female" else "男生"   # noqa: E731
        g = prof.get("gender")
        doubt = prof.get("gender_doubt") or {}
        if g and prof.get("gender_src") == "admin":
            return f"「{who}」是{w(g)}。"
        if doubt:
            said = w(doubt.get("before")) if doubt.get("before") else "另一种说法"
            how = {"自称": f"说过自己是{said}", "别人称呼": f"被人当成{said}", "昵称": f"昵称看着像{said}"}.get(
                doubt.get("before_kind", ""), f"看起来像{said}")
            ev = f"（{doubt['ev']}）" if doubt.get("ev") else ""
            return (f"「{who}」以前{how}，但现在有对不上的地方{ev}。你有点怀疑对方在骗你："
                    "心里有数就行，想挖苦可以轻轻点一句，别追着问。")
        if g:
            return f"「{who}」说过自己是{w(g)}，你姑且信了。"
        if prof.get("gender_guess"):
            if self._lied(prof):
                return (f"「{who}」以前骗过你说自己是{w(self._lied(prof))}，后来被你识破了，你觉得应该是{w(prof['gender_guess'])}。"
                        "这笔账你记着：聊到相关的，可以嘴硬地挖苦一句，但别翻来覆去说，也别真生气。")
            return f"你觉得「{who}」应该是{w(prof['gender_guess'])}：别说破，也别问。"
        claim = self.last_claim(prof)
        if claim:
            return f"「{who}」自称是{w(claim)}，但没有别的证据，你半信半疑：别当真，也别追问。"
        return ""

    @staticmethod
    def _lied(prof: dict) -> str | None:
        """骗过她的性别（被识破、现在猜的是另一种）；没有就 None"""
        lied, guess = prof.get("gender_lied"), prof.get("gender_guess")
        if not lied and guess and any("多半是骗" in str(f.get("text", "")) for f in prof.get("facts") or [] if isinstance(f, dict)):
            lied = "female" if guess == "male" else "male"      # 旧档案里只有那条记忆，没存标记
        return lied if lied and guess and lied != guess else None

    def gender_text(self, prof: dict) -> str:
        g = prof.get("gender")
        if g:
            return f"{self.GENDER_NAMES[g]}（{'管理员设定' if prof.get('gender_src') == 'admin' else '本人确认'}）"
        guess = prof.get("gender_guess")
        if guess:
            return f"未确认（有证据地判断是{self.GENDER_NAMES[guess]}生）"
        if prof.get("gender_doubt"):
            return "未确认（说法对不上，有点怀疑在骗人）"
        claim = self.last_claim(prof)
        return f"未确认（自称{self.GENDER_NAMES[claim]}生，半信半疑）" if claim else "未确认"

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
