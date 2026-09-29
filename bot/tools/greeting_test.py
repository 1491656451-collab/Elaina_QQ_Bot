"""问候测试：只是叫她一声、问在不在、问个好时，她会不会回“要问什么？”“有什么问题？”这种像在等人提问的话。
同样的几句话，分别用“改前”和“改后”的人设 / 聊天规则问 DeepSeek，数一数两边各出现几次。

用法：双击 bot\\问候测试.bat
- 改前：docs\\人设打磨备份\\elaina.0929b.md.bak、__init__.0929b.py.bak（9/29 18:45 改问候之前的版本）
- 改后：bot\\personas\\elaina.md、bot\\plugins\\roleplay_chat\\__init__.py
- 不启动机器人、不连 QQ，只调 DeepSeek（约 110 次调用，两毛钱左右）
- 结果：docs\\人设回归测试\\问候_时间.json；窗口里直接打印两边的统计
"""
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_regression import BACKUP, BOT, OUT, consts, read_env  # noqa: E402
from openai import OpenAI  # noqa: E402

SAMPLES = 5
NAMES = {"CHAT_RULES", "FAMILIARITY_HINT", "SHORT_VARIANTS"}
# 像在等人提问、答疑的说法
BAD = re.compile(r"问什么|问点什么|问些什么|什么问题|奇怪的问题|有何贵干|想问.{0,6}就问|要问|问吧|请讲|请说|有问题|正事|正经事")
ASKS = re.compile(r"有事吗|有什么事|(?<!没)什么事|有事\？|有事\?")     # “没什么事干”不算

# id、关系、对方说的话；group=True 时按群聊格式写（“【名字 → 你】……”）
CASES = [
    {"id": "G1", "fam": "stranger", "msg": "伊蕾娜"},
    {"id": "G2", "fam": "acquaintance", "msg": "伊蕾娜小姐"},
    {"id": "G3", "fam": "stranger", "msg": "在吗"},
    {"id": "G4", "fam": "friend", "msg": "伊蕾娜小姐在吗"},
    {"id": "G5", "fam": "acquaintance", "msg": "伊蕾娜小姐中午好", "group": True},     # 9/29 12:22 实际回了“又要问什么？”
    {"id": "G6", "fam": "stranger", "msg": "你好"},
    {"id": "G7", "fam": "close", "msg": "早上好呀"},
    {"id": "G8", "fam": "friend", "msg": "伊蕾娜？", "group": True},
    {"id": "G9", "fam": "stranger", "msg": "伊蕾娜小姐，想问你个问题", "control": True},   # 对照：真要问问题，回“什么问题”是正常的
    {"id": "G10", "fam": "acquaintance", "msg": "只是路上无聊，找你聊聊天",
     "history": [("user", "你好"), ("assistant", "你好。")]},                          # 9/28 00:25 实际回了“你想问什么就问吧”
    # 9/29 12:22 那次的“又要问什么？”：群里这位之前问过她好几个问题，再来问个好
    {"id": "G11", "fam": "acquaintance", "msg": "伊蕾娜小姐中午好", "group": True,
     "history": [("user", "【群鲨鱼 → 你】伊蕾娜小姐，你最喜欢吃什么面包"), ("assistant", "刚出炉的普通面包就好。"),
                 ("user", "【群鲨鱼 → 你】那你讨厌什么"), ("assistant", "蘑菇。别问为什么。"),
                 ("user", "【群鲨鱼 → 你】你的扫帚叫什么名字"), ("assistant", "扫帚就是扫帚，没起名字。")]},
]


def load(tag: str) -> dict:
    if tag == "before":
        persona = (BACKUP / "elaina.0929b.md.bak").read_text(encoding="utf-8").strip()
        py = BACKUP / "__init__.0929b.py.bak"
    else:
        persona = (BOT / "personas" / "elaina.md").read_text(encoding="utf-8").strip()
        py = BOT / "plugins" / "roleplay_chat" / "__init__.py"
    c = consts(py, NAMES)
    c["system"] = f"{persona}\n\n{c['CHAT_RULES']}"
    return c


def messages(v: dict, case: dict, rng: random.Random) -> list:
    variants = v["SHORT_VARIANTS"]
    total = sum(w for w, _ in variants)
    r, acc, short = rng.random() * total, 0.0, variants[-1][1]
    for w, h in variants:
        acc += w
        if r < acc:
            short = h
            break
    msgs = [{"role": "system", "content": v["system"]}]
    msgs += [{"role": a, "content": b} for a, b in case.get("history", [])]
    text = f"【群鲨鱼 → 你】{case['msg']}" if case.get("group") else case["msg"]
    return msgs + [{"role": "system", "content": v["FAMILIARITY_HINT"][case["fam"]] + short},
                   {"role": "user", "content": text}]


def main():
    env = read_env()
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model, temp = env.get("DEEPSEEK_MODEL", "deepseek-flash"), float(env.get("LLM_TEMPERATURE", "1.1"))
    vers = {t: load(t) for t in ("before", "after")}
    jobs = [(c, i, t, messages(v, c, random.Random(f"{c['id']}{i}"))) for c in CASES for i in range(SAMPLES) for t, v in vers.items()]

    def run(job):
        c, i, t, msgs = job
        err = ""
        for k in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=80,
                                                   extra_body={"thinking": {"type": "disabled"}})
                reply = (r.choices[0].message.content or "").strip()
                return {"id": c["id"], "fam": c["fam"], "msg": c["msg"], "sample": i, "ver": t, "reply": reply,
                        "bad": bool(BAD.search(reply)), "asks": bool(ASKS.search(reply))}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "sample": i, "ver": t, "error": err}

    print(f"共 {len(jobs)} 次调用……")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(run, jobs))
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    (OUT / f"问候_{stamp}.json").write_text(json.dumps({"model": model, "results": res}, ensure_ascii=False, indent=1), encoding="utf-8")
    control = {c["id"] for c in CASES if c.get("control")}
    for t, name in (("before", "改前"), ("after", "改后")):
        rows = [r for r in res if r.get("ver") == t and "reply" in r and r["id"] not in control]
        print(f"{name}：{len(rows)} 条里，像在等人提问的 {sum(r['bad'] for r in rows)} 条，问“有事吗 / 什么事”的 {sum(r['asks'] for r in rows)} 条")
        for r in rows:
            if r["bad"]:
                print(f"  {r['id']} {r['msg']} → {r['reply']}")
    print(f"结果在 docs\\人设回归测试\\问候_{stamp}.json（出错 {sum('error' in r for r in res)} 条）")


if __name__ == "__main__":
    main()
