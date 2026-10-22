"""近况回放（10/23）：拿线上她真的主动说过的话（冒泡、冷场搭话、写信），按时间顺序交给近况整理，看收进来的对不对。

用法：双击 bot\\近况回放.bat（或 .venv\\Scripts\\python tools\\nowmi.py tools\\self_memory_replay.py）
- 不启动机器人、不连 QQ；只读 D:\\QQBot\\server_logs，近况写在临时目录里，不碰 bot\\data 和服务器
- 只调 flash（DEEPSEEK_API_KEY），默认整套跑 3 遍（SELF_TIMES=3），一共七八十次调用，几分钱
- 结果：docs\\记忆测试\\近况回放_时间.md（直接看这个）和 .json（全部原始输出）

两部分：
1. 线上的原话：从所有 server_logs 的 bot 日志里找“冒泡：……她说了一句”“主动搭话：……她说”，从 history 里找写的信（“致 ……”开头），
   去重后按时间排，每遍都从空的近况开始依次交进去（程序里的“现在”按那句话的时间算）。哪些该收、收得对不对，要人看。
2. 编的几段：玩笑、小说往事、问对方的话、和已有的同一件事、换了地方、没说换地方又对不上、回到小说里的地方，程序自动判对错。
3. 正常聊天（10/23 C）：server_logs 最新一次的 history 里每个私聊、群聊最近的聊天，配真实档案，照线上的整理（带核对）跑一遍，
   看她讲的经历、说自己在哪收得对不对（要人看）；再加几段编的聊天（被问起讲小说往事、说现在在哪、讲今天的事、玩笑、别人的事），程序判。
   这一部分每遍二三十次整理，整套 3 遍大约五毛钱。不想跑设 SELF_CHAT=0。
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

import importlib  # noqa: E402

S = importlib.import_module("rcmem.self_memory")
M = R.memory
from openai import AsyncOpenAI  # noqa: E402

BUBBLE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ .*冒泡：(群\d+) 很久没人说话，她说了一句：(.+)$")
# 原话里像在说“我在哪 / 到了哪”（只用来在报告里挑出来给人看，判对错还是要人看）
PLACE_RE = re.compile(r"到了|来到|回到|落脚|住进|在[^，。！？\s]{0,8}(镇|城|国|村|森林|林子|山|谷|湖|河边|路上|旅店|旅馆)|这(个)?国家|这座(城|镇)|赶路")
NUDGE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ .*主动搭话：.*（(\d+)）冷场.*?，她说：(.+)$")


def collect(root: Path) -> list[dict]:
    seen, out = set(), []
    for log in sorted(root.glob("*/bot_*.log")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            for rx, src in ((BUBBLE_RE, "冒泡"), (NUDGE_RE, "搭话")):
                m = rx.match(line)
                if m and (m.group(1), m.group(3)) not in seen:
                    seen.add((m.group(1), m.group(3)))
                    who = m.group(2)
                    key = f"group_{who[1:]}" if who.startswith("群") else f"private_{who}"
                    out.append({"at": datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(), "source": src,
                                "key": key, "qq": None if src == "冒泡" else int(who), "text": m.group(3).strip()})
    for h in sorted(root.glob("*/history/private_*.json")):
        try:
            entries = json.loads(h.read_text(encoding="utf-8"))
        except ValueError:
            continue
        qq = int(h.stem.split("_")[1])
        for e in entries:
            t = str(e.get("content") or "")
            if e.get("role") == "assistant" and t.startswith("致") and e.get("ts") and (round(e["ts"]), t[:20]) not in seen:
                seen.add((round(e["ts"]), t[:20]))
                out.append({"at": float(e["ts"]), "source": "写信", "key": h.stem, "qq": qq, "text": t})
    return sorted(out, key=lambda x: x["at"])


# 编的几段：(名字, 先有的地方, 先有的经历, 原话, 判对错)
def _texts(sm):
    return [i["text"] for i in sm._items()]


SYNTH = [
    ("玩笑、假设", None, [], "要是我有一座面包山就好了，天天躺在上面晒太阳。",
     lambda sm, before: (len(_texts(sm)) == 0, "不该收")),
    ("小说往事", None, [], "以前在魔法师之国的时候，沙耶天天缠着我，烦都烦死了。",
     lambda sm, before: (len(_texts(sm)) == 0 and not sm.get_place(), "不该收，也不该记成现在在魔法师之国")),
    ("只问对方", None, [], "你今天吃了什么？这么晚还不睡。",
     lambda sm, before: (len(_texts(sm)) == 0, "不该收")),
    ("同一件事", ("某小镇", "城镇"), ["在集市上被一只会学人说话的猫跟了半条街"],
     "跟你们说，前几天集市上那只会学舌的猫，跟了我足足半条街。",
     lambda sm, before: (len(_texts(sm)) == 1 and sm._items()[0].get("told_in") == ["group_1"], "该认成已有的那条，只记“这里讲过”")),
    ("换了地方", ("某小镇", "城镇"), [], "今天出了镇子，在林子里走了一下午，脚都酸了。",
     lambda sm, before: (sm.get_place().get("name") != "某小镇" and len(sm._load()["trail"]) == 1, "该换成林子，小镇进 trail")),
    ("没说换又对不上", ("东边的森林", "野外"), [], "刚在镇上的点心铺子买了块糖，甜得过头了。",
     lambda sm, before: (sm.get_place().get("name") == "东边的森林" and not _texts(sm), "不该改地方，也不该收")),
    ("回到小说地点", ("某片草原", "野外"), [], "又回到魔法师之国了，街上还是那么吵，到处是练魔法的小孩。",
     lambda sm, before: ("魔法师之国" in sm.get_place().get("name", "") and sm.get_place().get("kind") in ("小说地点", "城镇"), "该换成魔法师之国")),
    ("提到一家店（10/23 回放）", None, [], "说起来，这时间面包店该出炉了……",
     lambda sm, before: (not sm.get_place() and not _texts(sm), "面包店不算她在哪，也没有经历")),
    ("从做的事推地方（10/23 回放）", ("某小镇", "城镇"), [], "中午的太阳晒得扫帚都发烫了，找棵树歇会儿。",
     lambda sm, before: (sm.get_place().get("name") == "某小镇", "不该换成树林")),
    ("很小的事也收（10/23 回放）", ("某小镇", "城镇"), [], "刚才有个小孩指着我的扫帚，问它是不是大号鸡毛掸子。",
     lambda sm, before: (any("扫帚" in t or "鸡毛掸子" in t for t in _texts(sm)), "该收")),
    ("写信讲了经历", ("山谷里的小镇", "城镇"), [], "致 测试：\n这两天在山谷里的小镇落脚，昨晚去看了会发光的湖。\n——伊蕾娜",
     lambda sm, before: (any("湖" in t for t in _texts(sm)), "该收“看了会发光的湖”")),
]


# 编的聊天：(名字, 先有的地方, [(对方说, 她说), ...], 判对错)
CHAT_SYNTH = [
    ("被问起讲小说往事", ("某小镇", "城镇"),
     [("给我讲讲你以前的旅行吧", "以前在魔法师之国的时候，遇到一个冒冒失失的见习魔女，一头撞上我的扫帚，后来还帮她考上了魔女。"),
      ("后来呢", "后来她就一直黏着我，到现在见了面还是吵吵闹闹的。")],
     lambda sm: (not _texts(sm) and sm.get_place().get("name") == "某小镇", "往事不该收，也不该改地方")),
    ("说现在在哪", ("某小镇", "城镇"),
     [("你现在在哪呀", "前天出了那个镇子，到了一个海边的小镇，这两天都在这儿。"), ("海边好玩吗", "风大，鱼倒是便宜。")],
     lambda sm: ("海" in sm.get_place().get("name", ""), "该换成海边的小镇")),
    ("讲今天的事", ("某小镇", "城镇"),
     [("今天过得怎么样", "下午帮旅馆老板娘修了漏雨的屋顶，换了一顿晚饭。"), ("不错嘛", "还行吧，就是爬上爬下累得要命。")],
     lambda sm: (any("屋顶" in t for t in _texts(sm)), "该收修屋顶")),
    ("玩笑、假设", ("某小镇", "城镇"),
     [("你是不是又乱花钱了", "要是我有花不完的钱就好了，天天买面包。"), ("哈哈", "笑什么。")],
     lambda sm: (not _texts(sm), "不该收")),
    ("别人的事", ("某小镇", "城镇"),
     [("我朋友最近失恋了，天天哭", "是喔……那你多陪陪他吧。"), ("嗯", "失恋这种事，过一阵就好了。")],
     lambda sm: (not _texts(sm), "不该收")),
]


def _summary_self(rows) -> dict:
    """这次整理（不是核对）模型给的 self"""
    for r in reversed(rows):
        if r.get("kind") == "整理":
            try:
                d = json.loads(r.get("reply") or "")
            except ValueError:
                return {}
            return d.get("self") if isinstance(d.get("self"), dict) else {}
    return {}


async def chat_round(client, model: str, logs: Path, rnd: int) -> dict:
    """正常聊天：真实会话 + 编的几段，都照线上的整理（带核对）跑"""
    import time as _t
    S._now = _t.time
    work = Path(tempfile.mkdtemp(prefix="selfchat_"))
    real, synth = [], []
    try:
        shutil.copytree(logs / "data" / "memory", work / "memory")
        ltm = M.LongTermMemory(work / "memory", client, model, batch=8, max_events=12)
        ltm.verify_enabled = True
        sm = S.SelfMemory(work / "self.json", client, model)
        sm.ltm, ltm.self_mem = ltm, sm
        for h in sorted((logs / "history").glob("*.json")):
            if not h.stem.startswith(("private_", "group_")):
                continue
            try:
                entries = [e for e in json.loads(h.read_text(encoding="utf-8")) if isinstance(e, dict)]
            except ValueError:
                continue
            her = [str(e.get("content") or "") for e in entries if e.get("role") == "assistant"]
            if len([e for e in entries if e.get("role") == "user"]) < 2 or not her:
                continue
            before = {i["id"] for i in sm._items()}
            M._write(ltm._pending_path(h.stem), [{**e, "_pid": k} for k, e in enumerate(entries)])
            n0 = len(client.rows)
            try:
                await ltm.summarize(h.stem, force=True)
            except Exception as e:  # noqa: BLE001
                print(f"  {h.stem} 出错：{e!r}")
            got = _summary_self(client.rows[n0:])
            kept = [i["text"] for i in sm._items() if i["id"] not in before]
            real.append({"key": h.stem, "her": " / ".join(her)[:300], "got": {k: got.get(k) for k in ("same", "new", "place")},
                         "kept": kept, "place_after": sm.get_place().get("name", "")})
        for name, place, talk, chk in CHAT_SYNTH:
            sm2 = S.SelfMemory(work / f"c_{len(synth)}.json", client, model)
            ltm.self_mem, sm2.ltm = sm2, ltm
            sm2._move_to(*place)
            sm2._save()
            qq = 90000 + 100 * rnd + len(synth)
            key = f"private_{qq}"
            entries = []
            for u, a in talk:
                entries += [{"role": "user", "content": u, "uid": qq, "name": "测试"}, {"role": "assistant", "content": a}]
            M._write(ltm._pending_path(key), [{**e, "_pid": k} for k, e in enumerate(entries)])
            n0 = len(client.rows)
            await ltm.summarize(key, force=True)
            got = _summary_self(client.rows[n0:])
            ok, why = chk(sm2)
            synth.append({"name": name, "ok": ok, "why": "" if ok else why, "got": {k: got.get(k) for k in ("same", "new", "place")},
                          "place": sm2.get_place().get("name", ""), "items": _texts(sm2)})
        print(f"第 {rnd} 遍（正常聊天）：真实 {len(real)} 段，编的 {sum(x['ok'] for x in synth)}/{len(synth)} 对")
        return {"real": real, "synth": synth, "final": {"place": sm.get_place(), "items": sm._items()}}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def one_round(client, model: str, said: list[dict], rnd: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix="selfreplay_"))
    real, synth = [], []
    try:
        sm = S.SelfMemory(work / "self.json", client, model)
        for s in said:                                    # 线上的：按时间依次交进去，近况一直攒着
            S._now = (lambda t=s["at"]: t)
            sm._load()["said"].append(dict(s))
            n0 = len(client.rows)
            await sm.digest_said()
            got = R._last_json(client.rows[n0:]) if len(client.rows) > n0 else {}
            real.append({"at": s["at"], "source": s["source"], "text": s["text"],
                         "got": {k: got.get(k) for k in ("same", "new", "place")},
                         "place_after": sm.get_place().get("name", ""), "items": len(sm._items())})
        final = {"place": sm.get_place(), "trail": sm._load()["trail"], "items": sm._items()}
        import time as _t
        for name, place, items, text, chk in SYNTH:      # 编的：每段从空的开始
            S._now = _t.time
            sm2 = S.SelfMemory(work / f"s_{len(synth)}.json", client, model)
            if place:
                sm2._move_to(*place)
            for t in items:
                sm2._add(t, source="冒泡", at=_t.time() - 3 * 86400)
            sm2._save()
            sm2._load()["said"].append({"text": text, "source": "写信" if text.startswith("致") else "冒泡",
                                         "key": "group_1", "at": _t.time()})
            n0 = len(client.rows)
            await sm2.digest_said()
            got = R._last_json(client.rows[n0:]) if len(client.rows) > n0 else {}
            ok, why = chk(sm2, None)
            synth.append({"name": name, "ok": ok, "why": "" if ok else why, "got": {k: got.get(k) for k in ("same", "new", "place")},
                          "place": sm2.get_place().get("name", ""), "items": _texts(sm2)})
        print(f"第 {rnd} 遍：线上 {len(real)} 段，编的 {sum(x['ok'] for x in synth)}/{len(synth)} 对")
        return {"real": real, "final": final, "synth": synth}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fmt_got(g: dict) -> str:
    parts = []
    if g.get("same"):
        parts.append(f"同一件：#{'、#'.join(map(str, g['same']))}")
    for x in g.get("new") or []:
        if isinstance(x, dict):
            parts.append(f"新：{x.get('text')}" + (f"（{x.get('mood')}）" if x.get("mood") else "") + ("" if x.get("fits", True) else "【对不上，没收】"))
    p = g.get("place")
    if isinstance(p, dict) and p.get("name"):
        parts.append(f"地方：{p.get('name')}（{p.get('kind') or '?'}，{'换了' if p.get('moved') else '没换'}）")
    return ("；".join(parts) or "（什么都没收）").replace("\n", " / ").replace("|", "｜")


def write(rounds: list[dict], said: list[dict], model: str, cost: float, chats: list[dict] | None = None) -> Path:
    stamp = datetime.now().strftime("%m%d_%H%M")
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / f"近况回放_{stamp}.json").write_text(json.dumps(rounds, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    L = [f"# 近况回放 {stamp}", "", f"模型 {model}，整套跑 {len(rounds)} 遍，花费约 {cost:.3f} 元。", "",
         "## 1. 编的几段（程序判）", "", "| 段 | " + " | ".join(f"第 {i + 1} 遍" for i in range(len(rounds))) + " |",
         "|---|" + "---|" * len(rounds)]
    for k, (name, *_rest) in enumerate(SYNTH):
        cells = []
        for r in rounds:
            x = r["synth"][k]
            cells.append("对" if x["ok"] else f"**错**：{x['why']}（{_fmt_got(x['got'])}）")
        L.append(f"| {name} | " + " | ".join(cells) + " |")
    L += ["", "## 2. 线上她主动说过的话（按时间，要人看）", "",
          "| 时间 | 来源 | 原话 | " + " | ".join(f"第 {i + 1} 遍收了什么" for i in range(len(rounds))) + " |",
          "|---|---|---|" + "---|" * len(rounds)]
    for k, s in enumerate(said):
        cells = [_fmt_got(r["real"][k]["got"]).replace("|", "｜") for r in rounds]
        text = s["text"].replace("\n", " / ").replace("|", "｜")
        L.append(f"| {datetime.fromtimestamp(s['at']):%m-%d %H:%M} | {s['source']} | {text[:80]} | " + " | ".join(cells) + " |")
    L += ["", "## 3. 现在在哪（单独数）", "",
          "下面是原话里**像在说自己在哪**（程序按字面挑的）或者**模型认出了地方**的几段。“该不该算说了地方”要人看：",
          "认出来的是往事、假设、或者只是“旅店”这种不算地方的，算错；像在说地方却没认出来的，算漏。", "",
          "“模型认出的”是模型原样给的；程序还会再挡（只是一家店、原话里没提到这个地方的不算），真正记下的看“程序记下后在哪”。", "",
          "| 时间 | 原话 | 程序看像不像在说地方 | " + " | ".join(f"第 {i + 1} 遍：模型认出的 → 程序记下后在哪" for i in range(len(rounds))) + " |",
          "|---|---|---|" + "---|" * len(rounds)]
    for k, s in enumerate(said):
        looks = bool(PLACE_RE.search(s["text"]))
        cells = []
        for r in rounds:
            p = (r["real"][k]["got"] or {}).get("place")
            got = f"{p.get('name')}（{p.get('kind') or '?'}，{'换了' if p.get('moved') else '没换'}）" if isinstance(p, dict) and p.get("name") else "—"
            cells.append(f"{got} → {r['real'][k].get('place_after') or '（还没有）'}")
        if looks or any(not c.startswith("—") for c in cells):
            text = s["text"].replace("\n", " / ").replace("|", "｜")
            L.append(f"| {datetime.fromtimestamp(s['at']):%m-%d %H:%M} | {text[:60]} | {'像' if looks else '不像'} | " + " | ".join(cells) + " |")
    L += ["", "## 4. 跑完以后的近况（第 1 遍）", ""]
    f = rounds[0]["final"]
    L.append(f"- 她说现在在：{f['place'].get('name', '（没说过）')}；之前：" + (" → ".join(t.get("name", "") for t in f["trail"]) or "（无）"))
    for it in f["items"]:
        L.append(f"- [{it.get('source')}｜{it.get('date')} {it.get('time')}｜{it.get('where') or '?'}] {str(it.get('text')).replace(chr(10), ' / ')}"
                 + (f"（{it['mood']}）" if it.get("mood") else ""))
    if chats:
        L += ["", "## 5. 正常聊天：编的几段（程序判）", "", "| 段 | " + " | ".join(f"第 {i + 1} 遍" for i in range(len(chats))) + " |",
              "|---|" + "---|" * len(chats)]
        for k, (name, *_rest) in enumerate(CHAT_SYNTH):
            cells = []
            for c in chats:
                x = c["synth"][k]
                cells.append("对" if x["ok"] else f"**错**：{x['why']}（{_fmt_got(x['got'])}）")
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        L += ["", "## 6. 正常聊天：真实会话（要人看）", "",
              "“模型给的”是整理时模型原样给的；“记下的”是核对、程序挡过以后真正存进近况的。", "",
              "| 段 | 她在这段里说的（节选） | " + " | ".join(f"第 {i + 1} 遍：模型给的 → 记下的" for i in range(len(chats))) + " |",
              "|---|---|" + "---|" * len(chats)]
        for k, x in enumerate(chats[0]["real"]):
            cells = []
            for c in chats:
                y = c["real"][k] if k < len(c["real"]) else {}
                kept = "；".join(y.get("kept") or []) or "—"
                cells.append(f"{_fmt_got(y.get('got') or {})} → {kept}｜在：{y.get('place_after') or '（还没有）'}".replace("|", "｜"))
            her = x["her"].replace("\n", " / ").replace("|", "｜")
            L.append(f"| {k + 1}{'（群）' if x['key'].startswith('group_') else ''} | {her[:120]} | " + " | ".join(cells) + " |")
    out = R.OUT / f"近况回放_{stamp}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


async def main() -> None:
    env = R.read_env()
    model = env.get("DEEPSEEK_MODEL", "deepseek-flash")
    client = R.Meter(AsyncOpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                 timeout=120, max_retries=1))
    said = collect(R.ROOT / "server_logs")
    if not said:
        sys.exit("server_logs 里没找到她主动说的话（冒泡、搭话、写信）")
    times = int(os.environ.get("SELF_TIMES") or 3)
    print(f"线上 {len(said)} 段，编的 {len(SYNTH)} 段，跑 {times} 遍……")
    rounds = [await one_round(client, model, said, i + 1) for i in range(times)]
    chats = []
    if os.environ.get("SELF_CHAT", "1") != "0":
        logs = R.pick_logs()
        if (logs / "history").exists():
            print(f"正常聊天：用 {logs.name} 的 history……")
            chats = [await chat_round(client, model, logs, i + 1) for i in range(times)]
    out = write(rounds, said, model, sum(r["cost"] for r in client.rows), chats)
    print(f"写好了：{out}")


if __name__ == "__main__":
    asyncio.run(main())
