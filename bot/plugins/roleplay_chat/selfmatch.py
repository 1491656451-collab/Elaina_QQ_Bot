"""底片比对（11/06，识图 agent）：别人发来的图，是不是她自己表情库 / 说说图库里的某一张

本机认人（tagger.py）认不出动画截图的近脸特写、便服图（《识图抽查-1022》），但这些图很多就在她自己的库里。
用户说过这两个库只放伊蕾娜 / 有伊蕾娜的图，所以只要和库里某张是“同一张图”（被 QQ 压缩、缩小、裁了一点边也算），
就可以直接算她，不会把别人认成她：库里没有别人。

- 先去掉四周纯色的边，再算两个 64 位指纹：pHash（缩成 32×32 灰度做 DCT）和 dHash（缩成 9×8 比相邻像素），
  另记长宽比和 4×4 的平均颜色
- 对得上 = 两个指纹的差异位数都不超过门槛，长宽比、平均颜色也差不多。门槛按《识图底片比对测试报告-1106》定的
- 库里每张图的指纹算一次就存进 data/self_hashes.json（按文件大小和修改时间判断要不要重算），
  每张只占几十字节；新加的图隔一会儿（REFRESH_SECONDS）再遇到时补算
- 只用 Pillow 和 numpy（本机认人本来就要装），不调模型
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

PH_MAX = 10          # pHash 最多差几位算同一张（64 位里）
DH_MAX = 14          # dHash 最多差几位
COLOR_MAX = 30.0     # 4×4 平均颜色每个通道平均最多差多少（0～255）
RATIO_TOL = 0.12     # 长宽比最多差 12%（裁了一点边、垫了白边还能对上；明显不同的构图对不上）
REFRESH_SECONDS = 600
MAX_PIXELS = 16_000_000   # 和 vision.py 一样：超过约 1600 万像素的 PNG 不解码（JPEG 按比例解码，不受限）
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
CACHE_VERSION = 4   # 指纹算法改了就加 1，旧指纹全部重算（11/06 23:1x 修过：JPEG 的颜色缩略图原来是灰的）

_DCT = None


def _dct_matrix(n: int = 32):
    import numpy as np
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    m[0] *= 1 / np.sqrt(2)
    return m


def _trim_box(g):
    """四周纯色的边（截图时多带的白边、黑边）去掉以后的范围，只比画面本身。去完太小、或者没有边就返回 None"""
    from PIL import Image, ImageChops
    corner = g.getpixel((0, 0))
    diff = ImageChops.difference(g, Image.new("L", g.size, corner)).point(lambda v: 255 if v > 24 else 0)
    box = diff.getbbox()
    if not box or box == (0, 0, g.size[0], g.size[1]):
        return None
    l, t, r, b = box
    if (r - l) < g.size[0] * 0.5 or (b - t) < g.size[1] * 0.5:
        return None
    return box


def _bits(arr) -> int:
    v = 0
    for b in arr.flatten():
        v = (v << 1) | int(bool(b))
    return v


def fingerprint(raw: bytes) -> tuple[int, int, float, bytes] | None:
    """(pHash, dHash, 长宽比, 4×4 平均颜色)；打不开或太大返回 None"""
    global _DCT
    import numpy as np
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if img.format == "JPEG":
            img.draft("RGB", (256, 256))   # 按比例解码；要 RGB，不然颜色缩略图会变成灰的
        elif w * h > MAX_PIXELS:
            return None
        img.seek(0)
        if img.mode in ("RGBA", "LA", "P", "PA"):      # 透明背景垫白，和看图时一样
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img)
        rgb = img.convert("RGB")
        box = _trim_box(rgb.convert("L"))
        if box:
            rgb = rgb.crop(box)
        g = rgb.convert("L")
    except Exception:  # noqa: BLE001
        return None
    w, h = g.size
    col = bytes(np.asarray(rgb.resize((4, 4), Image.BOX), dtype=np.uint8).flatten())   # 4×4 的平均颜色，防止构图像、颜色不同的图对上
    if _DCT is None:
        _DCT = _dct_matrix(32)
    a = np.asarray(g.resize((32, 32), Image.LANCZOS), dtype=np.float64)
    d = _DCT @ a @ _DCT.T
    low = d[:8, :8].flatten()
    ph = _bits(low > np.median(low[1:]))
    b = np.asarray(g.resize((9, 8), Image.LANCZOS), dtype=np.float64)
    dh = _bits(b[:, 1:] > b[:, :-1])
    return ph, dh, w / h if h else 1.0, col


def _coldiff(a: bytes, b: bytes) -> float:
    """两张 4×4 颜色缩略图的平均差（0～255）"""
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def _ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Library:
    """她自己的图库（表情库 + 说说图库）的指纹"""

    def __init__(self, dirs: list[Path], cache_file: Path, log=None):
        self.dirs = [Path(d) for d in dirs]
        self.cache_file = cache_file
        self.log = log or (lambda msg: None)
        self._items: dict[str, dict] = {}        # 相对路径 -> {"z": 大小, "m": 修改时间, "p": pHash, "d": dHash, "r": 长宽比}
        self._checked = 0.0
        self._lock = threading.Lock()
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("_v") == CACHE_VERSION:
                self._items = data.get("items", {})
        except (OSError, ValueError, AttributeError):
            pass

    def _key(self, path: Path) -> str:
        for d in self.dirs:
            try:
                return f"{d.name}/{path.relative_to(d).as_posix()}"
            except ValueError:
                continue
        return path.name

    def refresh(self, force: bool = False) -> int:
        """新加 / 改过的图补算指纹，删掉的图去掉；返回这次算了几张。隔 REFRESH_SECONDS 才真的去看一次文件夹"""
        with self._lock:
            if not force and time.time() - self._checked < REFRESH_SECONDS:
                return 0
            self._checked = time.time()
            seen, done = set(), 0
            for d in self.dirs:
                if not d.is_dir():
                    continue
                for f in sorted(d.rglob("*")):
                    if f.suffix.lower() not in IMG_EXT or not f.is_file():
                        continue
                    k = self._key(f)
                    seen.add(k)
                    st = f.stat()
                    old = self._items.get(k)
                    if old and old.get("z") == st.st_size and old.get("m") == int(st.st_mtime):
                        continue
                    try:
                        fp = fingerprint(f.read_bytes())
                    except OSError:
                        fp = None
                    if fp is None:
                        self._items.pop(k, None)
                        continue
                    self._items[k] = {"z": st.st_size, "m": int(st.st_mtime), "p": f"{fp[0]:016x}", "d": f"{fp[1]:016x}",
                                      "r": round(fp[2], 4), "c": fp[3].hex()}
                    done += 1
            gone = [k for k in self._items if k not in seen]
            for k in gone:
                del self._items[k]
            if done or gone:
                try:
                    self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_file.with_name(self.cache_file.name + ".tmp")
                    tmp.write_text(json.dumps({"_v": CACHE_VERSION, "items": self._items}, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, self.cache_file)
                except OSError as e:
                    self.log(f"底片比对：指纹存不下来（{e}），下次启动会重算")
                self.log(f"底片比对：图库 {len(self._items)} 张（新算 {done} 张，去掉 {len(gone)} 张）")
            return done

    def __len__(self) -> int:
        return len(self._items)

    def match(self, raw: bytes) -> tuple[str, int, int] | None:
        """和库里某张是同一张图：返回 (库里的文件, pHash 差几位, dHash 差几位)；对不上返回 None"""
        self.refresh()
        fp = fingerprint(raw)
        if fp is None:
            return None
        ph, dh, r, col = fp
        best = None
        for k, it in list(self._items.items()):
            if abs(it["r"] - r) > RATIO_TOL * max(it["r"], r):
                continue
            a = _ham(ph, int(it["p"], 16))
            if a > PH_MAX:
                continue
            b = _ham(dh, int(it["d"], 16))
            if b > DH_MAX:
                continue
            if _coldiff(col, bytes.fromhex(it["c"])) > COLOR_MAX:
                continue
            if best is None or a + b < best[1] + best[2]:
                best = (k, a, b)
        return best
