"""
本地动漫角色识别：WD14 Tagger（SmilingWolf 的 v3 系列，ONNX 格式，CPU 就能跑）

- 这类模型是按 Danbooru 图站的标签训练的，角色标签里有 elaina_(majo_no_tabitabi)，
  同人图、表情包都认得出来。只在本机运行，不花钱，一张图大约 1 秒。
- 第一次启动时在后台自动下载模型（约 470 MB），先试 hf-mirror.com（国内能直连），再试 huggingface.co。
  下载期间和下载失败时，识图照常工作，只是认不出角色。
- 也可以手动下载 model.onnx 和 selected_tags.csv，放进 data/models/<模型名>/ 文件夹。
- 依赖 onnxruntime、numpy、Pillow；没装的话只会在日志里提示一次，不影响机器人其他功能。
"""
from __future__ import annotations

import asyncio
import csv
import io
import threading
import time
import urllib.request
from pathlib import Path

from nonebot import logger

FILES = ("selected_tags.csv", "model.onnx")
SIZE = 448          # v3 系列的输入尺寸（加载模型后会按模型实际的输入尺寸为准）
CHARACTER = 4       # selected_tags.csv 里 category=4 表示角色标签
SELF_TAG = "elaina_(majo_no_tabitabi)"
MAX_PIXELS = 16_000_000    # 超过约 1600 万像素（比如 4000×4000）的图不识别：解码一张就要几百 MB 内存
MIN_MODEL_BYTES = 100 * 1024 * 1024   # v3 系列模型都在 300 MB 以上，比这小肯定是没下完整


class ImageTooLarge(Exception):
    pass


class TagTableMismatch(Exception):
    pass


class Tagger:
    def __init__(self, model_dir: Path, repo: str, mirrors: list[str], *, threshold: float = 0.75,
                 threads: int = 2, auto_download: bool = True):
        self.dir = model_dir / repo.split("/")[-1]
        self.repo = repo
        self.mirrors = [m.rstrip("/") for m in mirrors if m]
        self.threshold = threshold
        self.threads = threads
        self.auto_download = auto_download
        self._session = None
        self._input = ""
        self._size = SIZE
        self._chars: list[tuple[int, str]] = []    # (输出下标, 标签名)，只留角色标签
        self._self_idx: int | None = None          # 伊蕾娜这个标签在输出里的下标
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self.status = "未加载"

    # ------------------------------------------------------------ 加载
    @property
    def ready(self) -> bool:
        return self._session is not None

    def start(self) -> None:
        """机器人启动时调用：在后台准备模型（需要时先下载），不阻塞启动"""
        if self._task is None:
            self._task = asyncio.create_task(asyncio.to_thread(self._prepare))

    def _prepare(self) -> None:
        try:
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as e:
            self.status = "缺少依赖"
            logger.warning(f"角色识别：缺少依赖（{e.name}），这次不启用。关掉机器人窗口，重新双击 start.bat 会自动安装")
            return
        missing = [f for f in FILES if not (self.dir / f).exists()]
        if missing:
            if not self.auto_download:
                self.status = "没有模型"
                logger.warning(f"角色识别：{self.dir} 里没有 {'、'.join(missing)}，这次不启用")
                return
            if not self._download(missing):
                self.status = "下载失败"
                return
        try:
            self._load()
        except Exception as e:  # noqa: BLE001
            self.status = "加载失败"
            self._handle_load_error(e)

    def _handle_load_error(self, e: Exception) -> None:
        """只有确认文件坏了才删（删了下次启动会重新下载）；内存不够等其他原因保留文件，免得反复重下 470 MB"""
        model = self.dir / "model.onnx"
        msg = str(e)
        if isinstance(e, TagTableMismatch):
            (self.dir / "selected_tags.csv").unlink(missing_ok=True)
            logger.warning(f"角色识别：{msg}。标签表已删除，下次启动会重新下载")
            return
        size = model.stat().st_size if model.exists() else 0
        expected = self._expected_size()
        broken = ("protobuf" in msg.lower() or size < MIN_MODEL_BYTES
                  or (expected is not None and size != expected))
        if broken:
            model.unlink(missing_ok=True)
            logger.warning(f"角色识别：模型文件损坏或没下完整（{msg[:120]}），已删除，下次启动会重新下载")
        else:
            logger.warning(f"角色识别：模型加载失败（{msg[:200]}）。文件看起来是完整的，所以没删；"
                           f"如果是内存不够，关掉一些程序后重启机器人，或者设 VISION_TAGGER=false 先关掉角色识别")

    def _expected_size(self) -> int | None:
        """自动下载时记下的文件大小（手动下载的没有这个记录）"""
        try:
            return int((self.dir / "model.onnx.size").read_text().strip())
        except (OSError, ValueError):
            return None

    def _download(self, names: list[str]) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            ok = False
            for base in self.mirrors:
                url = f"{base}/{self.repo}/resolve/main/{name}"
                try:
                    self._fetch(url, self.dir / name)
                    ok = True
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"角色识别：从 {base} 下载 {name} 失败：{e}")
            if not ok:
                logger.warning(f"角色识别：{name} 下载失败，这次不启用；下次启动会接着下载。"
                               f"也可以手动下载后放进 {self.dir}")
                return False
        return True

    def _fetch(self, url: str, dest: Path) -> None:
        """下载到 .part 文件，支持断点续传；下完再改名"""
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "Mozilla/5.0"}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            if have and r.status != 206:     # 服务器不支持续传，从头下
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0)
            if total > 50 * 1024 * 1024:
                logger.info(f"角色识别：开始下载 {dest.name}（约 {total // 1024 // 1024} MB），不影响机器人正常聊天")
            step = max(total // 10, 1)
            next_log = (have // step + 1) * step
            with open(part, "ab" if have else "wb") as f:
                done = have
                while chunk := r.read(1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total > 50 * 1024 * 1024 and done >= next_log:
                        logger.info(f"角色识别：{dest.name} 已下载 {done * 100 // total}%")
                        next_log += step
        if total and part.stat().st_size < total:
            raise OSError("没下完整")
        part.replace(dest)
        if total:
            dest.with_name(dest.name + ".size").write_text(str(total))

    def _load(self) -> None:
        import onnxruntime as ort

        with open(self.dir / "selected_tags.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self._chars = [(i, r["name"]) for i, r in enumerate(rows) if r.get("category") == str(CHARACTER)]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, self.threads)   # 少占几个核，别让 QQ 卡
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False    # 不留内存池：识别完把临时内存还给系统（小内存服务器上很重要）
        t0 = time.monotonic()
        sess = ort.InferenceSession(str(self.dir / "model.onnx"), sess_options=opts,
                                    providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        self._input = inp.name
        if isinstance(inp.shape[1], int):
            self._size = inp.shape[1]
        n_out = sess.get_outputs()[0].shape[-1]
        if isinstance(n_out, int) and n_out != len(rows):
            raise TagTableMismatch(f"标签表（{len(rows)} 个）和模型输出（{n_out} 个）对不上")
        self._session = sess
        self.status = "已启用"
        self._self_idx = next((i for i, n in self._chars if n == SELF_TAG), None)
        has_elaina = self._self_idx is not None
        logger.info(f"角色识别：已启用（{self.repo}，{len(self._chars)} 个角色标签，"
                    f"{'认得' if has_elaina else '不认得'}伊蕾娜，加载用了 {time.monotonic() - t0:.1f} 秒）")

    # ------------------------------------------------------------ 识别
    def _preprocess(self, raw: bytes):
        """先查尺寸、先缩小，再垫白底和铺正方形：全程只在小图上做格式转换，大图也不会占太多内存"""
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(raw))       # 这一步只读文件头，还没解码
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise ImageTooLarge(f"{w}×{h}")
        img.seek(0)                             # 动图只看第一帧
        size = self._size
        if img.format == "JPEG":
            img.draft("RGB", (size, size))      # JPEG 直接按缩小的比例解码，省内存
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")           # 调色板、灰度等先转一下（像素数已经限制过）
        w, h = img.size
        scale = size / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BICUBIC)
        if img.mode == "RGBA":
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img)     # 透明背景垫成白色
        img = img.convert("RGB")
        square = Image.new("RGB", (size, size), (255, 255, 255))
        square.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
        arr = np.asarray(square, dtype=np.float32)[:, :, ::-1]   # RGB → BGR，数值保持 0～255
        return np.ascontiguousarray(arr[None, ...])

    def _detect_sync(self, raw: bytes) -> tuple[list[tuple[str, float]], float]:
        with self._lock:                     # 一次只处理一张（预处理也在锁里），避免同时占满 CPU 和内存
            x = self._preprocess(raw)
            probs = self._session.run(None, {self._input: x})[0][0]
        found = [(name, float(probs[i])) for i, name in self._chars if probs[i] >= self.threshold]
        self_score = float(probs[self._self_idx]) if self._self_idx is not None else 0.0
        return sorted(found, key=lambda t: -t[1])[:3], self_score

    async def detect_full(self, raw: bytes) -> tuple[list[tuple[str, float]], float] | None:
        """返回 ([(过了门槛的角色标签, 置信度)], 伊蕾娜的置信度)；模型没准备好或出错时返回 None；
        图太大没识别时返回 ([], -1.0)"""
        if not self.ready:
            return None
        try:
            return await asyncio.to_thread(self._detect_sync, raw)
        except ImageTooLarge as e:
            logger.info(f"角色识别：图太大（{e}），跳过")
            return [], -1.0          # 置信度 -1 表示“太大没识别”，和“识别过、不是她”区分开
        except Exception as e:  # noqa: BLE001
            logger.warning(f"角色识别：这张图识别失败：{e}")
            return None

    async def detect(self, raw: bytes) -> list[tuple[str, float]] | None:
        """返回 [(角色标签, 置信度)]；模型没准备好或出错时返回 None（和“没认出人”区分开）"""
        r = await self.detect_full(raw)
        return None if r is None else r[0]
