"""
表情包：用小号“收藏表情”里的伊蕾娜表情表达情绪

- 表情库：调用 NapCat 的 fetch_custom_face（获取收藏表情）拿到地址，下载到 data/stickers/，按内容哈希去重。
  发送时用本地文件（base64），不依赖 QQ 链接的有效期
- 打标签：每张新表情交给看图模型（deepseek-flash）看一次，从固定词表里选 1～2 个情绪，再写一句描述（含配字）。
  结果存在 data/stickers/index.json，之后不再重复看；管理员可以用 /表情 手动改
- 挑选：按情绪挑，同一个会话最近发过的几张不重复；情绪对不上时试相近的情绪
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import random
import re
import time
import urllib.request
from pathlib import Path

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment

INDEX_VERSION = 1

# 情绪词表（固定）：模型只能从这里选
EMOTIONS = ["得意", "嫌弃", "无语", "生气", "开心", "害羞", "疑惑", "委屈", "震惊", "敷衍"]

# 模型或管理员写的近义词 → 词表里的词
ALIASES = {
    "高兴": "开心", "快乐": "开心", "喜悦": "开心", "兴奋": "开心", "笑": "开心", "满意": "开心",
    "骄傲": "得意", "自豪": "得意", "自恋": "得意", "臭美": "得意", "嘚瑟": "得意", "炫耀": "得意",
    "鄙视": "嫌弃", "不屑": "嫌弃", "厌恶": "嫌弃", "白眼": "嫌弃", "瞧不起": "嫌弃",
    "无奈": "无语", "沉默": "无语", "尴尬": "无语", "汗": "无语", "服了": "无语",
    "愤怒": "生气", "气": "生气", "恼火": "生气", "不满": "生气", "凶": "生气",
    "脸红": "害羞", "羞": "害羞", "不好意思": "害羞", "羞涩": "害羞",
    "困惑": "疑惑", "问号": "疑惑", "不解": "疑惑", "好奇": "疑惑", "纳闷": "疑惑",
    "哭": "委屈", "难过": "委屈", "伤心": "委屈", "可怜": "委屈", "失落": "委屈", "心酸": "委屈",
    "惊讶": "震惊", "吃惊": "震惊", "惊吓": "震惊", "意外": "震惊",
    "随便": "敷衍", "好吧": "敷衍", "冷淡": "敷衍", "懒得理": "敷衍", "爱答不理": "敷衍", "无所谓": "敷衍",
    # 带“笑”“气”但不是开心、生气的
    "冷笑": "嫌弃", "嘲笑": "嫌弃", "讥笑": "嫌弃", "嗤笑": "嫌弃", "嘲讽": "嫌弃", "坏笑": "得意", "偷笑": "得意",
    "奸笑": "得意", "苦笑": "无语", "干笑": "无语", "傻笑": "开心", "微笑": "开心", "大笑": "开心",
    "气鼓鼓": "生气", "气呼呼": "生气", "生闷气": "生气",
    # 否定说法
    "不开心": "委屈", "不高兴": "生气", "不爽": "生气", "不满意": "生气", "没意思": "无语", "不屑一顾": "嫌弃",
}

# 包含匹配只用两个字以上的近义词（长的先试）；单字（笑、气、汗、羞、凶、哭）只做完全匹配，
# 免得“客气”被归成生气、“冷笑”被归成开心
_CONTAINS = sorted((k for k in ALIASES if len(k) >= 2), key=len, reverse=True)

# 库里没有这种情绪时，退而求其次用哪些
NEIGHBORS = {
    "开心": ["得意"], "得意": ["开心"], "嫌弃": ["无语"], "无语": ["嫌弃", "敷衍"], "生气": ["嫌弃"],
    "害羞": [], "疑惑": ["震惊", "无语"], "委屈": [], "震惊": ["疑惑"], "敷衍": ["无语"],
}

LABEL_PROMPT = (
    "这是一张表情图，画的是伊蕾娜（灰色长发、平时戴黑色三角帽的旅行魔女）。\n"
    "1. 判断它表达的情绪，只能从这些词里选 1～2 个最贴切的：" + "、".join(EMOTIONS) + "。\n"
    "2. 用不超过 25 个字描述她在图里的样子、动作和表情；图里有配字就写出配字原文。\n"
    "描述里不要用“动漫”“动画”“二次元”“表情包”“角色”这些词。\n"
    "严格按下面两行的格式输出，不要输出别的：\n"
    "情绪：嫌弃、无语\n"
    "描述：叉腰斜眼看人，配字“哈？”"
)

_MAGIC = {b"\xff\xd8\xff": ("image/jpeg", "jpg"), b"\x89PNG": ("image/png", "png"),
          b"GIF8": ("image/gif", "gif"), b"RIFF": ("image/webp", "webp")}


def _kind(data: bytes) -> tuple[str, str] | None:
    for sig, k in _MAGIC.items():
        if data.startswith(sig):
            return k
    return None


def normalize(word: str) -> str | None:
    """把一个情绪词归到词表里；归不进去返回 None"""
    w = word.strip().strip("[]【】「」\"'“”。，,、 ")
    if w in EMOTIONS:
        return w
    if w in ALIASES:
        return ALIASES[w]
    for k in _CONTAINS:                   # “有点嫌弃”“一脸冷笑”这种：包含关系也算（只用两个字以上的词）
        if k in w:
            return ALIASES[k]
    if re.match(r"^[不没别]", w):          # “不生气”“没开心”：否定的说法不按包含匹配
        return None
    for e in EMOTIONS:
        if e in w:
            return e
    return None


def parse_tags(text: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[、，,/\s]+", text):
        e = normalize(part) if part else None
        if e and e not in out:
            out.append(e)
    return out[:2]


def _small_png(raw: bytes, max_side: int = 512) -> bytes | None:
    """打标签前先缩小：取第一帧（动图也一样），缩到最长边不超过 max_side，转成 PNG。
    看图模型不用看原图，这样请求小得多，也不用管动图、webp 认不认"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im.seek(0)
            frame = im.convert("RGBA")
            frame.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            frame.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"表情：缩图失败（{e}），改发原图")
        return None


class StickerStore:
    def __init__(self, root: Path, client, model: str, *, sanitize=None,
                 max_bytes: int = 8 * 1024 * 1024, timeout: float = 20.0):
        self.root = root
        self.index_file = root / "index.json"
        self.client = client
        self.model = model
        self.sanitize = sanitize or (lambda s: s)
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.items: dict[str, dict] = {}      # 内容哈希 -> 记录
        self.next_no = 1
        self.last_refresh = 0.0
        self._labeling = False
        self._refresh_lock = asyncio.Lock()
        try:
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
            if data.get("_v") == INDEX_VERSION:
                self.items = data.get("items", {})
                self.next_no = int(data.get("next_no", 1))
                self.last_refresh = float(data.get("last_refresh", 0))   # 存在文件里：重开 start.bat 不会重新拉
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    # ------------------------------------------------------------ 存取
    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"_v": INDEX_VERSION, "next_no": self.next_no, "last_refresh": self.last_refresh,
                                   "items": self.items},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_file)

    def usable(self) -> list[dict]:
        """能发的表情：没被取消收藏、没被禁用、打好了标签、文件还在"""
        return [it for it in self.items.values()
                if not it.get("removed") and not it.get("disabled") and it.get("tags")
                and (self.root / it["file"]).exists()]

    def by_no(self, no: int) -> dict | None:
        return next((it for it in self.items.values() if it.get("no") == no), None)

    def unlabeled(self) -> list[dict]:
        return [it for it in self.items.values()
                if not it.get("removed") and not it.get("labeled") and it.get("fails", 0) < 3]

    # ------------------------------------------------------------ 拉取收藏
    def _download_sync(self, url: str) -> bytes | None:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = r.read(self.max_bytes + 1)
        return None if len(data) > self.max_bytes else data

    async def refresh(self, bot, count: int = 200) -> tuple[int, int, int]:
        """重新拉一遍收藏表情。返回（新增, 取消收藏, 现在可用）"""
        async with self._refresh_lock:
            raw = await bot.call_api("fetch_custom_face", count=count)
            if isinstance(raw, dict):          # 不同版本可能包一层
                raw = raw.get("data") or raw.get("emojiInfoList") or raw.get("list") or []
            urls = []
            for x in raw or []:
                u = x if isinstance(x, str) else (x.get("url") or x.get("file") or "") if isinstance(x, dict) else ""
                if u:
                    urls.append(u)
            if not urls:
                logger.warning("表情：收藏表情是空的（或者这个 NapCat 版本不支持 fetch_custom_face）")
            by_url = {it.get("url"): h for h, it in self.items.items()}
            seen: set[str] = set()
            added = 0
            for url in dict.fromkeys(urls):
                h = by_url.get(url)
                if h and (self.root / self.items[h]["file"]).exists():
                    seen.add(h)
                    continue
                try:
                    data = await asyncio.to_thread(self._download_sync, url)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"表情：下载失败（{e}）：{url[:80]}")
                    continue
                kind = _kind(data or b"")
                if not data or not kind:
                    logger.info(f"表情：不认识的格式或文件太大，跳过：{url[:80]}")
                    continue
                h = hashlib.sha1(data).hexdigest()[:16]
                seen.add(h)
                it = self.items.get(h)
                if it:                          # 同一张图换了地址
                    it["url"] = url
                    it.pop("removed", None)
                    if not (self.root / it["file"]).exists():
                        (self.root / it["file"]).write_bytes(data)
                    continue
                self.root.mkdir(parents=True, exist_ok=True)
                fname = f"{h}.{kind[1]}"
                (self.root / fname).write_bytes(data)
                self.items[h] = {"no": self.next_no, "file": fname, "url": url, "added": time.time()}
                self.next_no += 1
                added += 1
            removed = 0
            # 这次没见到的表情：连续两次拉取都没见到才算取消收藏。
            # 只没见到一次的，可能只是地址变了、这次又没下载成功，先照常能发
            # 拉取失败（空列表）、或者拉满了上限（后面可能还有没拉到的）时，不标取消收藏
            truncated = len(urls) >= count
            if truncated:
                logger.warning(f"表情：收藏表情拉满了 {count} 张，可能没拉全；这次不标取消收藏，请调大 STICKER_FETCH_COUNT")
            if urls and not truncated:
                for h, it in self.items.items():
                    if h in seen:
                        it.pop("removed", None)
                        it.pop("miss", None)
                    elif not it.get("removed"):
                        it["miss"] = it.get("miss", 0) + 1
                        if it["miss"] >= 2:
                            it["removed"] = True
                            removed += 1
            else:
                for h in seen:
                    self.items[h].pop("removed", None)
                    self.items[h].pop("miss", None)
            self.last_refresh = time.time()
            self.save()
            n = len(self.usable())
            missing = sum(1 for it in self.items.values() if it.get("miss") and not it.get("removed"))
            note = f"，{missing} 张这次没见到（下次还没有才算取消收藏）" if missing else ""
            logger.info(f"表情：收藏 {len(urls)} 张，新增 {added}，取消收藏 {removed}{note}，可用 {n}，待打标签 {len(self.unlabeled())}")
            return added, removed, n

    # ------------------------------------------------------------ 打标签
    async def _ask(self, raw: bytes, mime: str) -> str:
        b64 = base64.b64encode(raw).decode()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": LABEL_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
            temperature=0.2,
            max_tokens=100,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return resp.choices[0].message.content or ""

    async def label_one(self, it: dict) -> bool:
        path = self.root / it["file"]
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            it["fails"] = 9
            return False
        png = await asyncio.to_thread(_small_png, raw)
        if png:
            data, mime = png, "image/png"
        else:                                      # 缩不了（比如 Pillow 没装）：发原图
            kind = _kind(raw)
            data, mime = raw, (kind[0] if kind else "image/png")
        try:
            out = await self._ask(data, mime)
        except Exception as e:  # noqa: BLE001
            it["fails"] = it.get("fails", 0) + 1
            logger.warning(f"表情：{it['no']} 号打标签失败（第 {it['fails']} 次）：{e}")
            return False
        tags, desc = [], ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(("情绪", "情緒")):
                tags = parse_tags(re.split(r"[:：]", line, maxsplit=1)[-1])
            elif line.startswith(("描述", "樣子", "样子")):
                desc = re.split(r"[:：]", line, maxsplit=1)[-1].strip()
        if not tags:                             # 没按格式写：整段里找情绪词
            tags = parse_tags(out)
        desc = self.sanitize(desc.replace("表情包", "画像").replace("表情图", "画像"))[:30]
        if not it.get("manual"):
            it["tags"] = tags
        it["desc"] = desc
        it["labeled"] = True
        it.pop("fails", None)
        logger.info(f"表情：{it['no']} 号 → {'、'.join(it.get('tags') or []) or '（没认出情绪）'}｜{desc}")
        return True

    async def label_pending(self, can_run=lambda: True, gap: float = 2.0) -> int:
        """把还没打标签的表情一张张看一遍；can_run() 返回 False（比如到了高峰时段）就先停下"""
        if self._labeling:
            return 0
        self._labeling = True
        done = 0
        try:
            for it in self.unlabeled():
                if not can_run():
                    break
                if await self.label_one(it):
                    done += 1
                self.save()
                await asyncio.sleep(gap)
        finally:
            self._labeling = False
        return done

    # ------------------------------------------------------------ 挑选
    def emotions(self, allowed: set[str] | None = None) -> list[str]:
        """库里现在有哪些情绪（按词表顺序）"""
        have = {t for it in self.usable() for t in it["tags"]}
        return [e for e in EMOTIONS if e in have and (allowed is None or e in allowed)]

    def pick(self, emotion: str, avoid: set[str] | None = None, allowed: set[str] | None = None) -> dict | None:
        """按情绪挑一张；最近发过的尽量不选；这个情绪没有就试相近的"""
        e = normalize(emotion)
        if not e:
            return None
        avoid = avoid or set()
        for cand in [e] + NEIGHBORS.get(e, []):
            if allowed is not None and cand not in allowed:
                continue
            pool = [it for it in self.usable() if cand in it["tags"]]
            if not pool:
                continue
            fresh = [it for it in pool if it["file"] not in avoid]
            return random.choice(fresh or pool)
        return None

    def segment(self, it: dict, sub_type: int = 1) -> MessageSegment:
        data = (self.root / it["file"]).read_bytes()
        seg = {"file": "base64://" + base64.b64encode(data).decode()}
        if sub_type >= 0:
            seg["sub_type"] = sub_type          # NapCat：1 = 显示成表情样式（小图）；设成 -1 就按普通图片发
            seg["summary"] = "[动画表情]"
        return MessageSegment("image", seg)

    # ------------------------------------------------------------ 管理
    def set_tags(self, no: int, words: list[str]) -> list[str] | None:
        it = self.by_no(no)
        if not it:
            return None
        tags = []
        for w in words:
            e = normalize(w)
            if e and e not in tags:
                tags.append(e)
        if not tags:
            return []
        it["tags"], it["manual"], it["labeled"] = tags, True, True
        self.save()
        return tags

    def set_disabled(self, no: int, value: bool) -> bool:
        it = self.by_no(no)
        if not it:
            return False
        it["disabled"] = value
        self.save()
        return True

    def list_lines(self) -> list[str]:
        lines = []
        for it in sorted(self.items.values(), key=lambda x: x.get("no", 0)):
            if it.get("removed"):
                state = "（已取消收藏）"
            elif it.get("disabled"):
                state = "（已禁用）"
            elif not it.get("labeled"):
                state = "（还没打标签）"
            else:
                state = ""
            tags = "、".join(it.get("tags") or []) or "无情绪"
            manual = "✎" if it.get("manual") else ""
            lines.append(f"{it['no']}. {tags}{manual}｜{it.get('desc') or ''}{state}")
        return lines

    def summary(self) -> str:
        counts = {e: 0 for e in EMOTIONS}
        for it in self.usable():
            for t in it["tags"]:
                counts[t] += 1
        have = "、".join(f"{e}{n}" for e, n in counts.items() if n) or "无"
        missing = "、".join(e for e, n in counts.items() if not n)
        s = f"可用 {len(self.usable())} 张｜各情绪：{have}"
        if missing:
            s += f"｜没有：{missing}"
        pending = len(self.unlabeled())
        if pending:
            s += f"｜待打标签 {pending} 张"
        return s
