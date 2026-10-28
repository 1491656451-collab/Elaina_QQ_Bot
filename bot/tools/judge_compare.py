"""判断回放（10/28，聊天 agent，《问题排查记录-群里没反应过来-1028》第一节）

群里没 @ 她的消息，程序会先问一个小模型“这句是不是在跟伊蕾娜说话”（addressed.py 的 _judge，提示是 prompts.JUDGE_PROMPT）。
这个工具拿日志里真实出现过、当时交给判断模型的群消息（tools\\judge_cases.json，每题人工标了“该回 / 不该回 / 都行”），
按当时的群聊上下文重新拼出判断提示，用现在的写法（A）和改过的写法（B）各问几次，数一数：
  - 该回的，认出来几次；
  - 不该回的，误判成“是”几次。

上下文是从 napcat 日志里还原的：关键那句之前的 6 条群消息（谁说的、对谁说、回复了哪句），她最近说的一句。
线上真实的判断提示用的是“她的群聊记录最后 4 条 + 还没进记录的旁听最后 4 条”，和这里大体一致，但不完全一样（报告里会注明）。

怎么用：双击 bot\\判断回放.bat（不碰 data，不发消息，只调判断模型；每次调用很便宜）。
结果：docs\\判断回放\\判断回放_时间.md 和 .json
环境变量：JUDGE_SAMPLES = 每题每种问几次（默认 3）；JUDGE_VARIANTS = 只跑哪几种（A,B,C,D,E,F）；JUDGE_FAKE=1 = 程序自测，不调模型；JUDGE_PRO=1 = 判断用 pro
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
BOT_DIR = Path(os.environ.get("REPLAY_BOT") or Path(__file__).resolve().parent.parent)
LOG_ROOT = BOT_DIR.parent / "server_logs"
OUT_DIR = BOT_DIR.parent / "docs" / "判断回放"
CASES_FILE = BOT_DIR / "tools" / "judge_cases.json"
SELF_ID = "3394537326"
HER = "伊蕾娜"

# 改过的写法（B）：在“替别人转述……”那条后面加两条。例句故意不用题目里的原话，免得等于把答案告诉它
NEW_RULES = (
    "接着她刚才的话往下说的，也是在跟她说话，回答“是”：回应她刚说的话、顺着她刚说的事追问或打趣、给她解释她刚问的东西"
    "（比如她刚说旅馆太贵，有人接一句“那你还天天买面包”）——哪怕中间夹了别人的话、换了个人说、没叫她名字。\n"
    "跟她说话时提到别的群友（“他想认识你”“这家伙刚才说的你别理”），话是说给她听的，也回答“是”。\n"
)
ANCHOR = "如果是群友之间在聊天"
UNSURE = "拿不准的时候也回答“否”。"
LAST_LINE = "只输出一个字：是 或 否。"
# 10/28 第三轮（F）：C 把“牛福”“绷不住了”这种冲着她的话起哄的也判成“是”，再加一条排除
CHEER_RULE = "只是冲着她的话起哄、感叹一句（“笑死”“哈哈哈”“草”“厉害了”这类），不用她接，回答“否”。\n"
# 10/28 第二轮：先让它用一句话说清新消息在接谁的话，再给结论（D、E）
THINK_LINE = "先用一句话说清这条新消息是在接谁的话、对谁说的，然后另起一行，只写一个字：是 或 否。"

_NC_RE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d) \S+ (?:\S+ \| )?(接收 <-|发送 ->) 群聊 \[[^\]]*\((\d+)\)\] \[(.*?)\((\d+)\)\] (.*)$")
_QUOTE_RE = re.compile(r"^\[回复消息 \[(.*?)\((\d+)\)\] (.*?)\] ")
_AT_RE = re.compile(r"@(.+?) \((\d+|all)\)\s*")
_BAD_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def _ts(s: str, year: int = 2026) -> float:
    return datetime.strptime(f"{year}-{s}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING).timestamp()


def _name(n: str) -> str:
    return _BAD_RE.sub("", n).strip()[:20] or "某人"


def read_group(folder: str, gid: str) -> list[dict]:
    """napcat 日志里这个群的全部消息：时间、谁、对谁说、引用、正文"""
    out = []
    for f in sorted((LOG_ROOT / folder).glob("napcat*.log")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            m = _NC_RE.match(line)
            if not m or m.group(3) != gid:
                continue
            t, direction, name, uid, text = _ts(m.group(1)), m.group(2), m.group(4), m.group(5), m.group(6)
            targets, quote = [], ""
            q = _QUOTE_RE.match(text)
            if q:
                text = text[q.end():]
                quote = q.group(3).strip()[:30]
                targets.append(HER if q.group(2) == SELF_ID else _name(q.group(1)) + "（回复）")
            lead = re.match(r"^(?:@.+? \((?:\d+|all)\)\s*)+", text)       # 开头的 @ 才算“在跟谁说”，中间的留在正文里
            if lead:
                for a in _AT_RE.finditer(lead.group(0)):
                    targets.append(HER if a.group(2) == SELF_ID else ("全体" if a.group(2) == "all" else _name(a.group(1))))
                text = text[lead.end():]
            text = _AT_RE.sub(lambda a: f"@{_name(a.group(1))} ", text).strip()
            if not direction.startswith("发送") and text.startswith("/"):
                continue                     # 给别的机器人的指令：线上不进旁听，判断时也看不到
            out.append({"t": t, "her": direction.startswith("发送"), "uid": uid, "name": _name(name),
                        "targets": list(dict.fromkeys(targets))[:3], "quote": quote,
                        "quote_who": ("她" if q.group(2) == SELF_ID else "自己" if q.group(2) == uid else "") if q else "",
                        "text": text.replace("【", "[").replace("】", "]")})
    out.sort(key=lambda e: e["t"])
    return out


def fmt(e: dict) -> str:
    if e["her"]:
        return f"【{HER}】{e['text']}"
    head = f"【{e['name']} → {'、'.join(e['targets'])}】" if e["targets"] else f"【{e['name']}】"
    note = ""
    if e["quote"]:
        note = f"（回复{ {'她': '伊蕾娜', '自己': '自己'}.get(e['quote_who'], '') }说的“{e['quote']}”）"
    return head + note + (e["text"] or "（只@了一下）")


def build(case: dict, msgs: list[dict], template: str) -> str:
    t = _ts(f"{case['date'][5:]} {case['time']}")
    idx = next((i for i, e in enumerate(msgs) if not e["her"] and abs(e["t"] - t) <= 1.5 and case["text"] in e["text"]), None)
    if idx is None:
        raise LookupError(f"{case['id']}：日志里找不到 {case['date']} {case['time']} “{case['text']}”")
    before = msgs[max(0, idx - 6):idx]
    last = next((e["text"] for e in reversed(msgs[:idx]) if e["her"]), "")
    return template.format(last=last[:100] or "（最近没说话）", recent="\n".join(fmt(e)[:120] for e in before) or "（无）",
                           line=fmt(msgs[idx])[:220])


def current_template() -> str:
    """现在线上的写法：从 prompts.py 里直接读 JUDGE_PROMPT（不 import 插件，免得拉起整个机器人）"""
    src = (BOT_DIR / "plugins" / "roleplay_chat" / "prompts.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "JUDGE_PROMPT":
            return ast.literal_eval(node.value)
    raise RuntimeError("prompts.py 里找不到 JUDGE_PROMPT")


def new_template(cur: str) -> str:
    assert ANCHOR in cur, "JUDGE_PROMPT 改过了，找不到插入新规则的位置，要更新 judge_compare.py"
    if NEW_RULES in cur:                 # 10/28 18:40 起线上已经加了这两条：B 就是现在的写法
        return cur
    return cur.replace(ANCHOR, NEW_RULES + ANCHOR, 1)


def _env() -> dict:
    env = {}
    try:
        for line in (BOT_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip().upper()] = v.split(" #")[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def main() -> None:
    fake = os.environ.get("JUDGE_FAKE") == "1"
    samples = int(os.environ.get("JUDGE_SAMPLES") or 3)
    d = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = d["cases"]
    cur = current_template()
    b = new_template(cur)
    assert UNSURE in b and LAST_LINE in b, "JUDGE_PROMPT 改过了，要更新 judge_compare.py"
    # A 现在的；B 加两条规则；C = B 再去掉“拿不准就答否”；D = A + 先说一句理由；E = B + 先说一句理由
    variants = {"A": cur, "B": b, "C": b.replace(UNSURE, ""), "D": cur.replace(LAST_LINE, THINK_LINE),
                "E": b.replace(LAST_LINE, THINK_LINE),
                "F": b.replace("；" + UNSURE, "。").replace(ANCHOR, CHEER_RULE + ANCHOR, 1)}   # 例句故意不用题目里的原话
    only_v = [x for x in os.environ.get("JUDGE_VARIANTS", "").split(",") if x]
    if only_v:
        variants = {k: v for k, v in variants.items() if k in only_v}
    env = _env()
    pro = os.environ.get("JUDGE_PRO") == "1"         # 10/28 第四轮：判断换成 pro（和 phrasing_compare 的 P 一样读 .env 的 MEMORY_PRO_*）
    model = (env.get("MEMORY_PRO_MODEL") or "deepseek-v4-pro") if pro else (env.get("DEEPSEEK_MODEL") or "deepseek-flash")
    client = None
    if not fake:
        from openai import OpenAI
        key = (env.get("MEMORY_PRO_API_KEY") or env["DEEPSEEK_API_KEY"]) if pro else env["DEEPSEEK_API_KEY"]
        client = OpenAI(api_key=key, base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    cache: dict = {}
    rows = []
    for c in cases:
        key = (c["log"], str(c["group"]))
        if key not in cache:
            cache[key] = read_group(*key)
        for v, tpl in variants.items():
            prompt = build(c, cache[key], tpl)
            answers, raws = [], []
            think = THINK_LINE in tpl
            for _ in range(samples):
                if fake:
                    a = ("新消息在接她的话\n" if think else "") + ("是" if ("接着她刚才的话" in prompt and "你" in c["text"]) else "否")
                else:
                    r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], temperature=0,
                                                       max_tokens=120 if think else 2, extra_body={"thinking": {"type": "disabled"}})
                    a = (r.choices[0].message.content or "").strip()
                    time.sleep(0.3)
                last = a.splitlines()[-1].strip() if a else ""
                answers.append("是" if (last if think else a).startswith("是") else "否")
                if think:
                    raws.append(a)
            rows.append({"id": c["id"], "variant": v, "expect": c["expect"], "answers": answers, "raw": raws,
                         "prompt": prompt if v == "A" else prompt[prompt.index("最近的群聊"):]})
            print(f"{c['id']} {v} 期望 {c['expect']}：{''.join(answers)}  {c['text'][:20]}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%m%d_%H%M")
    (OUT_DIR / f"判断回放_{stamp}.json").write_text(json.dumps({"model": model, "samples": samples, "fake": fake,
                                                              "new_rules": NEW_RULES, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# 判断回放 {stamp}", "", f"模型 {model}，每题每种问 {samples} 次（温度 0）。{'（程序自测，没调模型）' if fake else ''}", "",
          "| 写法 | 该回的认出来 | 不该回的误判成“是” | “都行”的答“是” |", "|---|---|---|---|"]
    for v in variants:
        rs = [r for r in rows if r["variant"] == v]
        def cnt(exp):
            sub = [r for r in rs if r["expect"] == exp]
            return sum(a == "是" for r in sub for a in r["answers"]), len(sub) * samples
        y, ny = cnt("是"); n, nn = cnt("否"); m, nm = cnt("都行")
        md.append(f"| {v} | {y}/{ny} | {n}/{nn} | {m}/{nm} |")
    md += ["", "| 题 | 期望 | 那句话 | " + " | ".join(variants) + " |", "|---|---|---|" + "---|" * len(variants)]
    for c in cases:
        cells = ["".join(r["answers"]) for v in variants for r in rows if r["id"] == c["id"] and r["variant"] == v]
        md.append(f"| {c['id']} | {c['expect']} | {c['text'][:24]} | " + " | ".join(cells) + " |")
    md += ["", "B、C、E 加的两条：", "", "```", NEW_RULES.strip(), "```", "",
           "C 去掉了“" + UNSURE + "”；F = C 再加“" + CHEER_RULE.strip() + "”；D、E 把最后一句换成“" + THINK_LINE + "”（每次多花几十个字的输出）"]
    (OUT_DIR / f"判断回放_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n结果在 {OUT_DIR}\\判断回放_{stamp}.md")


if __name__ == "__main__":
    sys.exit(main())
