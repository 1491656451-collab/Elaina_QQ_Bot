"""
从 Safebooru 抓伊蕾娜的同人图，放进“待选”文件夹给你挑（11/05 空间 agent 写）。

用法：双击 bot\\抓说说配图.bat（一次抓 30 张没抓过的）。
- 图放在 D:\\QQBot\\待选配图\\，文件名 safebooru_编号.jpg/png。你看过以后，把想要的移进 bot\\qzone\\images\\，
  再照常双击“缩小说说配图.bat”、scp 上传、QQ 里发 /说说 图库。不要的直接删掉就行，删了也不会再抓回来。
- 出处（原图链接、Safebooru 页面）记在 D:\\QQBot\\配图出处.json，按文件名查。说说里不写画师。
- 抓过的都记在 D:\\QQBot\\待选配图\\已抓过.json，下次跳过；图库里已经有的（同名）也跳过。

为什么用 Safebooru：站里只有全年龄的图，有公开的 JSON 接口，不用登录；Danbooru 不允许程序抓取（robots.txt），Pixiv 要登录。
只抓：分级 general / safe、长边 ≥ 800 像素、不是漫画 / 四格 / 草图 / 黑白线稿；单人图（solo）排在前面。
标签不一定准，所以先放待选，由你挑过再进图库，不让她直接拿去发。

网络：先看环境变量 HTTPS_PROXY / HTTP_PROXY；没有就试本机 127.0.0.1:7890（你的代理）；都没有就直连。
命令行：python tools\\fetch_gallery.py [张数] [--proxy http://127.0.0.1:7890]
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx

BOT = Path(__file__).resolve().parents[1]
ROOT = BOT.parent
OUT = ROOT / "待选配图"
SEEN_FILE = OUT / "已抓过.json"
CREDIT_FILE = ROOT / "配图出处.json"
GALLERY = BOT / "qzone" / "images"

API = "https://safebooru.org/index.php"
TAG = "elaina_(majo_no_tabitabi)"
UA = "QQBot-gallery/1.0"
PER_PAGE = 100
MAX_PAGES = 10
MIN_SIDE = 800
SKIP_TAGS = {"comic", "4koma", "sketch", "lineart", "monochrome", "greyscale", "text_focus", "text-only_page",
             "photo_(medium)", "cosplay", "real_life", "translation_request", "speech_bubble"}
GOOD_RATINGS = {"general", "safe", "g", "s"}
EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def pick_proxy(arg: str | None) -> str | None:
    if arg:
        return arg
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        with socket.create_connection(("127.0.0.1", 7890), timeout=0.5):
            return "http://127.0.0.1:7890"
    except OSError:
        return None


def existing_names() -> set[str]:
    """图库和待选里已有的图（只看文件名主体，缩小时 a.png 会变成 a.jpg）"""
    names = set()
    for d in (GALLERY, OUT):
        if d.is_dir():
            names |= {f.stem.lower() for f in d.iterdir() if f.is_file()}
    return names


def good(post: dict) -> str:
    """能不能要；能要返回空字符串，不能要返回原因"""
    if str(post.get("rating", "")).lower() not in GOOD_RATINGS:
        return "分级"
    if post.get("status") not in (None, "active"):
        return "状态"
    w, h = int(post.get("width") or 0), int(post.get("height") or 0)
    if max(w, h) < MIN_SIDE:
        return "太小"
    tags = set(str(post.get("tags", "")).split())
    if tags & SKIP_TAGS:
        return "漫画/线稿等"
    if Path(str(post.get("image", ""))).suffix.lower() not in EXTS:
        return "格式"
    return ""


def file_url(post: dict) -> str:
    if post.get("file_url"):
        return str(post["file_url"])
    return f"https://safebooru.org/images/{post['directory']}/{post['image']}"


def fetch(client: httpx.Client, want: int, log=print) -> list[str]:
    seen = _read(SEEN_FILE, {})
    credit = _read(CREDIT_FILE, {})
    have = existing_names()
    cands = []
    for page in range(MAX_PAGES):
        r = client.get(API, params={"page": "dapi", "s": "post", "q": "index", "json": 1,
                                    "limit": PER_PAGE, "pid": page, "tags": TAG})
        r.raise_for_status()
        posts = r.json() if r.text.strip() else []
        if not posts:
            break
        for p in posts:
            name = f"safebooru_{p['id']}"
            if str(p.get("id")) in seen or str(p.get("hash")) in seen.values() or name.lower() in have:
                continue
            if good(p):
                continue
            cands.append(p)
        if len(cands) >= want * 3:
            break
        time.sleep(1)
    # 单人图排前面，其余按新到旧（接口默认就是新到旧）
    cands.sort(key=lambda p: "solo" not in str(p.get("tags", "")).split())
    got = []
    OUT.mkdir(parents=True, exist_ok=True)
    for p in cands:
        if len(got) >= want:
            break
        url = file_url(p)
        name = f"safebooru_{p['id']}{Path(str(p['image'])).suffix.lower()}"
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log(f"  下载失败，跳过：{name}（{e}）")
            continue
        (OUT / name).write_bytes(r.content)
        seen[str(p["id"])] = str(p.get("hash", ""))
        credit[name] = {"原图": p.get("source") or "", "safebooru": f"https://safebooru.org/index.php?page=post&s=view&id={p['id']}",
                        "尺寸": f"{p.get('width')}x{p.get('height')}", "抓取": time.strftime("%Y-%m-%d")}
        got.append(name)
        log(f"  {len(got):>2}. {name}  {p.get('width')}x{p.get('height')}  {'单人' if 'solo' in str(p.get('tags','')).split() else ''}")
        _write(SEEN_FILE, seen)
        _write(CREDIT_FILE, credit)
        time.sleep(1)
    return got


def main() -> None:
    args = [a for a in sys.argv[1:] if a.strip()]
    proxy = None
    if "--proxy" in args:
        i = args.index("--proxy")
        proxy = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    want = int(args[0]) if args and args[0].isdigit() else 30
    proxy = pick_proxy(proxy)
    print(f"从 Safebooru 抓 {want} 张伊蕾娜的图（{'代理 ' + proxy if proxy else '直连'}）……")
    try:
        with httpx.Client(proxy=proxy, headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
            got = fetch(c, want)
    except httpx.HTTPError as e:
        print(f"连不上 Safebooru：{e}\n检查代理有没有开（默认试 127.0.0.1:7890），或者用 --proxy 指定。")
        return
    if not got:
        print("没有新图可抓（能抓的都抓过了，或者都被筛掉了）。")
        return
    print(f"\n抓了 {len(got)} 张，在：{OUT}")
    print("看一遍，把想要的移进 bot\\qzone\\images\\，再双击“缩小说说配图.bat”上传。出处在 配图出处.json。")


if __name__ == "__main__":
    main()
