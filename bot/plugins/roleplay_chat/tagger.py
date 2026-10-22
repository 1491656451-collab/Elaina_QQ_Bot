"""
本地动漫角色识别：WD14 Tagger（SmilingWolf 的 v3 系列，ONNX 格式，CPU 就能跑）

- 这类模型是按 Danbooru 图站的标签训练的，角色标签里有 elaina_(majo_no_tabitabi)，
  同人图、表情包都认得出来。只在本机运行，不花钱，一张图大约 1 秒。
- 第一次启动时在后台自动下载模型（约 470 MB），先试 hf-mirror.com（国内能直连），再试 huggingface.co。
  下载期间和下载失败时，识图照常工作，只是认不出角色。
- 也可以手动下载 model.onnx 和 selected_tags.csv，放进 data/models/<模型名>/ 文件夹。
- 依赖 onnxruntime、numpy、Pillow；没装的话只会在日志里提示一次，不影响机器人其他功能。
- 内存记录（10/23）：每认一张图看一眼机器人占的内存，创了当天新高就在日志里记一行；每天 05:00 再汇总一行
  前一天的识图张数和内存峰值。只在 Linux（服务器）上记，Windows 上什么都不做。见 _MemWatch。
"""
from __future__ import annotations

import asyncio
import csv
import io
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import logger

FILES = ("selected_tags.csv", "model.onnx")
SIZE = 448          # v3 系列的输入尺寸（加载模型后会按模型实际的输入尺寸为准）
CHARACTER = 4       # selected_tags.csv 里 category=4 表示角色标签
SELF_TAG = "elaina_(majo_no_tabitabi)"
MAX_PIXELS = 16_000_000    # 超过约 1600 万像素（比如 4000×4000）的图不识别：解码一张就要几百 MB 内存
MIN_MODEL_BYTES = 100 * 1024 * 1024   # v3 系列模型都在 300 MB 以上，比这小肯定是没下完整


DAY_START_HOUR = 5          # 和花费账本一样，凌晨 5 点算新的一天
MEM_LOG_STEP_MB = 20        # 当天新高比上次记的多出这么多才再记一行，免得刷屏


def _proc_status_mb(field: str) -> int | None:
    """读 /proc/self/status 里的 VmRSS（现在占的物理内存）或 VmHWM（启动以来最高）；不是 Linux 就返回 None"""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def _cgroup_mb() -> dict[str, int | None]:
    """机器人这个服务（systemd 的 cgroup）整体占的内存：当前、本次启动以来峰值、上限。读不到的项是 None"""
    out: dict[str, int | None] = {"current": None, "peak": None, "max": None}
    try:
        with open("/proc/self/cgroup", encoding="ascii") as f:
            line = next((x for x in f if x.startswith("0::")), "")
        base = Path("/sys/fs/cgroup") / line.strip()[3:].lstrip("/")
    except OSError:
        return out
    for key in out:
        try:
            v = (base / f"memory.{key}").read_text().strip()
            out[key] = None if v == "max" else int(v) // (1024 * 1024)
        except (OSError, ValueError):
            pass
    return out


def _day_of(now: datetime) -> str:
    return (now - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


class _MemWatch:
    """识图时的内存记录：每认一张图调一次 sample()；每天 05:00 由 daily_loop() 汇总前一天"""

    def __init__(self):
        self.day = _day_of(datetime.now())
        self.count = 0
        self.high: int | None = None          # 当天识图后看到的进程内存最高值（MB）
        self.logged: int | None = None        # 当天已经记过的最高值

    def _roll(self, now: datetime) -> None:
        d = _day_of(now)
        if d != self.day:
            self.summary()
            self.day, self.count, self.high, self.logged = d, 0, None, None

    def _service_text(self) -> str:
        cg = _cgroup_mb()
        parts = []
        if cg["current"] is not None:
            parts.append(f"服务整体现在 {cg['current']} MB")
        if cg["peak"] is not None:
            parts.append(f"本次启动以来最高 {cg['peak']} MB")
        if cg["max"] is not None:
            parts.append(f"上限 {cg['max']} MB")
        return "，".join(parts)

    def sample(self) -> None:
        rss = _proc_status_mb("VmRSS")
        if rss is None:                       # Windows 等：不记
            return
        self._roll(datetime.now())
        self.count += 1
        if self.high is None or rss > self.high:
            self.high = rss
        if self.logged is None or rss >= self.logged + MEM_LOG_STEP_MB:
            self.logged = rss
            svc = self._service_text()
            logger.info(f"角色识别：内存今天新高，机器人进程 {rss} MB（今天第 {self.count} 张图）"
                        + (f"；{svc}" if svc else ""))

    def summary(self) -> None:
        if _proc_status_mb("VmRSS") is None:
            return
        hwm = _proc_status_mb("VmHWM")
        svc = self._service_text()
        high = f"{self.high} MB" if self.high is not None else "—"
        logger.info(f"角色识别：{self.day} 识图 {self.count} 张，识图时机器人进程最高 {high}"
                    + (f"，进程启动以来最高 {hwm} MB" if hwm is not None else "")
                    + (f"；{svc}" if svc else ""))

    async def daily_loop(self) -> None:
        if _proc_status_mb("VmRSS") is None:
            return
        while True:
            now = datetime.now()
            nxt = now.replace(hour=DAY_START_HOUR, minute=0, second=5, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep((nxt - now).total_seconds())
            try:
                self._roll(datetime.now())
            except Exception as e:  # noqa: BLE001   记录出错不能影响识图
                logger.warning(f"角色识别：内存汇总出错：{e}")


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
        self._mem_task: asyncio.Task | None = None
        self.mem = _MemWatch()
        self.status = "未加载"

    # ------------------------------------------------------------ 加载
    @property
    def ready(self) -> bool:
        return self._session is not None

    def start(self) -> None:
        """机器人启动时调用：在后台准备模型（需要时先下载），不阻塞启动"""
        if self._task is None:
            self._task = asyncio.create_task(asyncio.to_thread(self._prepare))
        if self._mem_task is None:
            self._mem_task = asyncio.create_task(self.mem.daily_loop())

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
            try:
                self.mem.sample()             # 刚认完一张图，看一眼内存
            except Exception as e:  # noqa: BLE001   记录出错不能影响识图
                logger.warning(f"角色识别：内存记录出错：{e}")
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
