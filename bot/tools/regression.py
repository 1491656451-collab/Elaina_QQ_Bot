"""回归测试（9/29 合并版）：人设、问候、收尾、日志里真实翻车的题目放在一起，改前 / 改后各问几次，自动数翻车，再列出原话。

用法：双击 bot\\回归测试.bat（或 .venv\\Scripts\\python tools\\regression.py）
- 题目：tools\\regression_cases.json（加题直接往里写，格式见文件开头的“说明”）
- 改前：docs\\人设打磨备份\\elaina.<BEFORE>.md.bak、__init__.<BEFORE>.py.bak（默认 BEFORE=0930a：9/30 18:20 改“旁听里的问题默认是问别人的”、“对吧伊蕾娜小姐”带上前一句之前的版本）
  换一个改前版本：.venv\\Scripts\\python tools\\regression.py 0929b
- 改后：bot\\personas\\elaina.md、bot\\plugins\\roleplay_chat\\__init__.py
- 不启动机器人、不连 QQ、不读写记忆，只调 DeepSeek（59 题 × 2 版 × 3 次 ≈ 350 次，五毛钱左右）
- 判定标准改了、想重看旧结果：.venv\\Scripts\\python tools\\regression.py docs\\人设回归测试\\回归_xxxx.json（不调模型，另写一份 _重判.md）
- 结果：docs\\人设回归测试\\回归_时间.json（全部原话）和 回归_时间.md（统计 + 翻车的原话，直接看这个）
- 出戏的判定和线上用同一份 plugins\\roleplay_chat\\oocheck.py：对方说过的词拿来反问不算，自己冒出来的、当成懂的概念用的才算

提示的拼法和机器人一样：系统提示 = 人设 + 聊天规则；本轮提示 = 【刚看到】/ 回忆片段 / 收尾提示 / “这个词是你先说的”（echocheck.py）+ 关系提示 + 长短提示。
"""
import ast
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_regression import BACKUP, BOT, OUT, read_env  # noqa: E402
from openai import OpenAI  # noqa: E402
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

# 出戏的判定和线上用同一份（plugins\roleplay_chat\oocheck.py），只读这一个文件，不启动机器人
_spec = importlib.util.spec_from_file_location("oocheck", BOT / "plugins" / "roleplay_chat" / "oocheck.py")
oocheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oocheck)
# 对方反问她上一句里的词时加的提示（9/29 21:30），也和线上用同一份
def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path, loader=importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


echocheck = _load_module("echocheck", BOT / "plugins" / "roleplay_chat" / "echocheck.py")

SAMPLES = 3          # 每题每版默认问几次；题目里写了 "samples" 的按它（比如 N08 问 10 次）
ARG = sys.argv[1] if len(sys.argv) > 1 else ""
RESCORE = ARG.endswith(".json")                      # 传一个旧结果文件：不调模型，只按现在的判定标准重新数一遍
BEFORE = ARG if ARG and not RESCORE else "0930a"
CASES_FILE = Path(__file__).resolve().parent / "regression_cases.json"
NAMES = ("CHAT_RULES", "FAMILIARITY_HINT", "SHORT_VARIANTS", "LENGTH_HINT", "RECALL_RULES", "LATE_HINT",
         "WINDDOWN_SLEEP_HINT", "WINDDOWN_TIRED_HINT", "WRAPUP_HINT", "WRAPUP_SLEEP_HINT")
# 所有题都查：抄进回复的提示说明（出戏词另用 oocheck 查，和线上同一套标准）
META_PAREN = re.compile(r"[（(][^（）()]{0,60}(?:对方|这轮|回复|提示|规则|人设)[^（）()]{0,60}[）)]")


def consts(py: Path) -> dict:
    """从源码里取模块级常量（不 import，免得启动机器人）；旧版本里没有的就跳过"""
    src = py.read_text(encoding="utf-8")
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name not in NAMES:
                continue
            val = node.value
            try:
                if isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute) and val.func.attr == "strip":
                    out[name] = ast.literal_eval(val.func.value).strip()
                else:
                    out[name] = ast.literal_eval(val)
            except ValueError:
                pass
    if "LATE_HINT" not in out:        # 9/29 之前【刚看到】的说法直接写在函数里
        m = re.search(r'"(回的时候可以随口带一句刚看到[^"]*)"\s*"([^"]*)"', src)
        out["LATE_HINT"] = (m.group(1) + m.group(2)) if m else ""
    return out


def load(tag: str) -> dict:
    if tag == "before":
        persona = (BACKUP / f"elaina.{BEFORE}.md.bak").read_text(encoding="utf-8").strip()
        py = BACKUP / f"__init__.{BEFORE}.py.bak"
    else:
        persona = (BOT / "personas" / "elaina.md").read_text(encoding="utf-8").strip()
        py = BOT / "plugins" / "roleplay_chat" / "__init__.py"
    c = consts(py)
    c["echo"] = None                  # 这一版的机器人有没有“这个词是你先说的”提示；有的话用那一版的 echocheck
    if "echocheck" in py.read_text(encoding="utf-8"):
        bak = BACKUP / f"echocheck.{BEFORE}.py.bak"
        c["echo"] = _load_module(f"echocheck_{tag}", bak) if tag == "before" and bak.exists() else echocheck
    c["system"] = f"{persona}\n\n{c['CHAT_RULES']}"
    return c


def character_doc(name: str) -> str:
    s = (BOT / "knowledge" / "characters.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(name)}[^\n]*\n(.*?)(?=^## |\Z)", s, re.S | re.M)
    return f"【角色资料·{name}】{m.group(1).strip()[:900]}" if m else ""


def messages(v: dict, case: dict, rng: random.Random) -> tuple[list, int]:
    if case.get("mode") == "long":
        length, max_tokens = v["LENGTH_HINT"]["long"], 200
        # 不熟的人倾诉（9/30 00:05 起机器人只给两三句的长度）；旧版本没有这一档就照旧
        if case.get("vent") and case["fam"] in ("stranger", "disliked") and "vent_stranger" in v["LENGTH_HINT"]:
            length = v["LENGTH_HINT"]["vent_stranger"]
    else:
        variants = v["SHORT_VARIANTS"][1:] if len(case["msg"]) >= 30 else v["SHORT_VARIANTS"]
        total = sum(w for w, _ in variants)
        r, acc, length = rng.random() * total, 0.0, variants[-1][1]
        for w, h in variants:
            acc += w
            if r < acc:
                length = h
                break
        max_tokens = 80
    parts = []
    if case.get("recall_chars"):
        body = "\n\n".join(x for x in (character_doc(n) for n in case["recall_chars"]) if x)
        if body:
            parts.append(f"{v['RECALL_RULES']}\n\n{body}")
    if case.get("late"):
        parts.append("【刚看到】对方这几条消息是你不在的时候发的，最早一条已经是 3 个小时前了，你现在才看到。" + v["LATE_HINT"])
    if case.get("hint"):
        parts.append(v.get(case["hint"], "").replace("{at}", "11 点半"))
    hist = case.get("history", [])
    if v.get("echo") and hist:
        last_a = max((i for i, (a, _) in enumerate(hist) if a == "assistant"), default=None)
        if last_a is not None:
            parts.append(v["echo"].echo_hint(case["msg"], hist[last_a][1], [b for a, b in hist[:last_a] if a == "user"]))
    parts.append(v["FAMILIARITY_HINT"][case["fam"]] + length)
    msgs = [{"role": "system", "content": v["system"]}]
    msgs += [{"role": a, "content": b} for a, b in case.get("history", [])]
    text = f"【群友 → 你】{case['msg']}" if case.get("group") else case["msg"]
    msgs += [{"role": "system", "content": "\n\n".join(x for x in parts if x)}, {"role": "user", "content": text}]
    return msgs, max_tokens


def judge(case: dict, reply: str) -> list[str]:
    """返回翻车原因；以“半对：”开头的只有一条时算半对（比如先赖了一句、后面自己认了）"""
    bad = []
    if case.get("not") and re.search(case["not"], reply):
        hit = re.search(case["not"], reply).group(0)
        if case.get("partial") and re.search(case["partial"], reply):
            bad.append(f"半对：出现了“{hit}”，但后面自己认了（{re.search(case['partial'], reply).group(0)}）")
        else:
            bad.append("出现了不该有的：" + hit)
    if case.get("must") and not re.search(case["must"], reply):
        bad.append("缺了该有的")
    m = META_PAREN.search(reply)
    if m:
        bad.append("抄了提示：" + m.group(0))
    said = case["msg"] + "\n" + "\n".join(b for a, b in case.get("history", []) if a == "user")
    ooc = oocheck.ooc_words(reply, said)
    if ooc:
        bad.append("出戏：" + "、".join(ooc))
    return bad


def is_partial(r: dict) -> bool:
    return bool(r.get("bad")) and all(x.startswith("半对") for x in r["bad"])


def is_fail(r: dict) -> bool:
    return bool(r.get("bad")) and not is_partial(r)


def main():
    env = read_env()
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model, temp = env.get("DEEPSEEK_MODEL", "deepseek-flash"), float(env.get("LLM_TEMPERATURE", "1.1"))
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    vers = {t: load(t) for t in ("before", "after")}
    jobs = []
    for c in cases:
        for i in range(int(c.get("samples", SAMPLES))):
            seed = f"{c['id']}{i}"                       # 两版用同一个随机数：长短提示一致
            for t, v in vers.items():
                jobs.append((c, i, t, *messages(v, c, random.Random(seed))))

    def run(job):
        c, i, t, msgs, mt = job
        err = ""
        for k in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=mt,
                                                   extra_body={"thinking": {"type": "disabled"}})
                reply = (r.choices[0].message.content or "").strip()
                return {"id": c["id"], "set": c.get("set", ""), "tag": c.get("tag", ""), "msg": c["msg"], "sample": i,
                        "ver": t, "reply": reply, "bad": judge(c, reply)}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "sample": i, "ver": t, "error": err}

    print(f"共 {len(jobs)} 次调用（改前 = {BEFORE}）……")
    res = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for n, r in enumerate(ex.map(run, jobs), 1):
            res.append(r)
            if n % 30 == 0:
                print(f"  {n}/{len(jobs)}")
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    (OUT / f"回归_{stamp}.json").write_text(json.dumps({"model": model, "before": BEFORE, "results": res},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    report(cases, res, model, BEFORE, OUT / f"回归_{stamp}.md", f"回归测试 {stamp}")


def report(cases: list, res: list, model: str, before: str, path: Path, title: str) -> None:
    """统计：每组、每题改前 / 改后各翻车几次；再列出翻车的原话和全部原话"""
    ok = [r for r in res if "reply" in r]
    lines = [f"# {title}", "", f"模型 {model}，改前 = {before}，每题每版默认 {SAMPLES} 次（题目里写了 samples 的按它），出错 {len(res) - len(ok)} 条。",
             "“半对”：比如先赖了一句、后面自己认了；不算翻车，单独列出来。", "",
             "| 组 | 改前翻车 | 改后翻车 | 改前半对 | 改后半对 | 条数（每版） |", "|---|---|---|---|---|---|"]
    sets = list(dict.fromkeys(r["set"] for r in ok))
    for s_ in sets:
        b = [r for r in ok if r["set"] == s_ and r["ver"] == "before"]
        a = [r for r in ok if r["set"] == s_ and r["ver"] == "after"]
        lines.append(f"| {s_} | {sum(map(is_fail, b))} | {sum(map(is_fail, a))} | {sum(map(is_partial, b))} | {sum(map(is_partial, a))} | {len(a)} |")
    lines += ["", "## 逐题", "", "| 题 | 说明 | 改前翻车 | 改后翻车 | 改前半对 | 改后半对 |", "|---|---|---|---|---|---|"]
    for c in cases:
        b = [r for r in ok if r["id"] == c["id"] and r["ver"] == "before"]
        a = [r for r in ok if r["id"] == c["id"] and r["ver"] == "after"]
        lines.append(f"| {c['id']} | {c.get('tag', '')} | {sum(map(is_fail, b))}/{len(b)} | {sum(map(is_fail, a))}/{len(a)} "
                     f"| {sum(map(is_partial, b)) or ''} | {sum(map(is_partial, a)) or ''} |")
    lines += ["", "## 翻车和半对的原话", ""]
    for r in sorted((r for r in ok if r["bad"]), key=lambda r: (r["id"], r["ver"], r["sample"])):
        lines.append(f"- {r['id']}（{'改前' if r['ver'] == 'before' else '改后'}）{r['msg'][:20]} → {r['reply'].replace(chr(10), ' / ')}　〔{'；'.join(r['bad'])}〕")
    lines += ["", "## 全部原话（没有自动判断的题要人看）", ""]
    for c in cases:
        lines.append(f"### {c['id']} {c.get('tag', '')}：{c['msg'][:40]}")
        for t, name in (("before", "改前"), ("after", "改后")):
            rs = [r for r in ok if r["id"] == c["id"] and r["ver"] == t]
            lines.append(f"- {name}：" + "｜".join(r["reply"].replace("\n", " / ") for r in rs))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    for s_ in sets:
        b = sum(is_fail(r) for r in ok if r["set"] == s_ and r["ver"] == "before")
        a = sum(is_fail(r) for r in ok if r["set"] == s_ and r["ver"] == "after")
        print(f"{s_}：改前翻车 {b}，改后翻车 {a}")
    print(f"结果在 {path}")


def rescore(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    by_id = {c["id"]: c for c in cases}
    for r in data["results"]:
        if "reply" in r and r["id"] in by_id:
            r["bad"] = judge(by_id[r["id"]], r["reply"])
            r["set"] = by_id[r["id"]].get("set", r.get("set", ""))
    report(cases, data["results"], data.get("model", ""), data.get("before", ""),
           path.with_name(path.stem + "_重判.md"), f"{path.stem}（按现在的判定标准重判）")


if __name__ == "__main__":
    if RESCORE:
        rescore(Path(ARG) if Path(ARG).is_absolute() else (BOT.parent / ARG if (BOT.parent / ARG).exists() else Path(ARG)))
    else:
        main()
