"""说说图库：bot/qzone/images/ 里放伊蕾娜的图，发说说时从这里挑

- 每张图第一次用到时处理一次，结果记在 data/qzone/gallery.json：
  ① 压缩：长边缩到 2048 像素以内、转成 JPEG，存到 data/qzone/cache/（原图不动）。原图动辄 5～12 MB，直接上传容易失败；
  ② 认人：用本机的角色识别（tagger.py）确认画的是不是伊蕾娜；
  ③ 看图：用 deepseek-flash 写一段描述，并打上场景、动作、天气、季节等标签。
- 挑图：和今天见闻的关键词、当前季节越搭越优先；60 天内用过的不再用；实在不够就挑最久没用的。
- 不想用某张图：直接删掉文件，或者在 gallery.json 里给它加 "disabled": true。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
from datetime import date, datetime
from pathlib import Path

from nonebot import logger

from . import budget
from .vision import SELF_TAG, sanitize

EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# 上传用的图：按 QQ 空间自己的高清规格来（上传接口里写的是宽 ≤ 2048、高 ≤ 10000、质量 96）。
# 以前是“长边 ≤ 2048”，竖图会被多缩一截（3000×4200 缩成 1463×2048，空间其实能留 2048×2867）
UPLOAD_MAX_W = 2048
UPLOAD_MAX_H = 10000
UPLOAD_QUALITY = 95
LOOK_SIDE = 1024           # 给模型看的图：再小一点，省 token
UPLOAD_MAX_BYTES = 3 * 1024 * 1024
COMPRESS_VERSION = 2       # 压缩规格改了就加一，已有的缓存图会按新规格重新压一遍（描述、认人结果保留）

DESCRIBE_PROMPT = """这是一张画着伊蕾娜（灰色长发、黑色三角帽和长袍的旅行魔女）的画。它会用作她旅行日记的配图。
请只输出 JSON，格式：
{"desc": "……", "tags": ["……"], "season": "春/夏/秋/冬/不明", "time": "清晨/白天/傍晚/夜晚/不明", "weather": "晴/阴/雨/雪/不明"}
- desc：不超过 60 字，写画面里她在哪、在做什么、表情和姿势、周围的景物和光线；有文字就写出关键文字。用“她”指代，不要用“动漫”“动画”“二次元”“角色”这些词。
- tags：3～6 个简短的中文标签，从场景（室内、街道、森林、海边、天空、城镇……）、动作（读书、吃东西、飞行、睡觉、喝茶、施法……）、情绪（开心、得意、生气、无语、害羞、平静……）、物品（扫帚、面包、花、猫……）里挑最贴切的。
- 看不出来的就写“不明”。"""

SEASON_OF_MONTH = {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春", 6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}


def _to_jpeg(img, quality: int) -> bytes:
    for q in dict.fromkeys((quality, 90, 85, 78, 70)):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= UPLOAD_MAX_BYTES or q == 70:
            return buf.getvalue()
    return buf.getvalue()


def _compress(src, box: tuple[int, int], quality: int = 90) -> bytes:
    """src 是文件路径或图片字节；box 是 (最大宽, 最大高)，等比缩小、不放大。
    先缩小再处理（大原图整张解码很吃内存），再垫白底、转成 JPEG"""
    from PIL import Image

    img = Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
    img.seek(0)
    if img.format == "JPEG":
        img.draft("RGB", box)                  # JPEG 解码时直接按缩小后的尺寸解，省内存
    img.thumbnail(box, Image.LANCZOS)           # 先缩小，后面的垫底、转色都在小图上做
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    return _to_jpeg(img, quality)


def _json_of(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


class Gallery:
    def __init__(self, image_dir: Path, data_dir: Path, client, model: str, tagger=None):
        self.image_dir = image_dir
        self.cache_dir = data_dir / "cache"
        self.index_file = data_dir / "gallery.json"
        self.client = client
        self.model = model
        self.tagger = tagger
        self._lock = asyncio.Lock()
        try:
            self.items: dict[str, dict] = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = {}

    def _save(self) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------ 扫描、处理
    def files(self) -> list[Path]:
        if not self.image_dir.exists():
            return []
        return sorted(f for f in self.image_dir.iterdir() if f.is_file() and f.suffix.lower() in EXTS)

    def _stale(self, f: Path) -> bool:
        it = self.items.get(f.name)
        return not it or it.get("size") != f.stat().st_size or not it.get("desc") or \
            not it.get("cache") or not (self.cache_dir / it["cache"]).exists() or \
            it.get("cv") != COMPRESS_VERSION or \
            (it.get("self") is None and self.tagger is not None and self.tagger.ready)

    async def prepare(self, limit: int | None = None) -> int:
        """处理新放进来的图（压缩、认人、看图）；返回这次处理了几张"""
        async with self._lock:
            names = {f.name for f in self.files()}
            for gone in [n for n in self.items if n not in names]:      # 文件被删了：索引里也去掉
                self.items.pop(gone)
            todo = [f for f in self.files() if self._stale(f)]
            if limit is not None:
                todo = todo[:limit]
            done = 0
            for f in todo:
                try:
                    await self._prepare_one(f)
                    done += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"图库：处理 {f.name} 失败：{e}")
                    self.items.setdefault(f.name, {})["error"] = str(e)[:100]
                self._save()
            if done:
                logger.info(f"图库：处理了 {done} 张新图，共 {len(self.items)} 张")
            return done

    async def _prepare_one(self, f: Path) -> None:
        old = self.items.get(f.name, {})
        it = {"file": f.name, "size": f.stat().st_size, "used": old.get("used", []),
              "disabled": old.get("disabled", False)}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        up = await asyncio.to_thread(_compress, f, (UPLOAD_MAX_W, UPLOAD_MAX_H), UPLOAD_QUALITY)
        cache = self.cache_dir / (f.name + ".jpg")      # 带上原扩展名：a.png 和 a.jpg 不会共用一个缓存
        cache.write_bytes(up)
        old_cache = old.get("cache")
        if old_cache and old_cache != cache.name and (self.cache_dir / old_cache).exists():
            (self.cache_dir / old_cache).unlink(missing_ok=True)
        it["cache"] = cache.name
        it["cv"] = COMPRESS_VERSION
        look = await asyncio.to_thread(_compress, up, (LOOK_SIDE, LOOK_SIDE), 85)    # 给模型看的小图从压好的上传图生成，不再解码原图

        # 认人（本机，免费）。原图没换、以前认过（包括你在 gallery.json 里手动改过的）就沿用，不重新认
        same_file = old.get("size") == it["size"]
        it["self"] = old.get("self") if same_file else None
        if it["self"] is None and self.tagger is not None and self.tagger.ready:
            found = await self.tagger.detect(look)
            if found is not None:
                it["self"] = any(t == SELF_TAG for t, _ in found)
                if not it["self"]:
                    logger.warning(f"图库：{f.name} 没认出是伊蕾娜（认出：{[t for t, _ in found] or '无'}），挑图时会排在后面")

        # 看图（deepseek-flash）；描述已经有了、只是补认人时就不再花钱
        if old.get("desc") and old.get("size") == it["size"]:
            for k in ("desc", "tags", "season", "time", "weather"):
                it[k] = old.get(k)
        else:
            if not budget.can_background():
                raise RuntimeError("今天的钱花完了，明天再看这张图")
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(look).decode()}},
                ]}],
                temperature=0.2,
                max_tokens=300,
                extra_body={"thinking": {"type": "disabled"}},
            )
            budget.track(resp, "gallery", user=None, group=None)
            data = _json_of(resp.choices[0].message.content or "")
            desc = sanitize(str(data.get("desc") or "").strip())[:80]
            if not desc:
                raise RuntimeError("模型没有给出描述")
            it["desc"] = desc
            it["tags"] = [sanitize(str(t)).strip()[:8] for t in (data.get("tags") or []) if str(t).strip()][:6]
            for k in ("season", "time", "weather"):
                v = str(data.get(k) or "不明").strip()
                it[k] = v if v else "不明"
            logger.info(f"图库：{f.name} → {it['desc']}｜{it['tags']}")
        self.items[f.name] = it

    # ------------------------------------------------------------ 挑图
    def usable(self) -> list[dict]:
        return [it for it in self.items.values()
                if it.get("desc") and it.get("cache") and not it.get("disabled")
                and (self.cache_dir / it["cache"]).exists() and (self.image_dir / it["file"]).exists()]

    @staticmethod
    def _days_since_used(it: dict, today: date) -> float:
        used = it.get("used") or []
        if not used:
            return 1e9
        return (today - date.fromisoformat(used[-1])).days

    def pick(self, keywords: list[str], reuse_days: int, today: date | None = None, exclude: set[str] | None = None) -> dict | None:
        today = today or datetime.now().date()
        pool = [it for it in self.usable() if it["file"] not in (exclude or set())]
        if not pool:
            return None
        fresh = [it for it in pool if self._days_since_used(it, today) >= reuse_days]
        if not fresh:                               # 图都用过了：挑最久没用的那几张
            pool.sort(key=lambda it: -self._days_since_used(it, today))
            fresh = pool[: max(1, len(pool) // 4)]
        season = SEASON_OF_MONTH[today.month]

        def score(it: dict) -> float:
            text = it.get("desc", "") + "".join(it.get("tags") or [])
            s = sum(2.0 for k in keywords if k and len(k) >= 1 and k in text)
            if it.get("season") == season:
                s += 1.0
            elif it.get("season") not in ("不明", None, "", season):
                s -= 1.0
            if it.get("self") is False:
                s -= 1.5
            return s + random.random() * 1.5

        ranked = sorted(fresh, key=score, reverse=True)
        return random.choice(ranked[:3])

    def image_bytes(self, it: dict) -> bytes:
        return (self.cache_dir / it["cache"]).read_bytes()

    def mark_used(self, name: str, day: date | None = None) -> None:
        it = self.items.get(name)
        if not it:
            return
        it.setdefault("used", []).append((day or datetime.now().date()).isoformat())
        it["used"] = it["used"][-20:]
        self._save()

    def stats(self, reuse_days: int) -> str:
        files = self.files()
        today = datetime.now().date()
        ok = self.usable()
        fresh = [it for it in ok if self._days_since_used(it, today) >= reuse_days]
        not_self = [it["file"] for it in ok if it.get("self") is False]
        errors = [n for n, it in self.items.items() if it.get("error") and not it.get("desc")]
        pending = [f.name for f in files if self._stale(f)]
        lines = [f"图库：{len(files)} 张｜可用 {len(ok)}｜{reuse_days} 天内没用过的 {len(fresh)}"]
        if pending:
            lines.append(f"还没处理：{len(pending)} 张（发说说前会自动处理）")
        if not_self:
            lines.append("没认出是伊蕾娜（会排在后面）：" + "、".join(not_self[:5]) + ("……" if len(not_self) > 5 else ""))
        if errors:
            lines.append("处理失败：" + "、".join(errors[:5]))
        if len(ok) < 10:
            lines.append("⚠️ 可用的图不到 10 张，记得补图")
        return "\n".join(lines)
