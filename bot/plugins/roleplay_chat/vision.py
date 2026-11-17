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
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path

from nonebot import logger

from . import budget

CACHE_VERSION = 2
CACHE_MAX = 2000    # 缓存最多留多少张图；超了就丢最久没用过的，管理员 /认图 纠正过的一律保留
SELF_TAG = "elaina_(majo_no_tabitabi)"
SELF_NAME = "伊蕾娜"
# 认得出、并且她认识的人（Tagger v3 的词表里，《魔女之旅》只有伊蕾娜一个人）
KNOWN = {SELF_TAG: SELF_NAME}
# 11/05 描述只写看到的，不替发图的人猜用意（“在调侃对方”这类判断留给聊天模型结合上下文去做）。
# 11/05 复杂的图放宽：一般 50 字以内，文字多的图（聊天截图、多格漫画……）可以写到 100 字。
# 这里是硬上限：模型偶尔写超了，就在这个长度以内最后一个句读处截断
DESC_MAX = 180   # 11/06 聊天截图可以写到 150 字（原话要抄全，聊天那边拿来对聊天记录），上限从 130 调到 180

# 11/06 聊天截图（聊天 agent《交接-截图认出自己-识图-1106》）：看图模型只把“谁说了什么”抄清楚，原话加引号，不认哪句是她说的。
# 先试过让它按名字、头像认出她，14 次里错了 6 次（左右弄反、把叫“伊蕾娜”的别人认成她，见《识图截图测试报告-1106》），
# 用户 11/06 定的：认她交给聊天那边拿引号里的原话对聊天记录
# 11/06 底片比对（selfmatch.py）：和她自己表情库 / 说说图库里某张是同一张图，就算她（用户说这两个库只放她的图）。
# 管本机认人认不出的近脸截图、便服图。相对 bot 目录；设成空列表就关掉
SELF_LIBRARY_DIRS = ["data/stickers", "qzone/images"]
_BOT_DIR = Path(__file__).resolve().parents[2]
# 长条图（长比宽 ≥ 2，手机截图都是这样）：本机认人认出伊蕾娜，多半是聊天背景、头像或贴图，不直接算她的画像
TALL_RATIO = 2.0

# 11/17：表情读偏（K3 闭眼张嘴大笑、托下巴的得意脸被写成“张嘴吐舌……无奈又嫌弃”，她接话意思全反；动图 G5、G9、G20 也是）。
# 第一版只靠提示词（嘴型照实写、含泪写流泪、拿不准不写情绪）：K3 还是写吐舌，反倒把 8 张表情库图的汗滴写成了眼泪，
# 好几张“嫌弃”“无语”的情绪也丢了（《识图表情测试》11-17 14:59）。所以改成：本机认人顺便读出的表情标签（tagger.EXPR_TAGS，
# 这个模型就是按二次元图训练的，K3 认得出闭眼 + 张嘴 + 笑、没有吐舌，汗滴和眼泪也分得清）告诉看图模型，提示词只留一句“情绪合起来看”。
# FACE_RULE、EXPR_HINT 单独放，测试时可以拿掉做新旧对比
FACE_RULE = "情绪看眼睛、眉毛、嘴角和手势合起来判断，不要只凭嘴型：闭眼或眯眼、嘴角上扬是在笑、得意，不是嫌弃。"
EXPR_HINT = ("\n本机识别读出的{where}表情特征（比较准，描述表情以它为准；标了“不太确定”的自己再看看）：{words}。"
             "情绪词照常写。")
EXPR_NO_TEAR = "特征里没有“眼泪”“哭”就不要写流泪、含泪，脸边、额头的水滴是汗。"
EXPR_NO_TONGUE = "特征里没有“吐舌”就不要写吐舌，张着嘴能看到舌头不算吐舌。"
EXPR_ON = True
DESCRIBE_PROMPT = (
    "用简短的中文描述这张图片，一般不超过 50 字。"
    "写出主要内容；图里有文字就写出关键文字；如果是表情包，写出画面上的表情、动作和配字，"
    "画里人物的情绪（开心、无语、嫌弃之类）和梗本身的意思可以写，但别替发图的人猜用意（比如“在调侃对方”“在敷衍”“想求安慰”），"
    "这要看的人结合聊天自己判断。"
    + FACE_RULE +
    "图里文字多、内容复杂（聊天截图、长段配文、多格漫画、通知公告、表格清单）时可以写到 100 字，"
    "把关键的话写全，聊天截图写清谁说了什么。"
    "画出来的人物写成“画中的少女”“画中人”之类，不要用“动漫”“动画”“二次元”“番剧”“角色”这些词。"
    "如果拍的是实物（手办、人偶、玩偶、立牌、雕像之类），开头写明，比如“一尊人偶：……”“一个玩偶：……”，"
    "里面的人物写成“人偶”，不要写成“画中的少女”。"
    "如果是聊天截图（或聊天记录的截图）：开头写明“群聊截图”或“私聊截图”，然后只写聊天内容——每条消息的文字，"
    "消息里发的图片、表情包也简单说一下是什么（带花边、图案的气泡框只是气泡样式，里面的字照常当文字抄）；"
    "聊天背景、手机状态栏、输入框、按钮这些都不写。可以写到 150 字。"
    "写清每句话是谁说的，原话照抄、加引号：群聊写每条消息上方显示的名字；私聊每条消息没有名字，写“左边”“右边”，"
    "开头写一句顶部标题（那是左边那位的名字），右边是截图的人。不要用“你”“我”“对方”来称呼截图里的人。"
    "只输出描述本身，不要加“图片中”“这是一张”之类的开头。"
)
SELF_HINT = (
    "\n已确认：画中人物是伊蕾娜（灰色长发、黑色三角帽和长袍的旅行魔女）。"
    "描述请以“伊蕾娜本人的画像”开头，再写她在画里的样子、动作、表情，有配字就写出配字。"
    "例如：伊蕾娜本人的画像，戴着魔女帽一脸嫌弃，配字“嫌弃”。"
    "如果拍的是实物，开头还是写“伊蕾娜本人的画像”，后面写明，例如：伊蕾娜本人的画像，是一尊人偶，穿白裙举着手。"
)
MAYBE_HINT = (
    "\n画中人物有可能是伊蕾娜（灰色长发、琉璃色眼睛；平时戴黑色三角帽、穿黑色长袍，"
    "但画里也可能穿便服、不戴帽子）。请仔细看：发色、眼睛和整体样子都和她一致，描述就以“伊蕾娜本人的画像”开头；"
    "不像的话就正常描述，不要提伊蕾娜。"
    "拍的是实物（人偶等）时也一样，像她就写“伊蕾娜本人的画像，是一尊人偶，……”。"
    "如果这是手机截图（聊天界面、网页之类），伊蕾娜只出现在背景、头像或贴图里，就不要用“伊蕾娜本人的画像”开头，"
    "照截图来写，背景不用提。"
)
# 11/17 动图：以前只看第一帧，字后来才出现、靠动作表达意思的动图会看漏。现在从动图里均匀取 4 帧拼成 2×2 交给看图模型
# （本机认人、底片比对还是看第一帧）
GIF_HINT = ("\n这是一张动图，按时间顺序取了 4 个画面拼在一起（左上 1 → 右上 2 → 左下 3 → 右下 4）。"
            "当成一张动图来描述：画面里的动作怎么变、中途或最后出现的字都写出来；"
            "不要说成“四格图”“拼图”“四个画面”，直接说动图里发生了什么。")
GIF_FRAMES = 4
GIF_CELL = 512          # 每格长边最多多少像素（拼好最大约 1032×1032）
NOT_SELF_NOTE = "（画的不是你）"
# 10/25：伊蕾娜分数在这条线以上、又没认成她时，算“拿不准”：不说“画的不是你”，也不把别的角色名告诉看图模型。
# 起因是一尊伊蕾娜手办的实拍照只有 0.17，还被认成了别的角色，她就说“这也能叫伊蕾娜小姐”。
# 线上“不是她”的图分数都在 0.05 以下（《识图抽查-1022》），所以这条线基本只影响拿不准的图
UNSURE_MIN = 0.1
UNSURE_NOTE = "（看不太出是不是你）"
# 描述里有这些词，说明画里有人。本机识别认过、结论是“不是伊蕾娜”时，给她补一句，免得她把随便一个画中少女认成自己
_PERSON_RE = re.compile(r"画中|少女|少年|女孩|男孩|女生|男生|人物|女子|男子|姑娘|人偶|玩偶|雕像|立牌")
OTHER_HINT = "\n画中人物是{names}。描述里不要写出这个名字，只写外貌、动作和表情。"

# 模型偶尔还是会写出这些词，统一换成伊蕾娜能理解的说法
_REWRITE = [
    (re.compile(r"(动漫|动画|二次元|番剧)(风格|风|画风)?的?(角色|人物)"), "画中人"),
    (re.compile(r"(动漫|动画|二次元|番剧)(风格|风|画风)"), "插画风格"),
    (re.compile(r"(动漫|动画|二次元|番剧)"), "插画"),
    (re.compile(r"角色"), "人物"),
    # 实物周边：她的世界里没有“手办”，换成人偶、玩偶（10/25 用户定的）
    (re.compile(r"粘土人"), "小人偶"),
    (re.compile(r"手办"), "人偶"),
    (re.compile(r"公仔"), "玩偶"),
    # 11/05：描述会包在「[图片：……]」里，聊天那边按方括号认出描述、算字数时去掉；描述里自己带方括号（截图里的用户名之类）会打乱，换成【】
    (re.compile(r"\["), "【"),
    (re.compile(r"\]"), "】"),
]

# 交给模型前先在本机缩小：DeepSeek 看图时本来也会缩小，原图几 MB 传上去只是更慢、更容易超时
LOOK_MAX_SIDE = 1536          # 长边超过这个就缩小
KEEP_AS_IS_BYTES = 2 * 1024 * 1024   # 本来就不大（长边不超过上面的值、文件不超过 2 MB）的图原样用，动图也保留
MAX_DECODE_PIXELS = 16_000_000       # PNG 等格式要整张解码，超过约 1600 万像素就不看（JPEG 能按比例解码，不受这个限制）
_shrink_lock = threading.Lock()      # 一次只缩一张，免得两张大图同时解码占内存


class TooBig(Exception):
    pass


def shrink(raw: bytes) -> bytes:
    """把图缩成长边 ≤ 1536 的 JPEG（透明背景垫白、动图取第一帧）；本来就小的原样返回。太大没法处理时抛 TooBig"""
    from PIL import Image

    with _shrink_lock:
        img = Image.open(io.BytesIO(raw))            # 只读文件头，还没解码
        w, h = img.size
        if max(w, h) <= LOOK_MAX_SIDE and len(raw) <= KEEP_AS_IS_BYTES:
            return raw
        if img.format == "JPEG":
            img.draft("RGB", (LOOK_MAX_SIDE, LOOK_MAX_SIDE))   # 按缩小的比例解码，再大的 JPEG 也只占很少内存
        elif w * h > MAX_DECODE_PIXELS:
            raise TooBig(f"{w}×{h}")
        img.seek(0)
        img.thumbnail((LOOK_MAX_SIDE, LOOK_MAX_SIDE), Image.LANCZOS)
        if img.mode in ("RGBA", "LA", "P", "PA"):
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img)
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue()


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


def gif_sheet(raw: bytes) -> bytes | None:
    """动图（GIF / 动的 WebP）均匀取 GIF_FRAMES 帧，拼成 2×2 的 JPEG，左上角标上 1～4；不是动图、只有一帧、太大时返回 None"""
    from PIL import Image, ImageDraw, ImageFont

    with _shrink_lock:
        img = Image.open(io.BytesIO(raw))
        if img.format not in ("GIF", "WEBP") or not getattr(img, "is_animated", False):
            return None
        w, h = img.size
        if w * h > MAX_DECODE_PIXELS:
            return None
        n = getattr(img, "n_frames", 1)
        if n < 2:
            return None
        k = min(GIF_FRAMES, n)
        idx = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
        frames = []
        for i in idx:
            img.seek(i)
            f = img.convert("RGBA")
            bg = Image.new("RGBA", f.size, (255, 255, 255, 255))
            f = Image.alpha_composite(bg, f).convert("RGB")
            f.thumbnail((GIF_CELL, GIF_CELL), Image.LANCZOS)
            frames.append(f)
    cw, ch = max(f.width for f in frames), max(f.height for f in frames)
    cols = 2 if len(frames) > 1 else 1
    rows = (len(frames) + cols - 1) // cols
    gap = 8
    sheet = Image.new("RGB", (cols * cw + (cols - 1) * gap, rows * ch + (rows - 1) * gap), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=max(14, cw // 14))
    except TypeError:            # 老版本 Pillow 的默认字体不能调大小
        font = ImageFont.load_default()
    for j, f in enumerate(frames):
        x, y = (j % cols) * (cw + gap), (j // cols) * (ch + gap)
        sheet.paste(f, (x + (cw - f.width) // 2, y + (ch - f.height) // 2))
        r = max(10, cw // 20)
        draw.ellipse((x + 4, y + 4, x + 4 + 2 * r, y + 4 + 2 * r), fill=(40, 40, 40))
        draw.text((x + 4 + r, y + 4 + r), str(j + 1), fill=(255, 255, 255), font=font, anchor="mm")
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _is_tall(raw: bytes) -> bool:
    """长比宽 ≥ TALL_RATIO（只读文件头，不解码）"""
    try:
        from PIL import Image
        w, h = Image.open(io.BytesIO(raw)).size
        return h >= w * TALL_RATIO
    except Exception:  # noqa: BLE001
        return False


def _md5_key(raw: bytes) -> str:
    """手写描述按图的内容存的那份记录用的键"""
    return "md5:" + hashlib.md5(raw).hexdigest()


def _clip_desc(desc: str) -> str:
    """超过 DESC_MAX 时在上限以内最后一个句读处截断，免得半个词或半句引号留在里面"""
    if len(desc) <= DESC_MAX:
        return desc
    head = desc[:DESC_MAX]
    cut = max(head.rfind(c) for c in "。！？；，")
    return (head[:cut] if cut >= DESC_MAX // 2 else head) + "……"


def sanitize(desc: str) -> str:
    for pat, rep in _REWRITE:
        desc = pat.sub(rep, desc)
    return desc


class Vision:
    def __init__(self, client, model: str, cache_file: Path, *, tagger=None, maybe: float = 0.35,
                 max_bytes: int = 20 * 1024 * 1024, max_tokens: int = 300, timeout: float = 30.0):
        self.client = client
        self.maybe = maybe          # 伊蕾娜置信度在 maybe～门槛之间：让 deepseek-flash 再确认一次
        self.model = model
        self.cache_file = cache_file
        self.tagger = tagger
        self.max_bytes = max_bytes
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._raw: OrderedDict[str, bytes] = OrderedDict()   # 最近下载过的几张图，免得刚识别完角色又重新下载
        self._sheets: dict[str, bytes] = {}                   # 动图拼好的 4 帧（和 _raw 同进同出）
        self.library_dirs = [_BOT_DIR / d for d in SELF_LIBRARY_DIRS]   # 底片比对用的图库（测试里可以换）
        self._library = None
        self._cache: dict[str, dict] = {}
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("_v") == CACHE_VERSION:
                self._cache = data.get("items", {})
            else:
                logger.info("识图：旧版缓存作废（以前的描述认不出人物），图片会重新看一遍")
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # 文件坏了（比如写到一半断电）：改名留底，别悄悄清空，里面可能有 /认图 的手动纠正
            bad = cache_file.with_name(f"{cache_file.stem}.bad-{time.strftime('%Y%m%d-%H%M%S')}{cache_file.suffix}")
            try:
                cache_file.replace(bad)
                logger.warning(f"识图：缓存文件读不了（{e}），已改名为 {bad.name} 留底，这次从空缓存开始")
            except OSError:
                logger.warning(f"识图：缓存文件读不了（{e}），这次从空缓存开始")

    def _touch(self, key: str) -> None:
        """用到一次就挪到最后，截断时最后才被丢掉"""
        if key and key in self._cache:
            self._cache[key] = self._cache.pop(key)

    def _trim(self) -> None:
        if len(self._cache) <= CACHE_MAX:
            return
        items = list(self._cache.items())
        pinned = {k for k, v in items if "o" in v or "dm" in v}     # 管理员指定过、手写过描述的不丢
        room = max(0, CACHE_MAX - len(pinned))
        others = [k for k, _ in items if k not in pinned]
        keep = set(others[len(others) - room:]) if room else set()
        self._cache = {k: v for k, v in items if k in pinned or k in keep}

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._trim()
        # 先写临时文件再替换：写到一半崩溃，原文件也还是完整的
        tmp = self.cache_file.with_name(self.cache_file.name + ".tmp")
        tmp.write_text(json.dumps({"_v": CACHE_VERSION, "items": self._cache}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cache_file)

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

    @staticmethod
    def _is_unsure(entry: dict | None) -> bool:
        """有点像伊蕾娜、但没认成她（管理员没指定过）：分数 ≥ UNSURE_MIN"""
        return bool(entry) and "o" not in entry and not Vision.is_self(entry) and entry.get("s", 0) >= UNSURE_MIN

    def _stale(self, entry: dict) -> bool:
        """10/25 以前看的“拿不准”的图，描述是带着别的角色名写的（比如那尊伊蕾娜手办被写成了别人）：重新看一次。
        看过就记 u=1，不会反复看"""
        return self._is_unsure(entry) and not entry.get("u") and any(t not in KNOWN for t in entry.get("c", []))

    def _is_maybe(self, entry: dict | None) -> bool:
        return bool(entry) and "o" not in entry and not self.is_self(entry) and entry.get("s", 0) >= self.maybe

    @staticmethod
    def cache_key(data: dict) -> str:
        return str(data.get("file_unique") or data.get("file") or data.get("url") or "")

    # ------------------------------------------------------------ 下载
    def _download_sync(self, url: str) -> bytes | None:
        # 只用 Python 自带的 urllib，不依赖额外的库；最多读 max_bytes+1 字节，超了就停，不会把大文件整个读进内存
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = r.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise TooBig(f"超过 {self.max_bytes // 1024 // 1024} MB")
        return data

    def _mark_big(self, key: str, why: str) -> None:
        """太大没法看的图记下来，下次别再下载一遍（说说配图每轮都会再查）"""
        logger.info(f"识图：图片太大（{why}），跳过")
        if key:
            entry = self._cache.setdefault(key, {})
            entry["big"] = 1
            self._save()

    async def _get_raw(self, data: dict, key: str) -> bytes | None:
        """下载并缩小好的图（交给模型和本机认人的都是这份）"""
        if key and key in self._raw:
            return self._raw[key]
        if key and self._cache.get(key, {}).get("big"):
            return None
        url = data.get("url") or (data.get("file") if str(data.get("file", "")).startswith("http") else None)
        if not url:
            return None
        try:
            raw = await asyncio.to_thread(self._download_sync, url)
        except TooBig as e:
            self._mark_big(key, str(e))
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"识图：图片下载失败：{e}")
            return None
        sheet = None
        if raw and _mime(raw) in ("image/gif", "image/webp"):
            try:
                sheet = await asyncio.to_thread(gif_sheet, raw)
            except Exception as e:  # noqa: BLE001   拼不出来就照旧只看第一帧
                logger.warning(f"识图：动图取帧失败（{e}），只看第一帧")
        if raw and _mime(raw):
            size0 = len(raw)
            try:
                raw = await asyncio.to_thread(shrink, raw)
            except TooBig as e:
                self._mark_big(key, str(e))
                return None
            except Exception as e:  # noqa: BLE001
                logger.warning(f"识图：图片缩小失败（{e}），这张不看")
                return None
            if len(raw) != size0:
                logger.debug(f"识图：缩小后交给模型（{size0 // 1024} KB → {len(raw) // 1024} KB）")
        if raw and key:
            self._raw[key] = raw
            if sheet:
                self._sheets[key] = sheet
            while len(self._raw) > 8:
                old, _ = self._raw.popitem(last=False)
                self._sheets.pop(old, None)
        return raw

    # ------------------------------------------------------------ 认人
    async def _characters(self, key: str, raw: bytes) -> list[str] | None:
        """返回认出的角色标签；角色识别没准备好时返回 None"""
        r = await self.tagger.detect_full(raw) if self.tagging else None
        if r is None:
            return None
        found, self_score = r
        tags = [t for t, _ in found]
        if key and self_score >= 0:       # 11/17 顺便读出的表情，描述时告诉看图模型（tagger.EXPR_TAGS）
            expr = getattr(self.tagger, "expressions", lambda _r: None)(raw)
            if expr is not None:
                self._cache.setdefault(key, {})["e"] = expr
        if found:
            logger.info("角色识别：" + "、".join(f"{t}（{p:.2f}）" for t, p in found))
        if key and self_score < 0:          # 图太大没识别：记下来，别每次都重新下载，也不给她加“画的不是你”
            entry = self._cache.setdefault(key, {})
            entry["c"], entry["t"], entry["x"] = [], 1, 1
            return tags
        if SELF_TAG in tags and _is_tall(raw):
            tags = [t for t in tags if t != SELF_TAG]
            logger.info(f"角色识别：长条图（多半是截图），认出的伊蕾娜（{self_score:.2f}）可能只是背景或头像，交给看图模型再确认")
        lib_hit = None
        if SELF_TAG not in tags:
            lib_hit = await self._library_match(raw)
            if lib_hit:
                tags = [SELF_TAG] + tags
                logger.info(f"底片比对：和她图库里的 {lib_hit[0]} 是同一张图（指纹差 {lib_hit[1]}/{lib_hit[2]} 位），算她")
        if SELF_TAG not in tags and self_score >= 0.1:
            how = "交给看图模型再确认" if self_score >= self.maybe else "当作拿不准（不说“画的不是你”）"
            logger.info(f"角色识别：像伊蕾娜但没过门槛（{self_score:.2f}，门槛 {self.tagger.threshold}），{how}")
        if key:
            entry = self._cache.setdefault(key, {})
            entry["c"], entry["t"], entry["s"] = tags, 1, round(self_score, 3)
            if lib_hit:
                entry["lib"] = lib_hit[0]
            else:
                entry.pop("lib", None)
        return tags

    def _expr_hint(self, entry: dict | None, raw: bytes, gif: bool) -> str:
        """11/17 本机识别读出的表情特征 → 给看图模型的一句提示；没有、或者是长条截图就不加"""
        if not EXPR_ON:
            return ""
        expr = entry.get("e") if entry else None
        if expr is None and self.tagging:
            expr = getattr(self.tagger, "expressions", lambda _r: None)(raw)
        if not expr or _is_tall(raw):
            return ""
        h = EXPR_HINT.format(where="动图第一个画面的" if gif else "", words="、".join(expr))
        if gif:
            return h + "后面几个画面的表情可能会变，照画面写。"
        if not any(w in expr for w in ("眼泪", "哭", "流泪", "睁着眼哭")):
            h += EXPR_NO_TEAR
        if "吐舌" not in expr:
            h += EXPR_NO_TONGUE
        return h

    async def _library_match(self, raw: bytes):
        """底片比对：和她图库里哪张是同一张图，返回 (文件, pHash 差, dHash 差)；对不上 / 出错返回 None"""
        if not self.library_dirs:
            return None
        try:
            if self._library is None:
                from .selfmatch import Library
                self._library = Library(self.library_dirs, self.cache_file.with_name("self_hashes.json"), log=logger.info)
            return await asyncio.to_thread(self._library.match, raw)
        except Exception as e:  # noqa: BLE001   比对出错不能影响认人
            logger.warning(f"底片比对：出错，这张跳过：{e}")
            return None

    async def shows_self(self, data: dict) -> bool:
        """这张图画的是不是伊蕾娜。只用本机的角色识别，不调用 DeepSeek；识别没准备好就当“不是”"""
        if not self.tagging:
            return False
        key = self.cache_key(data)
        self._touch(key)
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
    async def image_part(self, data: dict) -> dict | None:
        """11/06 聊天模型直接看图（方案 A，《方案-聊天模型直接看图-1106》）：给一张图（OneBot image 段的 data），
        返回能直接放进聊天消息的图片部分 {"type": "image_url", ...}。用的是看图时已经缩好的那份（有就不重新下载）；
        太大、下载失败、格式不认识时返回 None，这张就只给描述。不调模型、不改缓存"""
        key = self.cache_key(data)
        raw = await self._get_raw(data, key)
        if key and key in self._sheets:            # 动图给拼好的 4 帧
            raw = self._sheets[key]
        mime = _mime(raw) if raw else None
        if not mime:
            return None
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}}

    async def image_parts(self, datas: list[dict], limit: int = 2) -> list[dict]:
        """几张图依次取 image_part，最多 limit 张，取不到的跳过"""
        out = []
        for d in datas:
            if len(out) >= limit:
                break
            try:
                part = await self.image_part(d)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"识图：给聊天模型带原图失败，这张只给描述：{e}")
                part = None
            if part:
                out.append(part)
        return out

    async def describe(self, data: dict) -> str | None:
        """data 是 OneBot image 段的 data（含 url / file）。返回一句描述；失败返回 None。
        本机认过、确定画的不是伊蕾娜、画里又有人时，末尾加「（画的不是你）」；
        伊蕾娜分数 ≥ UNSURE_MIN、拿不准时改加「（看不太出是不是你）」"""
        desc = await self._describe(data)
        if not desc or SELF_NAME in desc:
            return desc
        key = self.cache_key(data)
        entry = self._cache.get(key) if key else None
        judged = bool(entry) and ("o" in entry or (entry.get("t") and not entry.get("x")))
        if judged and not self.is_self(entry) and _PERSON_RE.search(desc):
            return desc + (UNSURE_NOTE if self._is_unsure(entry) else NOT_SELF_NOTE)
        return desc

    async def _describe(self, data: dict) -> str | None:
        key = self.cache_key(data)
        self._touch(key)
        entry = self._cache.get(key) if key else None
        if entry and entry.get("dm"):               # 11/17 管理员用 /认图 描述 手写过：直接用，不再让模型看
            return self._manual(entry)
        # 缓存里有描述，并且（已经认过人，或者现在也没法认人）就直接用
        if entry and entry.get("d") and (entry.get("t") or not self.tagging) and not self._stale(entry):
            h = entry.get("h")
            if not h and self._any_manual():     # 手写过描述以后，以前看过的图也要按内容对一次（每张只对一次）
                raw = await self._get_raw(data, key)
                if raw:
                    h = entry["h"] = _md5_key(raw)
                    self._save()
            hand = self._cache.get(h) if h else None
            if hand and hand.get("dm"):
                entry["dm"] = hand["dm"]
                self._save()
                return self._manual(entry)
            return entry["d"]
        raw = await self._get_raw(data, key)
        if not raw:
            return entry.get("d") if entry else None   # 链接过期了：有旧描述就先用旧的
        mime = _mime(raw)
        if not mime:
            logger.info("识图：不认识的图片格式，跳过")
            return None
        h = _md5_key(raw)
        hand = self._cache.get(h)      # 同一张图换了编号发来（别人转发、她自己甩的）：按图的内容找手写描述
        if hand and hand.get("dm"):
            if key:
                self._cache.setdefault(key, {}).update(dm=hand["dm"], h=h)
                self._save()
                return self._manual(self._cache[key])
            return hand["dm"]

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
        if others and not sure and self._is_unsure(entry):
            logger.info(f"识图：有点像伊蕾娜（{entry.get('s', 0):.2f}），不把别的角色名（{'、'.join(others)}）告诉看图模型")
            others = []
        if others:
            prompt += OTHER_HINT.format(names="、".join(others))
        sheet = self._sheets.get(key) if key else None
        prompt += self._expr_hint(entry, raw, bool(sheet))
        if sheet:
            prompt += GIF_HINT
            raw, mime = sheet, "image/jpeg"

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
            budget.track(resp, "vision")                 # 算到正在回的这个人 / 这个群头上
            desc = _clip_desc((resp.choices[0].message.content or "").strip().replace("\n", " "))
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
                entry["h"] = h
                entry.setdefault("t", 1 if tags is not None else 0)
                if tags is not None:
                    entry["c"], entry["t"] = tags, 1
                if maybe:
                    entry["m"] = confirmed
                if self._is_unsure(entry):
                    entry["u"] = 1
                self._save()
            logger.info(f"识图：{desc}" + ("（看图模型确认是伊蕾娜）" if confirmed else ""))
        return desc or None

    # ------------------------------------------------------------ 管理员纠正：/认图
    def info(self, data: dict) -> dict:
        key = self.cache_key(data)
        return dict(self._cache.get(key, {})) if key else {}

    def _any_manual(self) -> bool:
        return any(k.startswith("md5:") for k in self._cache)

    def _manual(self, entry: dict) -> str:
        d = entry["dm"]
        return d if (SELF_NAME in d or not self.is_self(entry)) else f"伊蕾娜本人的画像，{d}"

    async def set_manual(self, data: dict, text: str | None) -> bool:
        """11/17 管理员手写这张图的描述（G5 无奈脸被看成笑、G20 笑着含泪被看成得意这类，模型改不过来的个别图）。
        以后看到这张图直接用手写的，不再调看图模型；None 表示删掉手写的、恢复自动。
        同时按图的内容（下载缩小后的 md5）记一份：别人转发、换了图片编号也认得出是这张"""
        key = self.cache_key(data)
        if not key:
            return False
        entry = self._cache.setdefault(key, {})
        try:
            raw = await self._get_raw(data, key)
        except Exception:  # noqa: BLE001
            raw = None
        h = _md5_key(raw) if raw else entry.get("h")
        if text:
            entry["dm"] = sanitize(text.strip())[:DESC_MAX]
            if h:
                entry["h"] = h
                self._cache[h] = {"dm": self._manual(entry)}
        else:
            entry.pop("dm", None)
            if h:
                self._cache.pop(h, None)
        for e in self._cache.values():     # 同一张图以前用别的编号看过的记录也跟着改
            if h and e.get("h") == h and e is not entry:
                if text:
                    e["dm"] = self._cache[h]["dm"]
                else:
                    e.pop("dm", None)
        self._save()
        return True

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
