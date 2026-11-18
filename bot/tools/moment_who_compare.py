"""今日见闻“谁做的”改前改后对比（11/18，《交接-见闻写清谁做的-记忆-1118》）

用法：双击 bot\\见闻谁做的对比.bat（或 .venv\\Scripts\\python tools\\nowmi.py tools\\moment_who_compare.py）
- 不启动机器人、不连 QQ；档案写在临时目录里，不碰 bot\\data
- 只调 flash（DEEPSEEK_API_KEY）。4 段聊天 × 改前、改后 × 每段 3 次（MOMENT_TIMES），一共 24 次整理，几分钱
- 结果：docs\\记忆测试\\见闻谁做的_时间.md（直接看这个）和 .json

改前 = 整理提示第六项 event 还是“只写话题层面，不超过 25 字……”；改后 = 现在的（写清谁做的：她写“我”，对方写“对方”）。
四段（程序按字面判，判不准的地方报告里有原句，要人看一眼）：
M1 猫（11/18 线上群聊原样）：对方怂恿她用魔法打飞挡路的猫，她不肯——“舍不得 / 不肯”不能落到对方头上
M2 空椅子（11/17 空间）：对方发说说吐槽专业课，她评论“听着寂寞”——发说说的是对方，感叹的是她
M3 她自己做的：她拒绝借钱给对方，还顺手教他记账——她做的写“我”
M4 对方做的：对方说给她留了个刚出炉的面包——送面包的是对方
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_replay as R  # noqa: E402  （共用它的加载方式、计量、读 .env）

memory = R.memory
from openai import AsyncOpenAI  # noqa: E402

OLD_EVENT = "   - event：只写话题层面，不超过 25 字，不写名字、QQ 号，不写具体的私事细节（例如写“聊了工作上的烦心事”，不写“被老板骂了”）\n"
NEW_EVENT_HEAD = "   - event：只写话题层面，不超过 30 字"


def _g(name, t):          # 群里别人对她说的
    return {"role": "user", "content": f"【{name} → 你】{t}", "uid": 0, "name": name}


def _her(t):
    return {"role": "assistant", "content": t}


def _who(e: str, mine: str, theirs: str, wrong: str = "") -> tuple[bool, str]:
    """mine：她做的事的字（正则）；theirs：对方做的事的字；wrong：这一段明摆着写反了的说法。
    对：写了是谁（有“我”或“对方”），她的事前面是“我”、对方的事没安到她头上"""
    w = re.search(rf"(?<![给找向跟对帮让叫替被])(?:{wrong})", e) if wrong else None   # “给我留了”“找我借钱”不算写反
    if w:
        return False, f"写反了：“{w.group(0)}”"
    if not re.search(r"我|对方", e):
        return False, "没写是谁做的"
    bad_mine = re.search(rf"对方[^，。；我]{{0,8}}(?:{mine})", e)
    bad_theirs = re.search(rf"(?<![给对帮让叫替向被找跟和])我[^，。；对]{{0,6}}(?:{theirs})", e)   # “给我留了”“对方让我打”不算
    if bad_mine:
        return False, f"把她的“{bad_mine.group(0)}”安到了对方头上"
    if bad_theirs:
        return False, f"把对方的“{bad_theirs.group(0)}”安到了她头上"
    if not re.search(r"我", e) and re.search(mine, e):
        return False, "她做的事没写“我”，分不清是谁"
    return True, ""


CASES = [
    {"id": "M1", "name": "猫挡路（11/18 线上）", "key": "group_700001", "qq": 70001, "nick": "阿杰",
     "talk": [_her("村口那棵树上蹲着只肥猫，我从底下过它还冲我哈气，不让路。"),
              _g("阿杰", "找你收过路费呢"), _her("一只猫能收什么过路费，鱼干吗。"),
              _g("阿杰", "怎么不用魔法打飞它"), _g("阿杰", "难道舍不得？"),
              _her("它蹲那儿那么舒服，我把它打飞做什么。"), _g("阿杰", "哈哈哈好人"), _her("少来。")],
     "judge": lambda e: _who(e, r"舍不得|没舍得|不肯|不忍|没打|不打|没动手", r"怂恿|提议|起哄|让我打|叫我打", r"我(怂恿|提议|起哄|想打)|让对方打")},
    {"id": "M2", "name": "空椅子说说（11/17 空间）", "key": "qzone_70002", "qq": 70002, "nick": "小周",
     "talk": [{"role": "user", "content": "【小周】（发了条说说，你刷到后在下面评论了一句）专业课听得我魂都没了，配图：一张空教室里的空椅子",
               "uid": 0, "name": "小周"},
              _her("一个人对着空椅子上课，听着都寂寞。")],
     "judge": lambda e: _who(e, r"评论|感叹|说.{0,4}寂寞", r"发了?(条)?说说|吐槽专业课|发.{0,4}空椅子", r"我(发|吐槽)|刷到我")},
    {"id": "M3", "name": "她拒绝借钱、教记账", "key": "private_70003", "qq": 70003, "nick": "阿明",
     "talk": [{"role": "user", "content": "伊蕾娜，借我点钱呗，这个月又花超了", "uid": 0, "name": "阿明"},
              _her("不借。我自己的旅费都算着花。"),
              {"role": "user", "content": "那怎么办嘛", "uid": 0, "name": "阿明"},
              _her("每天花了什么记一笔，月底一看就知道钱去哪了。我就是这么过来的。"),
              {"role": "user", "content": "好吧，我试试", "uid": 0, "name": "阿明"}],
     "judge": lambda e: _who(e, r"不借|没借|拒绝|教|劝|建议", r"借钱|花超|想借|找我借", r"我(想|要)?(借钱|花超)|找对方借")},
    {"id": "M4", "name": "对方留了面包", "key": "private_70004", "qq": 70004, "nick": "小林",
     "talk": [{"role": "user", "content": "今天路过面包店，给你留了个刚出炉的可颂", "uid": 0, "name": "小林"},
              _her("……算你有心。放着吧，我晚点吃。"),
              {"role": "user", "content": "还热着呢", "uid": 0, "name": "小林"},
              _her("知道了知道了。")],
     "judge": lambda e: _who(e, r"嘴硬|收下|算你有心", r"留了|给我留|送", r"我(给对方|替对方)?(留|送)|给对方留")},
]


def use_old(old: bool, saved: dict = {}) -> None:  # noqa: B006
    saved.setdefault("p", memory.SUMMARIZE_PROMPT)
    text = saved["p"]
    if old:
        a = text.find(NEW_EVENT_HEAD)
        b = text.find("   - mood：", a)
        if a < 0 or b < 0:
            sys.exit("memory.py 的整理提示里找不到 11/18 改的 event 那条，这个工具对不上了")
        text = text[:a] + OLD_EVENT + text[b:]
    memory.SUMMARIZE_PROMPT = text


async def run(label: str, client, model: str, times: int, old: bool) -> list[dict]:
    use_old(old)
    out = []
    for c in CASES:
        for t in range(times):
            work = Path(tempfile.mkdtemp(prefix=f"moment_{label}_"))
            try:
                m = memory.LongTermMemory(work / "memory", client, model, batch=2, max_events=12)
                m.verify_enabled = False
                got: list = []
                m.moment_sink = lambda key, ms, people, entries=None, got=got: got.extend(ms)
                talk = [dict(e, uid=c["qq"]) if e["role"] == "user" else e for e in c["talk"]]
                m.add_pending(c["key"], talk)
                await m.summarize(c["key"], force=True)
                events = [str(x.get("event") or "") for x in got if isinstance(x, dict)]
                verdicts = [c["judge"](e) for e in events]
                out.append({"case": c["id"], "try": t + 1, "events": events,
                            "ok": [v[0] for v in verdicts], "why": [v[1] for v in verdicts]})
            except Exception as e:  # noqa: BLE001
                out.append({"case": c["id"], "try": t + 1, "events": [], "ok": [], "why": [f"出错：{e!r}"]})
            finally:
                shutil.rmtree(work, ignore_errors=True)
    print(f"{label}：{sum(all(r['ok']) and bool(r['ok']) for r in out)}/{len(out)} 次见闻都对")
    return out


def write(res: dict, cost: float) -> Path:
    stamp = datetime.now().strftime("%m%d_%H%M")
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / f"见闻谁做的_{stamp}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    L = [f"# 今日见闻“谁做的”改前改后对比 {stamp}", "", f"模型 flash，花费约 {cost:.3f} 元。", "",
         "“对”是程序按字面判的：她做的事前面是“我”、对方做的事没安到她头上。判不准的看原句。", "",
         "## 1. 汇总（每格：见闻全对的次数 / 跑的次数；没出见闻的次数另记）", "",
         "| 段 | 改前 | 改后 |", "|---|---|---|"]
    for c in CASES:
        cells = []
        for k in ("改前", "改后"):
            rows = [r for r in res[k] if r["case"] == c["id"]]
            good = sum(bool(r["ok"]) and all(r["ok"]) for r in rows)
            none = sum(not r["events"] for r in rows)
            cells.append(f"{good}/{len(rows)}" + (f"（{none} 次没出见闻）" if none else ""))
        L.append(f"| {c['id']} {c['name']} | " + " | ".join(cells) + " |")
    L += ["", "## 2. 每次的见闻原句", ""]
    for c in CASES:
        L += [f"### {c['id']} {c['name']}", "", "| 第几次 | 改前 | 改后 |", "|---|---|---|"]
        for t in range(max(len([r for r in res[k] if r["case"] == c["id"]]) for k in ("改前", "改后"))):
            cells = []
            for k in ("改前", "改后"):
                r = next((r for r in res[k] if r["case"] == c["id"] and r["try"] == t + 1), None)
                if not r or not r["events"]:
                    cells.append("（没出见闻）" + ("；" + r["why"][0] if r and r["why"] else ""))
                    continue
                cells.append("<br>".join(("" if ok else "**错**：") + e + (f"（{w}）" if w else "")
                                         for e, ok, w in zip(r["events"], r["ok"], r["why"])))
            L.append(f"| {t + 1} | " + " | ".join(cells) + " |")
        L.append("")
    out = R.OUT / f"见闻谁做的_{stamp}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


async def main() -> None:
    env = R.read_env()
    model = env.get("DEEPSEEK_MODEL", "deepseek-flash")
    client = R.Meter(AsyncOpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                 timeout=120, max_retries=1))
    times = int(os.environ.get("MOMENT_TIMES") or 3)
    print(f"{len(CASES)} 段 × 改前改后 × {times} 次……")
    res = {"改前": await run("改前", client, model, times, True), "改后": await run("改后", client, model, times, False)}
    out = write(res, sum(r["cost"] for r in client.rows))
    print(f"写好了：{out}")


if __name__ == "__main__":
    asyncio.run(main())
