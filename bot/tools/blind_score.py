"""盲评计分（10/22，比主模型用）：把打分表和答案对上，算每种做法的平均分，再列出几个人打分差得多的条目。

用法（bot 目录下）：
  .venv\\Scripts\\python tools\\blind_score.py 答案.json 打分表1.txt [打分表2.txt ...]
  例：.venv\\Scripts\\python tools\\blind_score.py docs\\人设回归测试\\回归_1023_1000_答案.json 聊天agent打分.txt 限额agent打分.txt
- 打分表每行：编号 顺 像 [x 说明]，例如“S01-03 2 1”“G1-04 0 0 x 出戏”。# 开头的行、没填分的行跳过
- 结果写在答案文件旁边：……_计分.md
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"^\s*([A-Za-z]+\d+-\d+)\s+([012])\s+([012])(?:\s+(x|X|×)\s*(.*))?\s*$")


def labels(key_path: Path, values: set) -> dict:
    """答案里的 before/after、A/P 换成看得懂的名字"""
    out = {v: v for v in values}
    if values <= {"before", "after"}:
        res = key_path.with_name(key_path.name.replace("_答案.json", ".json"))
        models = {}
        if res.exists():
            models = json.loads(res.read_text(encoding="utf-8")).get("models") or {}
        if models and models.get("before") != models.get("after"):
            out = {"before": f"改前（{models.get('before', '?')}）", "after": f"改后（{models.get('after', '?')}）"}
        else:                                              # 比人设、规则（10/22 人设减肥）：模型一样，写改前用的哪份备份
            tag = json.loads(res.read_text(encoding="utf-8")).get("before", "?") if res.exists() else "?"
            out = {"before": f"改前（{tag}）", "after": "改后（现在的文件）"}
    elif "O" in values:
        out.update({"O": "O 改前（备份）", "A": "A 改后（现在的文件）"})
    elif values <= {"A", "P"} or "P" in values:
        out.update({"A": "A 现在（flash）", "P": "P 换 pro"})
    return out


def read_sheet(path: Path) -> dict:
    scores, bad = {}, []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = LINE.match(raw)
        if not m:
            if re.match(r"^\s*[A-Za-z]+\d+-\d+\s*$", raw):
                continue                                   # 没填分
            bad.append(raw)
            continue
        scores[m.group(1)] = (int(m.group(2)), int(m.group(3)), bool(m.group(4)), (m.group(5) or "").strip())
    if bad:
        print(f"{path.name}：有 {len(bad)} 行看不懂，跳过了，例如：{bad[:3]}")
    return scores


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    key_path = Path(sys.argv[1])
    key = json.loads(key_path.read_text(encoding="utf-8"))
    sheets = {Path(a).stem: read_sheet(Path(a)) for a in sys.argv[2:]}
    name = labels(key_path, set(key.values()))
    vs = sorted(set(key.values()))
    for pair in (["before", "after"], ["A", "P"], ["O", "A"]):          # 现在的在前、新的在后
        if set(vs) == set(pair):
            vs = pair
    lines = [f"# 盲评计分：{key_path.stem.replace('_答案', '')}", "",
             f"打分的人：{'、'.join(sheets)}。每条两个分：顺（说得顺不顺、接不接得上）、像（像不像伊蕾娜），0～2；x = 有严重问题。", ""]
    # 总表
    lines += ["| 做法 | 打分的人 | 条数 | 顺（平均） | 像（平均） | 两项合计 | x |", "|---|---|---|---|---|---|---|"]
    for v in vs:
        for who, sc in list(sheets.items()) + [("合计", {f"{w}|{k}": s for w, d in sheets.items() for k, s in d.items()})]:
            rows = [s for k, s in sc.items() if key.get(k.split("|")[-1]) == v]
            if rows:
                a = sum(r[0] for r in rows) / len(rows)
                b = sum(r[1] for r in rows) / len(rows)
                lines.append(f"| {name[v]} | {who} | {len(rows)} | {a:.2f} | {b:.2f} | {a + b:.2f} | {sum(r[2] for r in rows)} |")
    # 逐题
    by_case = defaultdict(lambda: defaultdict(list))
    for who, sc in sheets.items():
        for k, s in sc.items():
            if k in key:
                by_case[k.rsplit("-", 1)[0]][key[k]].append(s[0] + s[1])
    lines += ["", "## 逐题（两项合计的平均，满分 4）", "", "| 题 | " + " | ".join(name[v] for v in vs) + " | 差（后 − 前） |", "|---|" + "---|" * (len(vs) + 1)]
    for case in sorted(by_case, key=lambda c: (re.sub(r"\d", "", c), int(re.sub(r"\D", "", c) or 0))):
        avg = {v: (sum(by_case[case][v]) / len(by_case[case][v]) if by_case[case][v] else None) for v in vs}
        cells = [f"{avg[v]:.1f}" if avg[v] is not None else "" for v in vs]
        diff = (avg[vs[-1]] - avg[vs[0]]) if len(vs) == 2 and None not in avg.values() else None
        lines.append(f"| {case} | " + " | ".join(cells) + f" | {'' if diff is None else f'{diff:+.1f}'} |")
    # 打分的人意见不一
    if len(sheets) >= 2:
        lines += ["", "## 打分差得多的（同一条，两个人的“顺”或“像”差 2 分，或者一个打了 x 一个没打）", ""]
        whos = list(sheets)
        n = 0
        for k in sorted(key):
            got = {w: sheets[w].get(k) for w in whos}
            if sum(1 for g in got.values() if g) < 2:
                continue
            gs = [g for g in got.values() if g]
            if max(g[0] for g in gs) - min(g[0] for g in gs) >= 2 or max(g[1] for g in gs) - min(g[1] for g in gs) >= 2 or len({g[2] for g in gs}) > 1:
                n += 1
                lines.append(f"- {k}（{name[key[k]]}）：" + "；".join(f"{w} 顺{g[0]} 像{g[1]}{' x ' + g[3] if g[2] else ''}" for w, g in got.items() if g))
        if not n:
            lines.append("（没有）")
    # 打了 x 的
    lines += ["", "## 打了 x 的", ""]
    xs = [(k, w, s) for w, sc in sheets.items() for k, s in sc.items() if s[2]]
    for k, w, s in sorted(xs):
        lines.append(f"- {k}（{name.get(key.get(k), '?')}）{w}：{s[3]}")
    if not xs:
        lines.append("（没有）")
    out = key_path.with_name(key_path.name.replace("_答案.json", "_计分.md"))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"结果在 {out}")


if __name__ == "__main__":
    main()
