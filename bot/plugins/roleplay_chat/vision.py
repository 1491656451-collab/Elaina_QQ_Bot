"""
识图：把对方发来的图片先“看”一遍，变成一句简短描述，再交给伊蕾娜回复

- 第一步（本机，免费）：用 WD14 Tagger 认出画里是哪个动漫角色，见 tagger.py。认出伊蕾娜时会告诉第二步
- 第二步：用 deepseek-flash 的图片理解写一句描述（OpenAI 兼容格式，image_url + base64）。
  认出是伊蕾娜时，描述写成「伊蕾娜本人的画像，……」，她就知道画的是自己
- 图片先由本机下载（只用 Python 自带的 urllib）再转 base64 发给 DeepSeek（QQ 的图片链接有时效，外部服务器不一定能直接访问）
- 描述里不出现“动漫”“二次元”之类的词（伊蕾娜不知道这些），免得触发防出戏重写
- 同一张图（包括反复使用的表情包）只看一次：结果缓存在 data/vision_cache.json。
  角色识别还没准备好时看的图，等它准备好之后再遇到会重新看一遍
- 描述会以「[图片：……]」的形式写进聊天记录，后面的对话和长期记忆也能用上
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import urllib.request
from collections import OrderedDict
from pathlib import Path

from nonebot import logger

CACHE_VERSION = 2
SELF_TAG = "elaina_(majo_no_tabitabi)"
SELF_NAME = "伊蕾娜"
# 认得出、并且她认识的人（Tagger v3 的词表里，《魔女之旅》只有伊蕾娜一个人）
KNOWN = {SELF_TAG: SELF_NAME}

DESCRIBE_PROMPT = (
    "用简短的中文描述这张图片，不超过 50 字。"
    "写出主要内容；图里有文字就写出关键文字；如果是表情包，说明它表达的情绪或意思。"
    "画出来的人物写成“画中的少女”“画中人”之类，不要用“动漫”“动画”“二次元”“番剧”“角色”这些词。"
    "只输出描述本身，不要加“图片中”“这是一张”之类的开头。"
)
SELF_HINT = (
    "\n已确认：画中人物是伊蕾娜（灰色长发、黑色三角帽和长袍的旅行魔女）。"
    "描述请以“伊蕾娜本人的画像”开头，再写她在画里的样子、动作、表情，有配字就写出配字。"
    "例如：伊蕾娜本人的画像，戴着魔女帽一脸嫌弃，配字“嫌弃”。"
)
MAYBE_HINT = (
    "\n画中人物有可能是伊蕾娜（灰色长发、琉璃色眼睛；平时戴黑色三角帽、穿黑色长袍，"
    "但画里也可能穿便服、不戴帽子）。请仔细看：发色、眼睛和整体样子都和她一致，描述就以“伊蕾娜本人的画像”开头；"
    "不像的话就正常描述，不要提伊蕾娜。"
)
NOT_SELF_NOTE = "（画的不是你）"
# 描述里有这些词，说明画里有人。本机识别认过、结论是“不是伊蕾娜”时，给她补一句，免得她把随便一个画中少女认成自己
_PERSON_RE = re.compile(r"画中|少女|少年|女孩|男孩|女生|男生|人物|女子|男子|姑娘")
OTHER_HINT = "\n画中人物是{names}。描述里不要写出这个名字，只写外貌、动作和表情。"

# 模型偶尔还是会写出这些词，统一换成伊蕾娜能理解的说法
_REWRITE = [
    (re.compile(r"(动漫|动画|二次元|番剧)(风格|风|画风)?的?(角色|人物)"), "画中人"),
    (re.compile(r"(动漫|动画|二次元|番剧)(风格|风|画风)"), "插画风格"),
    (re.compile(r"(动漫|动画|二次元|番剧)"), "插画"),
    (re.compile(r"角色"), "人物"),
]

_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}


def _mime(data: bytes) -> str | None:
    for sig, mime in _MAGIC.items():
        if data.startswith(sig):
            return mime
    return None


def _readable(tag: str) -> str:
    """hatsune_miku_(vocaloid) → hatsune miku（vocaloid）"""
    m = re.match(r"^(.*?)(?:_\(([^)]*)\))?$", tag)
    name, series = (m.group(1), m.group(2)) if m else (tag, None)
    name = name.replace("_", " ")
    return f"{name}（{series.replace('_', ' ')}）" if series else name


def sanitize(desc: str) -> str:
    for pat, rep in _REWRITE:
        desc = pat.sub(rep, desc)
    return desc


class Vision:
    def __init__(self, client, model: str, cache_file: Path, *, tagger=None, maybe: float = 0.35,
                 max_bytes: int = 5 * 1024 * 1024, max_tokens: int = 120, timeout: float = 15.0):
        self.client = client
        self.maybe = maybe          # 伊蕾娜置信度在 maybe～门槛之间：让 deepseek-flash 再确认一次
        self.model = model
        self.cache_file = cache_file
        self.tagger = tagger
        self.max_bytes = max_bytes
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._raw: OrderedDict[str, bytes] = OrderedDict()   # 最近下载过的几张图，免得刚识别完角色又重新下载
        self._cache: dict[str, dict] = {}
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("_v") == CACHE_VERSION:
                self._cache = data.get("items", {})
            else:
                logger.info("识图：旧版缓存作废（以前的描述认不出人物），图片会重新看一遍")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        # 缓存最多留 2000 条，太多了就丢掉最早的
        if len(self._cache) > 2000:
            self._cache = dict(list(self._cache.items())[-2000:])
        self.cache_file.write_text(json.dumps({"_v": CACHE_VERSION, "items": self._cache}, ensure_ascii=False),
                                   encoding="utf-8")

    @property
    def tagging(self) -> bool:
        return bool(self.tagger and self.tagger.ready)

    @staticmethod
    def is_self(entry: dict | None) -> bool:
        """缓存记录里：画的是不是伊蕾娜。管理员用 /认图 指定过的优先，其次是本机识别，最后是看图时确认的"""
        if not entry:
            return False
        if "o" in entry:
            return bool(entry["o"])
        return SELF_TAG in entry.get("c", []) or bool(entry.get("m"))

    def _is_maybe(self, entry: dict | None) -> bool:
        return bool(entry) and "o" not in entry and not self.is_self(entry) and entry.get("s", 0) >= self.maybe

    @staticmethod
    def cache_key(data: dict) -> str:
        return str(data.get("file_unique") or data.get("file") or data.get("url") or "")

    # ------------------------------------------------------------ 下载
    def _download_sync(self, url: str) -> bytes | None:
        # 只用 Python 自带的 urllib，不依赖额外的库
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = r.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            logger.info(f"识图：图片太大（超过 {self.max_bytes // 1024 // 1024} MB），跳过")
            return None
        return data

    async def _get_raw(self, data: dict, key: str) -> bytes | None:
        if key and key in self._raw:
            return self._raw[key]
        url = data.get("url") or (data.get("file") if str(data.get("file", "")).startswith("http") else None)
        if not url:
            return None
        try:
            raw = await asyncio.to_thread(self._download_sync, url)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"识图：图片下载失败：{e}")
            return None
        if raw and key:
            self._raw[key] = raw
            while len(self._raw) > 8:
                self._raw.popitem(last=False)
        return raw

    # ------------------------------------------------------------ 认人
    async def _characters(self, key: str, raw: bytes) -> list[str] | None:
        """返回认出的角色标签；角色识别没准备好时返回 None"""
        r = await self.tagger.detect_full(raw) if self.tagging else None
        if r is None:
            return None
        found, self_score = r
        tags = [t for t, _ in found]
        if found:
            logger.info("角色识别：" + "、".join(f"{t}（{p:.2f}）" for t, p in found))
        if SELF_TAG not in tags and self_score >= 0.1:
            how = "交给看图模型再确认" if self_score >= self.maybe else "当作不是"
            logger.info(f"角色识别：像伊蕾娜但没过门槛（{self_score:.2f}，门槛 {self.tagger.threshold}），{how}")
        if key:
            entry = self._cache.setdefault(key, {})
            entry["c"], entry["t"], entry["s"] = tags, 1, round(self_score, 3)
        return tags

    async def shows_self(self, data: dict) -> bool:
        """这张图画的是不是伊蕾娜。只用本机的角色识别，不调用 DeepSeek；识别没准备好就当“不是”"""
        if not self.tagging:
            return False
        key = self.cache_key(data)
        entry = self._cache.get(key) if key else None
        if entry and ("o" in entry or entry.get("t")):
            return self.is_self(entry)
        raw = await self._get_raw(data, key)
        if not raw or not _mime(raw):
            return False
        tags = await self._characters(key, raw)
        if key and tags is not None:
            self._save()
            return self.is_self(self._cache.get(key))
        return bool(tags) and SELF_TAG in tags

    # ------------------------------------------------------------ 描述
    async def describe(self, data: dict) -> str | None:
        """data 是 OneBot image 段的 data（含 url / file）。返回一句描述；失败返回 None。
        本机认过、确定画的不是伊蕾娜、画里又有人时，末尾加「（画的不是你）」"""
        desc = await self._describe(data)
        if not desc or SELF_NAME in desc:
            return desc
        key = self.cache_key(data)
        entry = self._cache.get(key) if key else None
        judged = bool(entry) and ("o" in entry or entry.get("t"))
        if judged and not self.is_self(entry) and _PERSON_RE.search(desc):
            return desc + NOT_SELF_NOTE
        return desc

    async def _describe(self, data: dict) -> str | None:
        key = self.cache_key(data)
        entry = self._cache.get(key) if key else None
        # 缓存里有描述，并且（已经认过人，或者现在也没法认人）就直接用
        if entry and entry.get("d") and (entry.get("t") or not self.tagging):
            return entry["d"]
        raw = await self._get_raw(data, key)
        if not raw:
            return entry.get("d") if entry else None   # 链接过期了：有旧描述就先用旧的
        mime = _mime(raw)
        if not mime:
            logger.info("识图：不认识的图片格式，跳过")
            return None

        tags = entry.get("c") if entry and entry.get("t") else await self._characters(key, raw)
        entry = self._cache.get(key) if key else None
        override = entry.get("o") if entry else None
        # 这张图算不算伊蕾娜：管理员指定的 > 本机识别过了门槛的；置信度中等的交给看图模型确认
        sure = override is True or (override is None and bool(tags) and SELF_TAG in tags)
        maybe = not sure and override is None and self._is_maybe(entry)
        prompt = DESCRIBE_PROMPT
        if sure:
            prompt += SELF_HINT
        elif maybe:
            prompt += MAYBE_HINT
        others = [_readable(t) for t in (tags or []) if t not in KNOWN]
        if others:
            prompt += OTHER_HINT.format(names="、".join(others))

        b64 = base64.b64encode(raw).decode()
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }],
                temperature=0.2,
                max_tokens=self.max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
            desc = (resp.choices[0].message.content or "").strip().replace("\n", " ")[:70]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"识图：调用模型失败：{e}")
            return entry.get("d") if entry else None
        desc = sanitize(desc)
        if sure and SELF_NAME not in desc:
            desc = f"伊蕾娜本人的画像，{desc}"          # 模型没按要求写开头时补上
        if override is False and desc.startswith("伊蕾娜本人的画像"):
            desc = desc.removeprefix("伊蕾娜本人的画像").lstrip("，,。 ")
        confirmed = maybe and desc.startswith("伊蕾娜本人的画像")
        if desc:
            if key:
                entry = self._cache.setdefault(key, {})
                entry["d"] = desc
                entry.setdefault("t", 1 if tags is not None else 0)
                if tags is not None:
                    entry["c"], entry["t"] = tags, 1
                if maybe:
                    entry["m"] = confirmed
                self._save()
            logger.info(f"识图：{desc}" + ("（看图模型确认是伊蕾娜）" if confirmed else ""))
        return desc or None

    # ------------------------------------------------------------ 管理员纠正：/认图
    def info(self, data: dict) -> dict:
        key = self.cache_key(data)
        return dict(self._cache.get(key, {})) if key else {}

    def set_override(self, data: dict, value: bool | None) -> bool:
        """管理员指定这张图是不是伊蕾娜（None 表示取消指定）。清掉旧描述，下次看到时按新结论重新描述"""
        key = self.cache_key(data)
        if not key:
            return False
        entry = self._cache.setdefault(key, {})
        if value is None:
            entry.pop("o", None)
        else:
            entry["o"] = value
        entry.pop("d", None)
        entry.pop("m", None)
        self._save()
        return True
