"""记混检查（9/29）：她讲旅途往事时，常把两段相似的经历拼在一起（比如把第17卷“梦回之城卡尔赛尔”和
第24卷“宁梦璃的梦境国度”混成“在梦回之城遇上艾姆妮西亚”）。模型自己很难发现，一旦说出口还会记进聊天记录，
后面越说越错。

这里不调用模型，只查“人”和“地方”对不对得上：
- 每个地方出现在哪几卷：看章节摘要的“地点”“经过”、卷概要，以及人设“旅途回忆总览”里那一卷的那一行；
- 每个角色出现在哪几卷：看章节摘要的“登场”“本卷重要角色”、角色档案里引用的“第N卷”，以及总览那一行；
- 回复里同一句话同时提到某个地方和某个角色，而两者没有一卷是重合的，就算记混了。

地名只认 knowledge/places.md 里列的专名（“某国”“森林”这种不算）。句子里有“不是”“要是”“想起”这类
否定、假设、联想的说法时不查，免得误伤“那个不是在卡尔赛尔”这种正确的话。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nonebot import logger

from .knowledge import character_names

_VOL_FILE = re.compile(r"vol(\d+)\.md$")
_CITE = re.compile(r"第\s*(\d+)\s*卷")
_OVERVIEW = re.compile(r"^\s*(\d+)\.\s*(.+)$")
_SENT = re.compile(r"[^。！？!?\n]+[。！？!?]*")
# 这些说法多半不是在陈述“某人在某地”：否定、假设、联想、比较
_SOFT = re.compile(r"(不是|没有|没在|没去|没遇|并不|而是|要是|如果|假如|万一|想起|想到|联想|让我想|像是|好像|就像|比起|不如|听说|据说)")
_SKIP_CHARS = {"伊蕾娜本人", "扫帚（人形）"}     # 她自己、扫帚：哪一卷都在


@dataclass
class Mismatch:
    place: str
    person: str
    place_vols: list[int]
    person_vols: list[int]
    sentence: str


class FactChecker:
    def __init__(self, summary_dir: Path, characters_file: Path | None, places_file: Path | None,
                 persona_text: str = ""):
        self.place_alias: dict[str, str] = {}          # 别名 -> 正式名
        self.place_vols: dict[str, set[int]] = {}
        self.person_alias: dict[str, str] = {}
        self.person_vols: dict[str, set[int]] = {}
        self.person_timeline: dict[str, str] = {}      # 正式名 -> 档案里“与伊蕾娜”那段（给纠正提示用）
        self.overview: dict[int, str] = {}              # 卷 -> 人设总览那一行
        self._load(summary_dir, characters_file, places_file, persona_text)

    # ------------------------------------------------------------------ 建表
    def _load(self, summary_dir: Path, characters_file: Path | None, places_file: Path | None, persona_text: str) -> None:
        in_overview = False
        for line in persona_text.splitlines():
            if line.startswith("## "):
                in_overview = "旅途回忆" in line
                continue
            m = _OVERVIEW.match(line) if in_overview else None
            if m:
                self.overview[int(m.group(1))] = m.group(2).strip()

        if places_file and places_file.exists():
            for line in places_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                names = [x.strip() for x in line.split("|") if len(x.strip()) >= 2]
                if names:
                    for n in names:
                        self.place_alias[n] = names[0]
                    self.place_vols.setdefault(names[0], set())

        if characters_file and characters_file.exists():
            s = characters_file.read_text(encoding="utf-8")
            for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", s, re.S | re.M):
                title, body = m.group(1).strip(), m.group(2)
                if title in _SKIP_CHARS:
                    continue
                names = character_names(f"角色资料·{title}")
                if not names:
                    continue
                canon = names[0]
                for n in names:
                    self.person_alias[n] = canon
                vols = self.person_vols.setdefault(canon, set())
                vols.update(int(v) for v in _CITE.findall(body))
                t = re.search(r"^- 与伊蕾娜：(.*?)(?=^- [^ ]|\Z)", body, re.S | re.M)
                if t:
                    self.person_timeline[canon] = re.sub(r"\s+", " ", t.group(1)).strip()

        for f in sorted(Path(summary_dir).glob("vol*.md")) if summary_dir and Path(summary_dir).exists() else []:
            m = _VOL_FILE.search(f.name)
            if not m:
                continue
            vol = int(m.group(1))
            text = f.read_text(encoding="utf-8")
            where, who = [], []
            section = ""
            for line in text.splitlines():
                if line.startswith("## "):
                    section = line[3:].strip()
                    continue
                if section == "卷概要" and line.strip():
                    where.append(line)
                elif line.startswith("- 地点：") or line.startswith("- 经过："):
                    where.append(line)
                elif line.startswith("- 登场："):
                    who.append(line)
                elif section.startswith("本卷重要角色") and line.startswith("- "):
                    who.append(line.split("：", 1)[0])
            ov = self.overview.get(vol, "")
            place_text = "\n".join(where) + "\n" + ov
            person_text = "\n".join(who) + "\n" + ov
            for alias, canon in self.place_alias.items():
                if alias in place_text:
                    self.place_vols[canon].add(vol)
            for alias, canon in self.person_alias.items():
                if alias in person_text:
                    self.person_vols[canon].add(vol)
        # 查不到在哪一卷的地方 / 人，不参与检查
        self.place_vols = {k: v for k, v in self.place_vols.items() if v}
        self.person_vols = {k: v for k, v in self.person_vols.items() if v}
        self._place_re = self._alt(a for a, c in self.place_alias.items() if c in self.place_vols)
        self._person_re = self._alt(a for a, c in self.person_alias.items() if c in self.person_vols)
        logger.info(f"记混检查：{len(self.place_vols)} 个地方、{len(self.person_vols)} 个角色")

    @staticmethod
    def _alt(words) -> re.Pattern | None:
        words = sorted(set(words), key=len, reverse=True)       # 长的先配，“梦回之城卡尔赛尔”不会被拆成两个
        return re.compile("|".join(map(re.escape, words))) if words else None

    # ------------------------------------------------------------------ 检查
    def mentions(self, text: str) -> tuple[list[str], list[str]]:
        """这段话里提到的地方、角色（正式名，按出现先后去重）"""
        places = list(dict.fromkeys(self.place_alias[m.group(0)] for m in self._place_re.finditer(text or ""))) if self._place_re else []
        people = list(dict.fromkeys(self.person_alias[m.group(0)] for m in self._person_re.finditer(text or ""))) if self._person_re else []
        return places, people

    def overview_for(self, text: str, limit: int = 3) -> list[str]:
        """这段话里提到的地方所在的那几段经历（人设总览里的原话），给核对故事用"""
        places, _ = self.mentions(text)
        vols = []
        for p in places:
            for v in sorted(self.place_vols.get(p, ())):
                if v in self.overview and v not in vols:
                    vols.append(v)
        return [f"第{v}段：{self.overview[v]}" for v in vols[:limit]]

    def check(self, text: str) -> list[Mismatch]:
        if not text or self._place_re is None or self._person_re is None:
            return []
        out, seen = [], set()
        for sent in _SENT.findall(text):
            if _SOFT.search(sent):
                continue
            places = {self.place_alias[m.group(0)] for m in self._place_re.finditer(sent)}
            people = {self.person_alias[m.group(0)] for m in self._person_re.finditer(sent)}
            for p in places:
                for c in people:
                    pv, cv = self.place_vols[p], self.person_vols[c]
                    if pv & cv or (p, c) in seen:
                        continue
                    seen.add((p, c))
                    out.append(Mismatch(p, c, sorted(pv), sorted(cv), sent.strip()))
        return out

    def _timeline(self, person: str, limit: int = 320) -> str:
        """档案里“与伊蕾娜”那段：开头一句 + 从最近往前尽量多放几条（最近的重逢最常被问到）"""
        tl = self.person_timeline.get(person, "")
        if not tl:
            return ""
        parts = [x.strip() for x in tl.split(" - ") if x.strip()]
        head, items = parts[0], parts[1:]
        keep = []
        for it in reversed(items):
            if len(head) + sum(len(x) + 1 for x in keep) + len(it) > limit:
                break
            keep.insert(0, it)
        return head + ("；" + "；".join(keep) if keep else "")

    def correction(self, found: list[Mismatch]) -> str:
        """给模型的纠正提示：哪里记混了、那个地方是哪段经历、那个人其实在哪几段里"""
        lines = []
        for m in found[:2]:
            pv = "、".join(f"第{v}段" for v in m.place_vols[:3])
            place_note = "；".join(self.overview[v][:60] for v in m.place_vols[:2] if v in self.overview)
            lines.append(f"你刚才说「{m.sentence[:40]}」，但「{m.place}」是旅途{pv}的事（{place_note or '日记里是另一段经历'}），"
                         f"那段经历里没有「{m.person}」。")
            tl = self._timeline(m.person)
            if tl:
                lines.append(f"「{m.person}」和你真正的交集：{tl}")
            else:
                pv2 = "、".join(self.overview[v][:50] for v in m.person_vols[-3:] if v in self.overview)
                if pv2:
                    lines.append(f"「{m.person}」出现在：{pv2}")
        return ("【你记混了】" + "\n".join(lines)
                + "\n请重新回复这条消息：把两段经历分开，只说你确定的；拿不准的细节就说记不太清了。不要道歉，也不要提“记混了”这件事本身。")


def load(summary_dir: Path, characters_file: Path | None, places_file: Path | None, persona_text: str) -> FactChecker | None:
    try:
        return FactChecker(summary_dir, characters_file, places_file, persona_text)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"记混检查没建起来（不影响聊天）：{e}")
        return None
