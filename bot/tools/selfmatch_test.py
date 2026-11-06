"""识图“底片比对”离线测试（11/06，识图 agent）

底片比对（plugins\\roleplay_chat\\selfmatch.py）：别人发来的图如果和她自己表情库 / 说说图库里某张是同一张图，就算她。
这个测试不调模型、不花钱、不碰 data\\（指纹算在临时文件夹里），只看三件事：
1. 她库里的图经过 QQ 那样的处理（缩小、压缩、裁一点边、加白边）以后，还能不能对上
2. 库里没有的图（别的角色、复杂图、实物照片、聊天截图，以及 tools\\底片比对对照图\\ 里你另放的图）会不会被误认成她
3. 把整个图库算一遍指纹要多久、占多少内存（服务器只有 2G）
结果写到 docs\\识图底片比对测试\\底片比对_日期_时间.md。用法：双击 bot\\识图底片比对测试.bat
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
LIB_DIRS = [BOT / "data" / "stickers", BOT / "qzone" / "images"]
NEG_DIRS = ["tools/识图对照图", "tools/识图复杂图", "tools/识图实物照片", "tools/识图截图", "tools/底片比对对照图"]
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def load_selfmatch():
    spec = importlib.util.spec_from_file_location("selfmatch", BOT / "plugins" / "roleplay_chat" / "selfmatch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def variants(path: Path) -> dict[str, bytes]:
    """模拟别人转发：缩小、压缩、裁边、加白边"""
    from PIL import Image

    def enc(img, q):
        b = io.BytesIO()
        img.convert("RGB").save(b, "JPEG", quality=q)
        return b.getvalue()

    im = Image.open(path)
    w, h = im.size
    if im.format == "JPEG":
        im.draft("RGB", (1600, 1600))
    elif w * h > 16_000_000:
        return {}
    im.seek(0)
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    im.thumbnail((1600, 1600))
    w, h = im.size
    out = {"缩一半+压缩": enc(im.resize((max(1, w // 2), max(1, h // 2))), 70)}
    s = min(1.0, 240 / max(w, h))
    out["缩到很小+重压缩"] = enc(im.resize((max(1, int(w * s)), max(1, int(h * s)))), 50)
    for pct in (0.02, 0.03):
        c = int(min(w, h) * pct)
        out[f"四边各裁 {int(pct * 100)}%"] = enc(im.crop((c, c, w - c, h - c)), 80)
    b = int(min(w, h) * 0.05)
    bi = Image.new("RGB", (w + 2 * b, h + 2 * b), "white")
    bi.paste(im, (b, b))
    out["加 5% 白边"] = enc(bi, 80)
    return out


def main() -> None:
    S = load_selfmatch()
    tmp = Path(tempfile.mkdtemp())
    lib = S.Library(LIB_DIRS, tmp / "self_hashes.json")
    tracemalloc.start()
    t0 = time.time()
    lib.refresh(force=True)
    build_s = time.time() - t0
    peak = tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()
    files = [f for d in LIB_DIRS if d.is_dir() for f in sorted(d.rglob("*")) if f.suffix.lower() in IMG_EXT]
    print(f"图库 {len(files)} 个文件，算出指纹 {len(lib)} 张，用时 {build_s:.1f} 秒，峰值内存 {peak:.0f} MB")

    kinds: dict[str, list[int]] = {}
    misses: list[tuple[str, str]] = []
    t0 = time.time()
    n_match = 0
    for f in files:
        try:
            vs = variants(f)
        except Exception as e:  # noqa: BLE001
            print(f"跳过 {f.name}：{e}")
            continue
        for k, raw in vs.items():
            m = lib.match(raw)
            n_match += 1
            kinds.setdefault(k, [0, 0])
            kinds[k][1] += 1
            if m:
                kinds[k][0] += 1
            else:
                misses.append((f.name, k))
    per_ms = (time.time() - t0) / max(1, n_match) * 1000

    negs = [f for d in NEG_DIRS if (BOT / d).is_dir() for f in sorted((BOT / d).iterdir())
            if f.suffix.lower() in IMG_EXT and f.stem != "头像"]
    neg_rows, wrong = [], []
    for f in negs:
        raw = f.read_bytes()
        fp = S.fingerprint(raw)
        m = lib.match(raw)
        if fp is None:
            neg_rows.append((f, None, None))
            continue
        best = min(((S._ham(fp[0], int(it["p"], 16)), S._ham(fp[1], int(it["d"], 16)), k)
                    for k, it in lib._items.items()), default=None)
        neg_rows.append((f, best, m))
        if m:
            wrong.append((f, m))

    lines = [
        f"# 底片比对离线测试（{datetime.now():%m-%d %H:%M}）",
        "",
        f"> 门槛：pHash ≤ {S.PH_MAX} 位、dHash ≤ {S.DH_MAX} 位、长宽比差 ≤ {int(S.RATIO_TOL * 100)}%、平均颜色差 ≤ {S.COLOR_MAX:g}。不调模型、不碰 data。",
        "",
        "## 汇总",
        "",
        f"- 图库：{len(files)} 个文件，算出指纹 {len(lib)} 张；全部算一遍 {build_s:.1f} 秒，峰值内存约 {peak:.0f} MB；之后每比一张图约 {per_ms:.0f} 毫秒",
        "- 她库里的图处理过以后还能对上的（对上库里任何一张都算她）：",
    ]
    for k, (a, b) in kinds.items():
        lines.append(f"  - {k}：{a}/{b}")
    lines.append(f"- **库里没有的图被误认成她的：{len(wrong)}/{len(negs)}**" + ("（" + "、".join(f.name for f, _ in wrong) + "）" if wrong else ""))
    lines += ["", "## 库里没有的图：离库里最近的一张差多少", "",
              f"差得越多越安全；门槛是 pHash {S.PH_MAX}、dHash {S.DH_MAX}。", "",
              "| 文件夹 | 文件 | pHash 差 | dHash 差 | 最近的库图 | 对上了吗 |", "|---|---|---|---|---|---|"]
    for f, best, m in neg_rows:
        if best is None:
            lines.append(f"| {f.parent.name} | `{f.name[:30]}` | — | — | 打不开 | — |")
        else:
            lines.append(f"| {f.parent.name} | `{f.name[:30]}` | {best[0]} | {best[1]} | `{best[2][:34]}` | {'❌ 对上了' if m else '没有'} |")
    lines += ["", "## 她的图没对上的", ""]
    lines += [f"- `{n}`：{k}" for n, k in misses] or ["（没有）"]
    out_dir = BOT.parent / "docs" / "识图底片比对测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"底片比对_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln in lines[6:6 + len(kinds) + 2]:
        print(ln)
    print(f"结果在 {out}")


if __name__ == "__main__":
    sys.exit(main())
