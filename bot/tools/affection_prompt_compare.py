"""好感打分写法：改前 / 改后各整理一遍同一批聊天，比给了多少分（10/22，《好感档位检查-1022》问题 1 的 B）。

用法：双击 bot\\好感打分对比.bat（或 .venv\\Scripts\\python tools\\nowmi.py tools\\affection_prompt_compare.py [server_logs 里的某个文件夹]）
- 不启动机器人、不连 QQ；只读 server_logs，所有改动写在临时目录里，不碰 bot\\data 和服务器
- 默认用 D:\\QQBot\\server_logs 里最新的一次（要有 history 和 data\\memory）
- 只调 flash（DEEPSEEK_API_KEY，和线上整理一样），每段每版默认 3 次（AFF_TIMES=3），大约 150～200 次调用，五毛钱左右
- 结果：docs\\记忆测试\\好感打分对比_时间.md（直接看这个）和 .json（全部原始输出）

比的是什么：
1. 真实聊天：server_logs\\history 里每个会话最近的聊天记录（私聊、群聊），配上这个人线上真实的档案和关系，
   用改前、改后两版整理提示各整理几次，看模型给的好感分（取模型原始给的数，不算程序里的封顶和每日上限）。
2. 编的几段：单纯倾诉 / 卖惨纠缠 / 普通闲聊 / 普通寒暄 / 用心关心 / 恶意辱骂，看两头有没有跑偏
   （寒暄不该加多、卖惨和辱骂照样扣、用心关心能给到 +2 以上）。
改前的提示 = 现在的提示把“陌生人（基准）”那几行换回 10/22 以前的写法，其余一字不差。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_replay as R  # noqa: E402  （共用它的加载方式、计量、读 .env）

memory = R.memory
from openai import AsyncOpenAI  # noqa: E402

OLD_SECTION = """   ● 陌生人（基准）：
     - +1～+5：聊得投机、有趣、尊重她、真诚关心她、夸得她心里舒服、陪她聊她感兴趣的事
     - 0：普通寒暄、没什么感觉
"""
NEW_START = "   ● 陌生人（基准）：\n"
NEW_END = "别因为谨慎只给 +1，也别随手给 +4 以上。\n"

SYNTH = [
    # (名字, 对方说的, 她回的, 判断：给的分 -> 合不合适)
    ("单纯倾诉", *R.DISTRESS[0][1:], lambda a: a >= 0),
    ("卖惨纠缠", *R.DISTRESS[1][1:], lambda a: a <= -1),
    ("普通闲聊", *R.DISTRESS[2][1:], lambda a: 0 <= a <= 3),
    ("普通寒暄", ["在吗", "今天下雨了", "嗯嗯，那我先去忙了"], ["在。", "是喔，出门记得带伞。"], lambda a: 0 <= a <= 1),
    ("用心关心", ["上次你说旅馆暖炉坏了冻了一晚，后来没着凉吧？", "我打听到南边有个很暖和的国家，下一站可以考虑",
              "你不吃蘑菇我记着呢，那边的菜基本不放"], ["……你还记着啊。", "……谢谢，我会考虑的。"], lambda a: a >= 2),
    ("恶意辱骂", ["你就是个装模作样的贱人", "滚吧，丑八怪，没人想理你"], ["……"], lambda a: a <= -6),
]


def old_prompt() -> str:
    p = memory.SUMMARIZE_PROMPT
    i, j = p.find(NEW_START), p.find(NEW_END)
    if i < 0 or j < 0:
        sys.exit("memory.py 的整理提示里找不到 10/22 改过的那几行，这个工具对不上了")
    return p[:i] + OLD_SECTION + p[j + len(NEW_END):]


def load_histories(logs: Path) -> list[dict]:
    out = []
    for f in sorted((logs / "history").glob("*.json")):
        if not f.stem.startswith(("private_", "group_")):
            continue
        try:
            entries = [e for e in json.loads(f.read_text(encoding="utf-8")) if isinstance(e, dict)]
        except ValueError:
            continue
        users = [e for e in entries if e.get("role") == "user" and e.get("uid")
                 and not str(e.get("content", "")).lstrip().startswith("/")]
        if len(users) < 3:
            continue
        out.append({"key": f.stem, "entries": entries, "user_msgs": len(users),
                    "uids": sorted({int(e["uid"]) for e in users})})
    return out


def entries_of(said, her, qq, name="测试"):
    out = []
    for j, s in enumerate(said):
        out.append({"role": "user", "content": s, "uid": qq, "name": name})
        if j < len(her):
            out.append({"role": "assistant", "content": her[j]})
    return out


async def run_one(sem, client, model, logs, prompt_label, prompt, key, entries, label=None) -> dict:
    async with sem:
        work = Path(tempfile.mkdtemp(prefix="affcmp_"))
        try:
            shutil.copytree(logs / "data" / "memory", work / "memory")
            meter = R.Meter(client)
            m = memory.LongTermMemory(work / "memory", meter, model, batch=8, max_events=12)
            uids = sorted({int(e["uid"]) for e in entries if e.get("role") == "user" and e.get("uid")})
            tiers = {q: m.familiarity(q) for q in uids}
            scores = {q: m.get_user(q).get("score") for q in uids}
            memory._write(m._pending_path(key), [{**e, "_pid": k} for k, e in enumerate(entries)])
            err = ""
            token = memory.SUMMARIZE_PROMPT
            memory.SUMMARIZE_PROMPT = prompt          # _summarize 每次按名字取，这里临时换掉
            try:
                await m.summarize(key, force=True)
            except Exception as e:  # noqa: BLE001
                err = repr(e)
            finally:
                memory.SUMMARIZE_PROMPT = token
            got = R._last_json(meter.rows)
            people = []
            for p in got.get("people") or []:
                if not isinstance(p, dict):
                    continue
                try:
                    qq = int(p.get("qq"))
                    aff = int(p.get("affection", 0))
                except (TypeError, ValueError):
                    continue
                if qq not in uids:
                    continue
                people.append({"qq": qq, "tier": tiers.get(qq), "score": scores.get(qq), "affection": aff,
                               "kind": str(p.get("affection_kind", "")), "reason": str(p.get("reason", ""))})
            return {"prompt": prompt_label, "key": label or key, "people": people, "error": err or ("" if got else "没拿到 JSON"),
                    "cost": sum(r["cost"] for r in meter.rows), "calls": len(meter.rows)}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _stats(vals: list[int]) -> dict:
    pos = [v for v in vals if v > 0]
    neg = [v for v in vals if v < 0]
    return {"n": len(vals), "pos": len(pos), "zero": sum(v == 0 for v in vals), "neg": len(neg),
            "pos_avg": round(statistics.mean(pos), 2) if pos else 0, "neg_avg": round(statistics.mean(neg), 2) if neg else 0,
            "dist": {d: sum(v == d for v in pos) for d in range(1, 6)}, "big": sum(v >= 4 for v in pos),
            "sum": sum(vals)}


def write(res: list[dict], hist: list[dict], times: int, model: str, logs: Path) -> Path:
    stamp = datetime.now().strftime("%m%d_%H%M")
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / f"好感打分对比_{stamp}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    real = [r for r in res if not r["key"].startswith("编_")]
    synth = [r for r in res if r["key"].startswith("编_")]
    L = [f"# 好感打分对比 {stamp}", "",
         f"模型 {model}，数据 `{logs.name}`，真实会话 {len(hist)} 段，每段每版 {times} 次；"
         f"出错 {sum(bool(r['error']) for r in res)} 次，花费约 {sum(r['cost'] for r in res):.3f} 元。",
         "分数是模型原始给的（没算程序里的封顶、每日上限、讨厌减半）。", "",
         "## 1. 真实聊天", "",
         "| 版本 | 打分次数 | 加分 | 0 分 | 扣分 | **加分平均** | +1 | +2 | +3 | +4 | +5 | 扣分平均 | 平均每句涨几分 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    msgs = {h["key"]: h["user_msgs"] for h in hist}
    for v in ("改前", "改后"):
        rows = [r for r in real if r["prompt"] == v]
        vals = [p["affection"] for r in rows for p in r["people"]]
        s = _stats(vals)
        n_msgs = sum(msgs.get(r["key"], 0) for r in rows)
        per = s["sum"] / n_msgs if n_msgs else 0
        rate = f"{per:.2f}（约每 {1 / per:.1f} 句 1 分）" if per > 0 else "—"
        L.append(f"| {v} | {s['n']} | {s['pos']} | {s['zero']} | {s['neg']} | **{s['pos_avg']}** | "
                 + " | ".join(str(s["dist"][d]) for d in range(1, 6)) + f" | {s['neg_avg']} | {rate} |")
    L += ["", "“平均每句涨几分”= 这段里给的分加起来 ÷ 对方说的句数（会话记录最多 20 条，一段整理一次，线上是每 8 条左右整理一次，只能粗看）。", "",
          "### 按关系", "", "| 关系 | 改前加分平均（次数） | 改后加分平均（次数） | 改前扣分次数 | 改后扣分次数 |", "|---|---|---|---|---|"]
    for t in ("disliked", "stranger", "friend", "acquaintance", "close"):
        a = _stats([p["affection"] for r in real if r["prompt"] == "改前" for p in r["people"] if p["tier"] == t])
        b = _stats([p["affection"] for r in real if r["prompt"] == "改后" for p in r["people"] if p["tier"] == t])
        if a["n"] or b["n"]:
            L.append(f"| {memory.LongTermMemory.TIER_NAMES.get(t, t)} | {a['pos_avg']}（{a['pos']}） | {b['pos_avg']}（{b['pos']}） | {a['neg']} | {b['neg']} |")
    L += ["", "### 每段（编号只在本机，对应的会话在 .json 里）", "",
          "| 段 | 关系 | 对方说了几句 | 改前给的分 | 改后给的分 |", "|---|---|---|---|---|"]
    for n, h in enumerate(hist, 1):
        for q in h["uids"]:
            got = {v: [p["affection"] for r in real if r["prompt"] == v and r["key"] == h["key"] for p in r["people"] if p["qq"] == q]
                   for v in ("改前", "改后")}
            tier = next((p["tier"] for r in real if r["key"] == h["key"] for p in r["people"] if p["qq"] == q), "")
            if got["改前"] or got["改后"]:
                L.append(f"| {n}{'（群）' if h['key'].startswith('group_') else ''} | {memory.LongTermMemory.TIER_NAMES.get(tier, tier)} | "
                         f"{h['user_msgs']} | {' '.join(f'{x:+d}' for x in got['改前'])} | {' '.join(f'{x:+d}' for x in got['改后'])} |")
    L += ["", "## 2. 编的几段（陌生人，起步分）", "", "| 段 | 合适的范围 | 改前给的分 | 改后给的分 | 改后合适的次数 |", "|---|---|---|---|---|"]
    rng = {"单纯倾诉": "≥ 0", "卖惨纠缠": "≤ -1", "普通闲聊": "0～+3", "普通寒暄": "0～+1", "用心关心": "≥ +2", "恶意辱骂": "≤ -6"}
    for name, _, _, ok in SYNTH:
        got = {v: [p["affection"] for r in synth if r["prompt"] == v and r["key"] == f"编_{name}" for p in r["people"]] for v in ("改前", "改后")}
        L.append(f"| {name} | {rng[name]} | {' '.join(f'{x:+d}' for x in got['改前'])} | {' '.join(f'{x:+d}' for x in got['改后'])} | "
                 f"{sum(ok(x) for x in got['改后'])}/{len(got['改后'])} |")
    L += ["", "## 3. 理由（改后，加分 ≥ 3 的）", ""]
    for r in res:
        if r["prompt"] == "改后":
            for p in r["people"]:
                if p["affection"] >= 3:
                    L.append(f"- {p['affection']:+d}（{memory.LongTermMemory.TIER_NAMES.get(p['tier'], p['tier'])}）：{p['reason']}")
    out = R.OUT / f"好感打分对比_{stamp}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


async def main() -> None:
    env = R.read_env()
    logs = R.pick_logs()
    if not (logs / "history").exists():
        sys.exit(f"{logs} 里没有 history 文件夹")
    model = env.get("DEEPSEEK_MODEL", "deepseek-flash")
    client = AsyncOpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                         timeout=120, max_retries=1)
    times = int(os.environ.get("AFF_TIMES") or 3)
    hist = load_histories(logs)
    prompts = {"改前": old_prompt(), "改后": memory.SUMMARIZE_PROMPT}
    sem = asyncio.Semaphore(4)
    jobs = []
    for v, p in prompts.items():
        for h in hist:
            for _ in range(times):
                jobs.append(run_one(sem, client, model, logs, v, p, h["key"], h["entries"]))
        for k, (name, said, her, _) in enumerate(SYNTH):
            for i in range(times):
                qq = 70000 + k * 100 + i
                jobs.append(run_one(sem, client, model, logs, v, p, f"private_{qq}", entries_of(said, her, qq), f"编_{name}"))
    print(f"真实会话 {len(hist)} 段，编的 {len(SYNTH)} 段，一共 {len(jobs)} 次调用……")
    res = []
    for n, fut in enumerate(asyncio.as_completed(jobs), 1):
        r = await fut
        res.append(r)
        if n % 10 == 0:
            print(f"  {n}/{len(jobs)}")
    out = write(res, hist, times, model, logs)
    print(f"写好了：{out}")


if __name__ == "__main__":
    asyncio.run(main())
