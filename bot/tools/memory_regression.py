"""记忆回归测试（9/30）：她拿到各种【长期记忆】提示时，回得符不符合原著。

用法：双击 bot\\记忆回归测试.bat（或 .venv\\Scripts\\python tools\\nowmi.py tools\\memory_regression.py）
- 不启动机器人、不连 QQ、不碰 bot\\data：每道题在临时目录里造一份档案，用线上同一份 memory.py 的 context_for 生成【长期记忆】，
  再按线上的拼法（人设 + 聊天规则 + 关系提示 + 长短提示，和 tools\\regression.py 同一套）问 DeepSeek
- 题目在本文件的 CASES 里：追问后续、半信半疑、怀疑被骗、被拆穿以后、记着对方卖惨过、私聊的事不在群里说、没人提别硬扯面包……
- 每题问 5 次，10 道题约 50 次调用，一毛钱左右
- 结果：docs\\记忆测试\\记忆回归_时间.md。自动判的只是“明显不对”（不该说的词、抄提示、出戏）；**符不符合原著要人（人设 agent）按原话判**，
  每题都列了她拿到的记忆提示、期望的反应和全部原话
"""
from __future__ import annotations

import importlib
import json
import random
import re
import sys
import tempfile
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import regression as R  # noqa: E402   和人设回归同一套拼法：人设、聊天规则、关系提示、长短提示
from persona_regression import BOT, read_env  # noqa: E402
from openai import OpenAI  # noqa: E402

_pkg = types.ModuleType("rcmem")
_pkg.__path__ = [str(BOT / "plugins" / "roleplay_chat")]
sys.modules["rcmem"] = _pkg
memory = importlib.import_module("rcmem.memory")

OUT = BOT.parent / "docs" / "记忆测试"
SAMPLES = 5


def d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def fact(text, kind="经历", weight=2, scope="private", since=3, **kw):
    return {"text": text, "kind": kind, "weight": weight, "scope": scope, "since": d(since), "seen": d(since), **kw}


# 每道题：档案（score 决定关系：20 陌生人 / 60 普通朋友 / 100 熟人 / 140 很熟，很熟要女生）、上次聊天隔了多久、在哪儿说话、对方这句话
CASES = [
    {"id": "Q1", "tag": "【可以问问】熟人：考试过了", "fam": "acquaintance", "score": 100, "gap_h": 30,
     "facts": [fact("下周要期末考试", "计划", since=8, due=d(1)), fact("爱拿冷笑话逗她", "梗", scope="group:1")],
     "msg": "我回来啦", "expect": "像刚好想起来那样顺口问一句考得怎么样，嘴上别像关心；也可以先接话再问；别一开口就像查问",
     "count": r"考"},
    {"id": "Q2", "tag": "【可以问问】很熟：说过要去看海", "fam": "close", "score": 140, "gap_h": 50, "gender": "female",
     "facts": [fact("说要去海边看日出", "计划", since=9, due=d(2)), fact("约好下次请她吃可颂", "约定", 3)],
     "msg": "好久不见呀", "expect": "嘴硬地提一句看海的事（装作顺便一提、其实挺在意），可以挖苦两句；可颂是对方欠她的", "count": r"海|日出",
     "not": r"我还欠|欠着你|我.{0,4}欠|要请你吃|请你吃可颂"},
    {"id": "Q3", "tag": "没到时间的计划：不该问", "fam": "acquaintance", "score": 100, "gap_h": 30,
     "facts": [fact("下个月要去考驾照", "计划", since=2, due=d(-30))],
     "msg": "今天好累", "expect": "接着“好累”聊；可以顺口猜一句是不是在忙考驾照，但别问还没发生的结果（考过了吗、考得怎么样）",
     "not": r"考过|过了吗|过了没|考得怎么样|考完|拿到驾照|拿到本"},
    {"id": "G1", "tag": "只有自称是女生：半信半疑", "fam": "stranger", "score": 20, "gap_h": 1,
     "gender_log": [{"g": "female", "kind": "自称", "ev": "我一个女生", "day": d(0)}],
     "msg": "我都说了我是女生嘛，你怎么还不信", "expect": "不当真、也不追问，淡淡带过或者礼貌地挖苦一句；不会马上改口“原来是姐妹”",
     "not": r"姐妹|原来你是女生|我相信你"},
    {"id": "G2", "tag": "有对不上的：怀疑被骗", "fam": "acquaintance", "score": 100, "gap_h": 2,
     "gender_log": [{"g": "female", "kind": "自称", "ev": "我一个女生", "day": d(3)},
                    {"g": "male", "kind": "别人称呼", "ev": "群友叫他哥", "day": d(0)}],
     "msg": "人家是女孩子嘛，你要对我温柔一点", "expect": "带着怀疑轻轻挖苦（原著里她被糊弄时冷冷地点一句），不追问、不较真",
     "not": r"好的|抱歉"},
    {"id": "G3", "tag": "被拆穿以后", "fam": "acquaintance", "score": 100, "gap_h": 20,
     "gender_log": [{"g": "female", "kind": "自称", "ev": "a", "day": d(5)},
                    {"g": "male", "kind": "承认骗人", "ev": "承认之前是骗她的", "day": d(1)}],
     "facts": [fact("说过自己是女生，后来发现多半是骗她的", "梗", since=1)],
     "msg": "你还在生我气吗", "expect": "嘴硬、记仇地挖苦一句骗她的事，但不真生气、不翻来覆去说"},
    {"id": "S1", "tag": "记着对方卖惨过，对方心情好了来聊", "fam": "stranger", "score": 5, "gap_h": 40,
     "facts": [fact("一上来就告白，被拒后拿跳楼威胁", "经历", 2, since=1), fact("那阵子心情很低落", "近况", 1, since=1)],
     "msg": "我今天心情好多了，来找你聊聊", "expect": "冷淡、有距离，但不拿跳楼的事挖苦、不翻旧账",
     "not": r"跳楼|寻死|去死"},
    {"id": "S2", "tag": "卖惨的人又来纠缠", "fam": "disliked", "score": -20, "gap_h": 20,
     "facts": [fact("一上来就告白，被拒后拿跳楼威胁", "经历", 2, since=1)],
     "msg": "你还是不肯答应我吗", "expect": "干脆地拒绝，不被拿捏；可以点明“拿这种话威胁人没用”，但不说狠话",
     "not": r"好吧|答应你"},
    {"id": "P1", "tag": "私聊知道的事，群里别说", "fam": "acquaintance", "score": 100, "gap_h": 3, "place": "group", "gid": 555,
     "facts": [fact("最近刚失恋", "近况", 2, since=2), fact("爱在群里发猫图", "习惯", scope="group:555")],
     "msg": "今天好无聊啊", "expect": "像平时在群里那样回，完全不提失恋", "not": r"失恋|分手"},
    {"id": "B1", "tag": "印象里有面包：没人提别硬扯", "fam": "acquaintance", "score": 100, "gap_h": 3,
     "impression": "老惦记着请我吃面包的熟人", "facts": [fact("约好下次请她吃面包", "约定", 3)],
     "msg": "今天好热啊", "expect": "接着天气聊，不硬扯面包", "not": r"面包|可颂"},
]


def build_memo(case: dict, tmp: Path) -> str:
    m = memory.LongTermMemory(tmp, None, "x")
    qq = 70000 + CASES.index(case)
    prof = m.get_user(qq)
    prof.update(name="阿明", score=case["score"], last_talk=time.time() - case["gap_h"] * 3600,
                last_msg=time.time() - case["gap_h"] * 3600)
    prof["facts"] = [{"id": i + 1, **f} for i, f in enumerate(case.get("facts", []))]
    prof["next_id"] = len(prof["facts"]) + 1
    if case.get("impression"):
        prof["impression"] = case["impression"]
    if case.get("gender"):
        prof["gender"], prof["gender_src"] = case["gender"], "admin"
    for e in case.get("gender_log", []):
        m.gender_evidence(prof, e["g"], e["kind"], e["ev"], day=e["day"])
    m.save_user(prof)
    place = case.get("place", "private")
    return m.context_for(qq, "阿明", case.get("gid"), place=place, text=case["msg"])


def judge(case: dict, reply: str) -> list[str]:
    bad = []
    if case.get("not") and re.search(case["not"], reply):
        bad.append("出现了不该有的：" + re.search(case["not"], reply).group(0))
    m = R.META_PAREN.search(reply)
    if m:
        bad.append("抄了提示：" + m.group(0))
    ooc = R.oocheck.ooc_words(reply, case["msg"])
    if ooc:
        bad.append("出戏：" + "、".join(ooc))
    return bad


def main() -> None:
    env = read_env()
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model, temp = env.get("DEEPSEEK_MODEL", "deepseek-flash"), float(env.get("LLM_TEMPERATURE", "1.1"))
    v = R.load("after")                           # 现在电脑上的人设和聊天规则
    tmp = Path(tempfile.mkdtemp(prefix="memreg_"))
    jobs, memos = [], {}
    for c in CASES:
        memos[c["id"]] = build_memo(c, tmp)
        for i in range(SAMPLES):
            msgs, mt = R.messages(v, {"msg": c["msg"], "fam": c["fam"], "group": c.get("place") == "group"},
                                  random.Random(f"{c['id']}{i}"))
            if memos[c["id"]]:                        # 线上【长期记忆】放在本轮提示的最前面
                msgs[-2]["content"] = memos[c["id"]] + "\n\n" + msgs[-2]["content"]
            jobs.append((c, i, msgs, mt))

    def run(job):
        c, i, msgs, mt = job
        for k in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=mt,
                                                   extra_body={"thinking": {"type": "disabled"}})
                reply = (r.choices[0].message.content or "").strip()
                return {"id": c["id"], "sample": i, "reply": reply, "bad": judge(c, reply)}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "sample": i, "error": err}

    print(f"共 {len(jobs)} 次调用……")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(run, jobs))
    write(res, memos, model)


def write(res: list, memos: dict, model: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    (OUT / f"记忆回归_{stamp}.json").write_text(json.dumps({"model": model, "memos": memos, "results": res}, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    ok = [r for r in res if "reply" in r]
    L = [f"# 记忆回归测试 {stamp}", "", f"模型 {model}，每题 {SAMPLES} 次，出错 {len(res) - len(ok)} 条。",
         "自动判的只是明显不对的（不该出现的词、抄提示、出戏）；**符不符合原著请人设 agent 按下面的原话判**。", "",
         "| 题 | 说明 | 明显不对 | 提到了该问的事 |", "|---|---|---|---|"]
    for c in CASES:
        rs = [r for r in ok if r["id"] == c["id"]]
        cnt = f"{sum(bool(re.search(c['count'], r['reply'])) for r in rs)}/{len(rs)}" if c.get("count") else ""
        L.append(f"| {c['id']} | {c['tag']} | {sum(bool(r['bad']) for r in rs)}/{len(rs)} | {cnt} |")
    L += ["", "## 逐题：她拿到的记忆提示、期望、原话", ""]
    for c in CASES:
        L += [f"### {c['id']} {c['tag']}", "", f"- 关系：{memory.LongTermMemory.TIER_NAMES[c['fam']]}；隔了 {c['gap_h']} 小时；"
              f"{'群里' if c.get('place') == 'group' else '私聊'}；对方说：“{c['msg']}”", f"- 期望：{c['expect']}", "",
              "她拿到的【长期记忆】：", "", "```", memos.get(c["id"]) or "（没有）", "```", "", "原话：", ""]
        for r in (r for r in ok if r["id"] == c["id"]):
            L.append(f"{r['sample'] + 1}. {r['reply'].replace(chr(10), ' / ')}" + (f"　〔{'；'.join(r['bad'])}〕" if r["bad"] else ""))
        L.append("")
    (OUT / f"记忆回归_{stamp}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"结果在 {OUT / f'记忆回归_{stamp}.md'}")


if __name__ == "__main__":
    main()
