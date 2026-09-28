"""
把说说配图缩成服务器好处理的尺寸，再传到服务器。

为什么：服务器只有 2G 内存，处理几千×几千像素的大原图（尤其是 PNG）要整张读进内存，会把机器人撑爆、甚至把整台服务器卡死。
缩的规格和 gallery.py 发说说时用的一样（宽 ≤ 2048、高 ≤ 10000、JPEG 质量 95、单张 ≤ 3 MB），所以清晰度和原来直接发一样，
只是服务器不用再去读大原图。

用法：
- 双击 bot\\缩小说说配图.bat：处理 bot\\qzone\\images\\ 里的图（你电脑上的图库原图）
- 把一个文件夹拖到 bat 上：处理那个文件夹里的图（比如新下载的图）
结果放在 D:\\QQBot\\待上传配图\\（每次先清空），原图一张都不改。
- 本来就够小的 JPEG（宽 ≤ 2048、高 ≤ 10000、≤ 3 MB）原样复制，文件名不变，服务器上也不用重新处理。
- 其余的转成 JPEG，文件名改成 “原名.jpg”（比如 a.png → a.jpg）。
"""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageOps

BOT = Path(__file__).resolve().parents[1]
ROOT = BOT.parent
SRC_DEFAULT = BOT / "qzone" / "images"
OUT = ROOT / "待上传配图"

MAX_W, MAX_H = 2048, 10000      # 和 gallery.py 的 UPLOAD_MAX_W / UPLOAD_MAX_H 一致
QUALITY = 95
MAX_BYTES = 3 * 1024 * 1024
EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

Image.MAX_IMAGE_PIXELS = None    # 原图可能很大，电脑上内存够，不限制


def to_jpeg(img: Image.Image) -> bytes:
    for q in (QUALITY, 90, 85, 78, 70):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= MAX_BYTES or q == 70:
            return buf.getvalue()
    return buf.getvalue()


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else SRC_DEFAULT
    if not src.is_dir():
        print(f"找不到文件夹：{src}")
        return
    files = sorted(f for f in src.iterdir() if f.is_file() and f.suffix.lower() in EXTS)
    if not files:
        print(f"{src} 里没有图片")
        return
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    print(f"来源：{src}（{len(files)} 张）")
    print(f"输出：{OUT}")
    print()
    copied = converted = failed = 0
    total_in = total_out = 0
    names: dict[str, str] = {}
    for f in files:
        size_in = f.stat().st_size
        total_in += size_in
        try:
            with Image.open(f) as im:
                w, h = im.size
                fmt = im.format
                small_jpeg = fmt == "JPEG" and w <= MAX_W and h <= MAX_H and size_in <= MAX_BYTES
                if small_jpeg:
                    out = OUT / f.name
                    shutil.copyfile(f, out)
                    copied += 1
                    note = "够小，原样复制"
                else:
                    im.seek(0)
                    img = ImageOps.exif_transpose(im)          # 手机拍的照片按方向转正
                    img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
                    if img.mode in ("RGBA", "LA", "P"):
                        img = img.convert("RGBA")
                        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                        img = Image.alpha_composite(bg, img)
                    img = img.convert("RGB")
                    data = to_jpeg(img)
                    out = OUT / (f.stem + ".jpg")
                    if out.name in names:                      # a.png 和 a.webp 都要变成 a.jpg：后来的加上原扩展名区分
                        out = OUT / (f.stem + "_" + f.suffix.lstrip(".").lower() + ".jpg")
                    out.write_bytes(data)
                    converted += 1
                    note = f"{w}×{h} → {img.width}×{img.height}"
            names[out.name] = f.name
            size_out = out.stat().st_size
            total_out += size_out
            print(f"  {f.name[:42]:<44}{size_in / 1024 / 1024:>6.1f} MB → {size_out / 1024 / 1024:>5.1f} MB  {note}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  {f.name[:42]:<44}处理失败：{e}")

    print()
    print(f"完成：原样复制 {copied} 张，缩小/转换 {converted} 张，失败 {failed} 张")
    print(f"总大小：{total_in / 1024 / 1024:.0f} MB → {total_out / 1024 / 1024:.0f} MB")
    renamed = [(n, o) for n, o in names.items() if n != o]
    if renamed:
        print(f"其中 {len(renamed)} 张改了文件名（转成了 JPEG），服务器上要把对应的原图删掉或挪走，否则会重复：")
        for n, o in renamed[:50]:
            print(f"    {o}  →  {n}")


if __name__ == "__main__":
    main()
