"""作息回放（11/13，聊天 agent，《方案-她自己定作息-1113》）：构造几种当天的情况，看她自己定的几点睡、几点起、理由。

题目（每题 SLEEP_SAMPLES 次，默认 10）：
  S1 在野外露宿第 3 天，今天说过在礁石上看浪
  S2 刚到一个镇子住进旅馆，今天说过找了家咖啡厅
  S3 今天赶了一整天路
  S4 什么都没说过（近况空的）
  S5 前两晚都是 1 点睡、8 点半起（连着熬夜），今天在镇上
  S6 镇上今晚有灯会，傍晚说过在广场看人放烟花（看会不会晚睡）
走的是真的 sleep_plan.ask（拼提示、调模型、收进范围），近况和前两晚是编的；计划存在临时目录，不碰 data，不连 QQ。

怎么用：双击 bot\\作息回放.bat。结果在 docs\\作息回放\\作息回放_时间.md 和 .json。
环境变量：REPLAY_BOT = bot 目录；SLEEP_ONLY = 只跑某几题（S1,S5）；SLEEP_SAMPLES = 每题几次；REPLAY_FAKE=1：程序自测，不调模型
"""
from __future__ import annotations

import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import replay_chat as rc

BEIJING = timezone(timedelta(hours=8))
BOT_DIR = Path(os.environ.get("REPLAY_BOT", ".")).resolve()
OUT_DIR = BOT_DIR.parent / "docs" / "作息回放"
ONLY = [x for x in os.environ.get("SLEEP_ONLY", "").split(",") if x]
SAMPLES = int(os.environ.get("SLEEP_SAMPLES") or 10)
FAKE = os.environ.get("REPLAY_FAKE") == "1"

_WILD = {"name": "另一片海边", "kind": "野外", "days_here": 3, "status": "现在"}
_TOWN = {"name": "一座港口小镇", "kind": "城镇", "days_here": 1, "status": "现在"}
CASES = [
    ("S1", "在野外露宿第 3 天，今天说过在礁石上看浪", {"place": _WILD, "fresh": [{"text": "傍晚坐在礁石上看浪一层层退下去", "when": "今天傍晚"},
                                                                       {"text": "早上被海浪声吵醒，坐着看日出", "when": "今天早上"}]}, []),
    ("S2", "刚到一个镇子住进旅馆，今天说过找了家咖啡厅", {"place": _TOWN, "fresh": [{"text": "进城住进一家带浴室的旅馆", "when": "今天下午"},
                                                                         {"text": "找了家咖啡厅喝咖啡欧蕾，看了半天报纸", "when": "今天傍晚"}]}, []),
    ("S3", "今天赶了一整天路", {"place": {"name": "往内陆的路上", "kind": "野外", "days_here": 1, "status": "现在"},
                         "fresh": [{"text": "从早上走到天黑，翻了两座山坡，脚都磨破了", "when": "今天"}]}, []),
    ("S4", "什么都没说过", {"place": {}, "fresh": []}, []),
    ("S5", "前两晚都是 1 点睡、8 点半起，今天在镇上", {"place": _TOWN, "fresh": [{"text": "在镇上的书店翻了一下午书", "when": "今天下午"}]},
     [{"start": 780, "end": 1230, "why": "日记写得停不下来"}, {"start": 780, "end": 1230, "why": "看书看过头了"}]),
    ("S6", "镇上今晚有灯会，傍晚说过在广场看人放烟花", {"place": _TOWN, "fresh": [{"text": "镇上今晚办灯会，傍晚在广场挤着看人放烟花，夜市还没散", "when": "今天傍晚"}]}, []),
]


def _shift_avg(rs):
    xs = [r["shift"] for r in rs if r.get("shift") is not None]
    return f"{sum(xs) / len(xs):+.0f}" if xs else "-"


def _shift_cnt(rs):
    """入睡比平常早 10 分钟以上 / 前后 10 分钟内 / 晚 10 分钟以上"""
    xs = [r["shift"] for r in rs if r.get("shift") is not None]
    return f"{sum(x < -10 for x in xs)}/{sum(-10 <= x <= 10 for x in xs)}/{sum(x > 10 for x in xs)}" if xs else "-"


@pytest.mark.asyncio
async def test_sleep_plan(monkeypatch):
    import plugins.roleplay_chat as p
    import importlib
    sl = importlib.import_module("plugins.roleplay_chat.sleep")
    sp = importlib.import_module("plugins.roleplay_chat.sleep_plan")
    env = rc._env()
    for key, attr, conv in (("DEEPSEEK_MODEL", "deepseek_model", str), ("LLM_TEMPERATURE", "llm_temperature", float)):
        if env.get(key):
            monkeypatch.setattr(p.cfg, attr, conv(env[key]))
    prompts: list[str] = []
    if FAKE:
        async def create(**kw):
            prompts.append(kw["messages"][0]["content"])
            h = random.choice([22, 23, 0, 3])
            return rc._resp(json.dumps({"sleep": f"{h}:{random.randint(0, 59):02d}", "wake": f"{random.choice([6, 7, 8, 11])}:15",
                                        "why": "假模型"}, ensure_ascii=False))
    else:
        real = rc._real_client().chat.completions.create

        async def create(**kw):
            prompts.append(kw["messages"][0]["content"])
            return await real(**kw)
    monkeypatch.setattr(p.core.client.chat.completions, "create", create)
    monkeypatch.setattr(p.cfg, "sleep_enabled", True)
    monkeypatch.setattr(p.cfg, "sleep_by_model", True)
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(sl, "plan_file", lambda: tmp / "sleep_plan.json")
    night = "2026-11-13"
    now_dt = datetime(2026, 11, 13, 20, 0, tzinfo=BEIJING)
    results = []
    for cid, desc, life, prev in CASES:
        if ONLY and cid not in ONLY:
            continue
        monkeypatch.setattr(p.core.self_mem, "life_for_proactive", lambda *a, _l=life, **k: dict(_l, told_elsewhere=[]))
        for k in range(SAMPLES):
            plans = {f"2026-11-{11 + i:02d}": dict(x) for i, x in enumerate(prev)}
            monkeypatch.setattr(sl, "_plans", plans)
            n0 = len(prompts)
            sp._usual.clear()                       # 每次重新抽“今天的平常”，相当于换了一晚
            plan = await sp.ask(night, now_dt)
            row = {"id": cid, "sample": k, "ok": bool(plan)}
            if plan:
                row.update(sleep=sl._hhmm(plan["start"]), wake=sl._hhmm(plan["end"]), hours=round((plan["end"] - plan["start"]) / 60, 1),
                           said=plan["said"], why=plan["why"], clamped=plan["clamped"], usual=plan.get("usual", ""))
                us = sp._usual.get(night)
                if us:
                    row["shift"] = plan["start"] - us[0]      # 比“今天的平常”晚多少分钟（负的是早）
            if k == 0 and len(prompts) > n0:
                row["prompt"] = prompts[n0]
            results.append(row)
            print(cid, k, row.get("usual"), row.get("said"), row.get("sleep"), row.get("wake"), row.get("why"), row.get("clamped"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    (OUT_DIR / f"作息回放_{stamp}.json").write_text(json.dumps({"fake": FAKE, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# 作息回放 {stamp}", "", "> 聊天 agent 的工具 `tools\\sleep_plan_compare.py`（《方案-她自己定作息-1113》）。每题 "
          f"{SAMPLES} 次；“她说”是模型原话里的时间，“定下”是收进范围（睡 22:00～01:30、起 06:00～09:30、6～10 小时）以后的。“今天的平常”是程序每次随机抽的（第 3 轮起），“比平常”是入睡比它晚几分钟（负的是早）。"
          + ("**程序自测（假模型）**" if FAKE else ""), "", "## 汇总", "",
          "| 题 | 情况 | 定成了 | 几点睡（最早～最晚） | 几点起（最早～最晚） | 睡几小时（平均） | 被收过 | 时间全一样 | 比平常（平均分钟，正=晚） | 早/照常/晚 | 过零点 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for cid, desc, _, _ in CASES:
        rs = [r for r in results if r["id"] == cid and r["ok"]]
        allr = [r for r in results if r["id"] == cid]
        if not allr:
            continue
        key = lambda t: (int(t[:2]) - 12) % 24 * 60 + int(t[3:])
        sl_ = sorted((r["sleep"] for r in rs), key=key)
        wk = sorted((r["wake"] for r in rs), key=key)
        md.append(f"| {cid} | {desc} | {len(rs)}/{len(allr)} | {sl_[0] + '～' + sl_[-1] if sl_ else '-'} | {wk[0] + '～' + wk[-1] if wk else '-'} | "
                  f"{sum(r['hours'] for r in rs) / len(rs):.1f} | {sum(bool(r['clamped']) for r in rs)} | "
                  f"{'是' if rs and len({(r['sleep'], r['wake']) for r in rs}) == 1 else '否'} | "
                  f"{_shift_avg(rs)} | {_shift_cnt(rs)} | {sum(key(r['sleep']) >= 720 for r in rs)} |" if rs else
                  f"| {cid} | {desc} | 0/{len(allr)} | - | - | - | - | - | - | - | - |")
    for cid, desc, _, _ in CASES:
        rs = [r for r in results if r["id"] == cid]
        if not rs:
            continue
        md += ["", f"## {cid}：{desc}", ""]
        if rs[0].get("prompt"):
            md += ["<details><summary>给她看的提示</summary>", "", "```", rs[0]["prompt"], "```", "", "</details>", ""]
        md += ["| # | 今天的平常 | 她说 | 定下 | 比平常 | 理由 | 收过 |", "|---|---|---|---|---|---|---|"]
        for r in rs:
            sh = r.get("shift")
            md.append(f"| {r['sample']} | {r.get('usual', '')} | {r.get('said', '（没定成）')} | {r.get('sleep', '')}～{r.get('wake', '')} | "
                      f"{'' if sh is None else f'{sh:+d} 分'} | {r.get('why', '')} | {'；'.join(r.get('clamped') or [])} |")
    out = OUT_DIR / f"作息回放_{stamp}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n结果在 {out}")
