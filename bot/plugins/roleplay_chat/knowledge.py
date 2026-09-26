"""
小说知识检索（纯 Python，无额外依赖）

- 数据源 1：章节摘要（knowledge/summaries/volNN.md，按 ### 小节切分）
- 数据源 2：小说原文（novel/*.txt，按“第N卷 章名”切章，再切成 ~450 字的段落）
- 检索：汉字二元组 + BM25，倒排表用 array 存，内存约几十 MB
- 首次启动时建索引并缓存到 data/novel_index.pkl；源文件变化会自动重建
"""
from __future__ import annotations

import html
import math
import pickle
import re
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

from nonebot import logger

# ------------------------------------------------------------------ 文本工具
_CN_NUM = "零一二三四五六七八九十"
_VOL_HEAD = re.compile(r"^\s*第([0-9一二三四五六七八九十]+)卷\s*(.*)$")
_NOISE = re.compile(
    r"(^|\n)[^\n]*(转自|录入|图源|扫图|翻译：|台版|网译版|轻之国度|深夜读书会|天使动漫)[^\n]*"
    r"|（?插图[pP]?\d*）?|^\s*\d{3}\s*$",
    re.M,
)
_KEEP = re.compile(r"[一-鿿㐀-䶿A-Za-z0-9]")


def _cn2int(t: str) -> int:
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if t.startswith("十"):
        return 10 + _CN_NUM.index(t[1])
    if "十" in t:
        a, b = t.split("十")
        return _CN_NUM.index(a) * 10 + (_CN_NUM.index(b) if b else 0)
    return _CN_NUM.index(t)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


INDEX_VERSION = 5
# 原文是台版转简体，残留了一些异体字；检索时统一，免得“抽烟”搜不到“抽菸”
_VARIANTS = str.maketrans({"菸": "烟", "著": "着", "姊": "姐", "瞭": "了", "乾": "干", "麵": "面", "裡": "里", "妳": "你"})


def bigrams(text: str) -> list[str]:
    """把文本转成二元组（只保留汉字/字母/数字，跨标点不连）"""
    text = text.translate(_VARIANTS)
    out = []
    for seg in re.split(r"[^一-鿿㐀-䶿A-Za-z0-9]+", text):
        if len(seg) == 1:
            out.append(seg)
        for i in range(len(seg) - 1):
            out.append(seg[i : i + 2])
    return out


@dataclass
class Doc:
    kind: str      # "character" | "summary" | "text"
    label: str     # 例如 “第4卷·第七章 忘却纪行的艾姆妮西亚”
    content: str


# ------------------------------------------------------------------ 语料加载
def load_summaries(folder: Path) -> list[Doc]:
    docs: list[Doc] = []
    for f in sorted(folder.glob("vol*.md")):
        s = f.read_text(encoding="utf-8")
        m = re.match(r"vol(\d+)", f.stem)
        vol = int(m.group(1)) if m else 0
        # 卷概要 / 角色 / 设定 / 性格 这些二级标题各算一条
        for sec in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", s, re.S | re.M):
            title, body = sec.group(1).strip(), sec.group(2).strip()
            if title == "章节摘要":
                for ch in re.finditer(r"^### (.+?)\n(.*?)(?=^### |\Z)", body, re.S | re.M):
                    if "后记" in ch.group(1):   # 作者后记不算伊蕾娜的回忆
                        continue
                    docs.append(Doc("summary", f"第{vol}卷·{ch.group(1).strip()}", ch.group(2).strip()))
            else:
                docs.append(Doc("summary", f"第{vol}卷·{title}", body))
    return docs


def load_characters(path: Path) -> list[Doc]:
    """角色档案：每个 “## 名字” 一条"""
    if not path.exists():
        return []
    s = path.read_text(encoding="utf-8")
    docs = []
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", s, re.S | re.M):
        docs.append(Doc("character", f"角色资料·{m.group(1).strip()}", m.group(2).strip()))
    return docs


def character_names(label: str) -> list[str]:
    """从 “角色资料·凯伊奈（萨莉欧）” 这类标题里拆出可匹配的名字"""
    title = label.split("·", 1)[-1]
    if title == "伊蕾娜本人":
        # 她的名字几乎每句话都会出现，不能每次都带档案；只在提到她的魔女名时带上（避免把称号来历说错）
        return ["灰之魔女"]
    names = re.split(r"[（）()与、/]", title)
    return [n.strip() for n in names if len(n.strip()) >= 2 and n.strip() not in ("人形", "母亲", "伊蕾娜的母亲")]


def load_novel(path: Path, chunk_chars: int = 450) -> list[Doc]:
    text = _read_text(path)
    lines = text.splitlines()
    heads = []
    for i, line in enumerate(lines):
        m = _VOL_HEAD.match(line)
        if m and len(line.strip()) < 50:
            try:
                heads.append((i, _cn2int(m.group(1)), m.group(2).strip()))
            except ValueError:
                continue
    docs: list[Doc] = []
    for k, (i, vol, title) in enumerate(heads):
        if title in ("插图",) or title.endswith("后记") or "后记" == title:
            continue
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        body = html.unescape("\n".join(lines[i + 1 : end]))
        body = _NOISE.sub("\n", body)
        paras = [p.strip() for p in body.splitlines() if p.strip()]
        if not paras:
            continue
        label = f"第{vol}卷·{title}"
        buf: list[str] = []
        size = 0
        for p in paras:
            buf.append(p)
            size += len(p)
            if size >= chunk_chars:
                docs.append(Doc("text", label, "\n".join(buf)))
                buf = buf[-1:] if len(buf[-1]) < chunk_chars // 3 else []  # 留一点重叠
                size = sum(len(x) for x in buf)
        if buf and size > 40:
            docs.append(Doc("text", label, "\n".join(buf)))
    return docs


# ------------------------------------------------------------------ BM25 索引
class KnowledgeBase:
    K1 = 1.5
    B = 0.75

    def __init__(self, docs: list[Doc]):
        self.docs = docs
        self.lengths = array("I")
        postings: dict[str, tuple[array, array]] = {}
        for idx, d in enumerate(docs):
            toks = bigrams(d.label + " " + d.content)
            self.lengths.append(len(toks))
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                p = postings.get(t)
                if p is None:
                    p = (array("I"), array("H"))
                    postings[t] = p
                p[0].append(idx)
                p[1].append(min(c, 65535))
        self.postings = postings
        n = len(docs)
        self.avglen = (sum(self.lengths) / n) if n else 1.0
        self.idf = {t: math.log(1 + (n - len(p[0]) + 0.5) / (len(p[0]) + 0.5)) for t, p in postings.items()}
        # 名字 -> 角色档案，用于“话里直接提到某人”时精确带上他的资料
        self.name_index: dict[str, int] = {}
        for idx, d in enumerate(docs):
            if d.kind == "character":
                for nm in character_names(d.label):
                    self.name_index.setdefault(nm, idx)

    def characters_in(self, text: str) -> list[Doc]:
        """找出文本里提到的角色（按出现先后，去重）"""
        hits = []
        for nm, idx in self.name_index.items():
            pos = text.find(nm)
            if pos >= 0:
                hits.append((pos, -len(nm), idx))
        seen, out = set(), []
        for _, _, idx in sorted(hits):
            if idx not in seen:
                seen.add(idx)
                out.append(self.docs[idx])
        return out

    def search(self, query: str, kind: str | None = None, top_k: int = 3) -> list[tuple[float, Doc]]:
        q = set(bigrams(query))
        n = len(self.docs)
        scores: dict[int, float] = {}
        for t in q:
            p = self.postings.get(t)
            if p is None or len(p[0]) > n * 0.25:   # 太常见的二元组（“什么”“我们”）不计
                continue
            idf = self.idf[t]
            ids, tfs = p
            for i, tf in zip(ids, tfs):
                if kind and self.docs[i].kind != kind:
                    continue
                dl = self.lengths[i]
                s = idf * tf * (self.K1 + 1) / (tf + self.K1 * (1 - self.B + self.B * dl / self.avglen))
                scores[i] = scores.get(i, 0.0) + s
        best = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [(s, self.docs[i]) for i, s in best]

    def max_idf(self, query: str) -> float:
        """查询里最“稀有”的二元组的 idf，用来判断是否在问具体的人/地/事"""
        vals = [self.idf.get(t, 0.0) for t in set(bigrams(query))]
        return max(vals) if vals else 0.0


def build_or_load(summary_dir: Path, novel_dir: Path, cache: Path, characters_file: Path | None = None) -> KnowledgeBase | None:
    char_files = [characters_file] if characters_file and characters_file.exists() else []
    sources = char_files + sorted(summary_dir.glob("vol*.md")) + sorted(novel_dir.glob("*.txt"))
    if not sources:
        logger.warning(f"知识库：没找到摘要或小说文件（{summary_dir}，{novel_dir}），已跳过")
        return None
    sig = [INDEX_VERSION] + [(str(p), p.stat().st_size, int(p.stat().st_mtime)) for p in sources]
    if cache.exists():
        try:
            with cache.open("rb") as f:
                saved_sig, kb = pickle.load(f)
            if saved_sig == sig:
                logger.info(f"知识库：已从缓存加载（{len(kb.docs)} 条）")
                return kb
        except Exception as e:  # noqa: BLE001
            logger.warning(f"知识库缓存读取失败，重建：{e}")
    t0 = time.time()
    docs = load_characters(char_files[0]) if char_files else []
    n_char = len(docs)
    docs += load_summaries(summary_dir)
    n_sum = len(docs) - n_char
    for txt in sorted(novel_dir.glob("*.txt")):
        docs += load_novel(txt)
    kb = KnowledgeBase(docs)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as f:
        pickle.dump((sig, kb), f, protocol=pickle.HIGHEST_PROTOCOL)
    logger.info(
        f"知识库：索引建立完成，角色 {n_char} 人、摘要 {n_sum} 条、原文片段 {len(docs) - n_sum - n_char} 条，"
        f"用时 {time.time() - t0:.1f}s"
    )
    return kb
