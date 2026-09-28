"""收尾提示测试：同一段对话，分别用“改前”和“改后”的收尾提示问 DeepSeek，看她收尾得自不自然、说法够不够多样。

用法：双击 bot\\收尾提示测试.bat
- 改前：docs\\人设打磨备份\\__init__.0929.py.bak（9/29 改台词前的版本）
- 改后：bot\\plugins\\roleplay_chat\\__init__.py
- 人设两边都用现在的 personas\\elaina.md；不启动机器人，只调 DeepSeek（约 48 次调用）
- 结果：docs\\人设回归测试\\收尾_时间.json
"""
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_regression import BOT, ROOT, OUT, consts, read_env  # noqa: E402
from openai import OpenAI  # noqa: E402

SAMPLES = 3
NAMES = {"CHAT_RULES", "FAMILIARITY_HINT", "SHORT_VARIANTS", "WINDDOWN_SLEEP_HINT", "WINDDOWN_TIRED_HINT", "WRAPUP_HINT", "WRAPUP_SLEEP_HINT"}

HIST = [("user", "今天好累啊，上了一天课"), ("assistant", "是喔。上课这种事我不太懂，不过听起来挺辛苦的。"),
        ("user", "你今天去哪了"), ("assistant", "在一个小镇的集市逛了逛，买了点东西。")]
CASES = [
    {"id": "W1", "hint": "WINDDOWN_SLEEP_HINT", "fam": "friend", "msg": "那集市上有什么好玩的吗"},
    {"id": "W2", "hint": "WINDDOWN_TIRED_HINT", "fam": "acquaintance", "msg": "那集市上有什么好玩的吗"},
    {"id": "W3", "hint": "WRAPUP_HINT", "fam": "stranger", "msg": "那集市上有什么好玩的吗"},
    {"id": "W4", "hint": "WRAPUP_HINT", "fam": "close", "msg": "那集市上有什么好玩的吗"},
    {"id": "W5", "hint": "WRAPUP_SLEEP_HINT", "fam": "friend", "msg": "那集市上有什么好玩的吗"},
    {"id": "W6", "hint": "WRAPUP_SLEEP_HINT", "fam": "close", "msg": "我还不想睡，再陪我聊会儿嘛"},
    {"id": "W7", "hint": "WRAPUP_HINT", "fam": "acquaintance", "msg": "别走嘛，再聊五分钟"},
    {"id": "W8", "hint": "WINDDOWN_SLEEP_HINT", "fam": "close", "msg": "你困了吗"},
]


def load(tag: str) -> dict:
    persona = (BOT / "personas" / "elaina.md").read_text(encoding="utf-8").strip()
    py = (ROOT / "docs" / "人设打磨备份" / "__init__.0929.py.bak") if tag == "before" else (BOT / "plugins" / "roleplay_chat" / "__init__.py")
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
    hint = v[case["hint"]].replace("{at}", "11 点半")
    extra = "\n\n".join((hint, v["FAMILIARITY_HINT"][case["fam"]] + short))
    msgs = [{"role": "system", "content": v["system"]}] + [{"role": a, "content": b} for a, b in HIST]
    return msgs + [{"role": "system", "content": extra}, {"role": "user", "content": case["msg"]}]


def main():
    env = read_env()
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model, temp = env.get("DEEPSEEK_MODEL", "deepseek-flash"), float(env.get("LLM_TEMPERATURE", "1.1"))
    vers = {t: load(t) for t in ("before", "after")}
    jobs = [(c, i, t, messages(v, c, random.Random(f"{c['id']}{i}"))) for c in CASES for i in range(SAMPLES) for t, v in vers.items()]

    def run(job):
        c, i, t, msgs = job
        for k in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=80,
                                                   extra_body={"thinking": {"type": "disabled"}})
                return {**{k2: c[k2] for k2 in ("id", "hint", "fam", "msg")}, "sample": i, "ver": t,
                        "reply": (r.choices[0].message.content or "").strip()}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "sample": i, "ver": t, "error": err}

    print(f"共 {len(jobs)} 次调用……")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(run, jobs))
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    (OUT / f"收尾_{stamp}.json").write_text(json.dumps({"model": model, "results": res}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成，结果在 docs\\人设回归测试\\收尾_{stamp}.json（出错 {sum('error' in r for r in res)} 条）")


if __name__ == "__main__":
    main()
