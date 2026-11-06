"""
把说说配图缩成服务器好处理的尺寸，只传服务器上还没有的（11/05 起：缩小和上传合成一步）。

为什么缩：服务器只有 2G 内存，处理几千×几千像素的大原图（尤其是 PNG）要整张读进内存，会把机器人撑爆、甚至把整台服务器卡死。
缩的规格和 gallery.py 发说说时用的一样（宽 ≤ 2048、高 ≤ 10000、JPEG 质量 95、单张 ≤ 3 MB），所以清晰度和原来直接发一样。

用法：
- 双击 bot\\缩小说说配图.bat：处理 bot\\qzone\\images\\ 里的图
- 把一个文件夹拖到 bat 上：处理那个文件夹里的图

每次：
1. 先问服务器图库里已经有哪些图（ssh 列一下文件名，服务器地址在 D:\\QQBot\\server.txt），已经有的跳过，不再缩、不再传。
   问不到服务器（没联网、server.txt 不在）时，就全部缩好放着，不上传，你自己传。
2. 内容一模一样、只是文件名不同的图，只处理第一张。
3. 缩好的新图放在 D:\\QQBot\\待上传配图\\（每次先清空），然后自动 scp 传到服务器图库。
4. 传完在 QQ 里发 /说说 图库，让她马上处理新图（不发也行，下次发说说前会自动处理）。

- 本来就够小的 JPEG（宽 ≤ 2048、高 ≤ 10000、≤ 3 MB）原样复制，文件名不变。
- 其余的转成 JPEG，文件名改成 “原名.jpg”（比如 a.png → a.jpg），服务器上认的也是这个名字。
- 同名的图你换了内容，服务器上那张不会自动换掉（按文件名判断）；要换就先改个文件名。
- 只想缩、不想传：命令行加 --no-upload。
"""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

BOT = Path(__file__).resolve().parents[1]
ROOT = BOT.parent
SRC_DEFAULT = BOT / "qzone" / "images"
OUT = ROOT / "待上传配图"
SERVER_FILE = ROOT / "server.txt"
REMOTE_DIR = "QQBot/bot/qzone/images"

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


def out_name(f: Path, w: int, h: int, fmt: str | None, size: int) -> str:
    """缩完叫什么：够小的 JPEG 原名，其余“原名.jpg”"""
    if fmt == "JPEG" and w <= MAX_W and h <= MAX_H and size <= MAX_BYTES:
        return f.name
    return f.stem + ".jpg"


def server_addr() -> str | None:
    try:
        line = SERVER_FILE.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return line or None


def server_names(server: str) -> set[str] | None:
    """服务器图库里已有的文件名；问不到返回 None"""
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", server, f"ls -1 {REMOTE_DIR}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"问不到服务器（{e}）")
        return None
    if r.returncode != 0:
        print(f"问不到服务器：{(r.stderr or r.stdout).strip()[:200]}")
        return None
    return {x.strip() for x in r.stdout.splitlines() if x.strip()}


def upload(server: str, files: list[Path]) -> bool:
    print(f"\n上传 {len(files)} 张到服务器……")
    r = subprocess.run(["scp", *[str(f) for f in files], f"{server}:{REMOTE_DIR}/"])
    return r.returncode == 0


def main() -> None:
    args = [a for a in sys.argv[1:] if a.strip()]
    no_upload = "--no-upload" in args
    args = [a for a in args if a != "--no-upload"]
    src = Path(args[0]) if args else SRC_DEFAULT
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

    server = None if no_upload else server_addr()
    on_server: set[str] | None = None
    if server:
        print("先看看服务器图库里已经有哪些图……")
        on_server = server_names(server)
        if on_server is not None:
            print(f"服务器上已有 {len(on_server)} 张")
    elif not no_upload:
        print(f"找不到 {SERVER_FILE}（里面写一行 admin@服务器IP），这次只缩不传")

    print(f"来源：{src}（{len(files)} 张）")
    print(f"输出：{OUT}")
    print()
    copied = converted = failed = skipped = dup = 0
    total_in = total_out = 0
    names: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for f in files:
        size_in = f.stat().st_size
        try:
            digest = hashlib.md5(f.read_bytes()).hexdigest()
            if digest in hashes:                       # 内容一模一样的图，换了个名字
                dup += 1
                print(f"  {f.name[:42]:<44}和 {hashes[digest]} 是同一张，跳过")
                continue
            hashes[digest] = f.name
            with Image.open(f) as im:
                w, h = im.size
                fmt = im.format
                target = out_name(f, w, h, fmt, size_in)
                if on_server is not None and target in on_server:
                    skipped += 1                       # 服务器上已经有了：不缩不传
                    continue
                total_in += size_in
                if target == f.name:
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
                    out = OUT / target
                    if out.name in names:                      # a.png 和 a.webp 都要变成 a.jpg：后来的加上原扩展名区分
                        out = OUT / (f.stem + "_" + f.suffix.lstrip(".").lower() + ".jpg")
                        if on_server is not None and out.name in on_server:
                            skipped += 1
                            continue
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
    print(f"完成：新图 {copied + converted} 张（原样复制 {copied}、缩小/转换 {converted}），"
          f"服务器上已有跳过 {skipped} 张，重复跳过 {dup} 张，失败 {failed} 张")
    if copied + converted:
        print(f"总大小：{total_in / 1024 / 1024:.0f} MB → {total_out / 1024 / 1024:.0f} MB")
    renamed = [(n, o) for n, o in names.items() if n != o]
    if renamed:
        print(f"其中 {len(renamed)} 张改了文件名（转成了 JPEG）：")
        for n, o in renamed[:50]:
            print(f"    {o}  →  {n}")
    new = sorted(OUT / n for n in names)
    if not new:
        print("\n没有要传的新图。")
        return
    if on_server is None:
        print(f"\n没有上传。缩好的图在 {OUT}，可以自己 scp 到服务器的 {REMOTE_DIR}/")
        return
    if upload(server, new):
        print("上传完了。在 QQ 里私聊她发 /说说 图库，让她马上处理新图。")
    else:
        print(f"！！上传没成功，缩好的图还在 {OUT}，请把这个窗口截图发给空间 agent")


if __name__ == "__main__":
    main()
