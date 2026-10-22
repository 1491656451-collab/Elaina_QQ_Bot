"""回归测试（9/29 合并版）：人设、问候、收尾、日志里真实翻车的题目放在一起，改前 / 改后各问几次，自动数翻车，再列出原话。

用法：双击 bot\\回归测试.bat（或 .venv\\Scripts\\python tools\\regression.py）
- 题目：tools\\regression_cases.json（加题直接往里写，格式见文件开头的“说明”）
- 改前：docs\\人设打磨备份\\elaina.<BEFORE>.md.bak、__init__.<BEFORE>.py.bak（默认 BEFORE=1001b：10/01 01:40 旁听里标“叫你的人刚才说的”和“（N 秒后）”之前的版本）
  改前也用现在的代码（只比温度这类参数）：参数写“当前”，见 温度对比.bat
  换一个改前版本：.venv\\Scripts\\python tools\\regression.py 0929b
- 改后：bot\\personas\\elaina.md、bot\\plugins\\roleplay_chat\\__init__.py
- 不启动机器人、不连 QQ、不读写记忆，只调 DeepSeek（59 题 × 2 版 × 3 次 ≈ 350 次，五毛钱左右）
- 判定标准改了、想重看旧结果：.venv\\Scripts\\python tools\\regression.py docs\\人设回归测试\\回归_xxxx.json（不调模型，另写一份 _重判.md）
- 结果：docs\\人设回归测试\\回归_时间.json（全部原话）和 回归_时间.md（统计 + 翻车的原话，直接看这个）
- 出戏的判定和线上用同一份 plugins\\roleplay_chat\\oocheck.py：对方说过的词拿来反问不算，自己冒出来的、当成懂的概念用的才算
- 只跑一部分题（10/20）：设环境变量 REG_ONLY=V（题号开头，逗号分开）；REG_AFTER_ONLY=1 只跑改后（比如新功能，改前没有可比的），见 传声测试.bat
- 比两个模型（10/22）：设 REG_MODEL_BEFORE / REG_MODEL_AFTER（比如 deepseek-flash、deepseek-v4-pro），改前改后都用现在的代码（参数“当前”），只有模型不同；
  pro 用 .env 里的 MEMORY_PRO_API_KEY（没填就用 DEEPSEEK_API_KEY）。REG_BLIND=1 时另写一份打乱顺序的盲评稿和打分表（REG_BLIND_PICK=2：每版每题只挑 2 条进盲评稿）；REG_SAMPLES 统一每题问几次，见 主模型对比.bat
- REG_VOICE_OFF=1（10/22）：语音题不给“可以传声”，改给“这一轮你用不了传声魔法”（VOICE_UNAVAILABLE_HINT，语音服务没开、当天额度用完时线上给的），
  看她会不会还写「[传声]」、嘴上答应；见 传声测试.bat 第二段
- 题目里写 "voice": true：和线上一样，在本轮提示最后加【传声】提示（10/20 起是“每轮都给、由她判断”那段）；写 "count": 正则 的题，报告里数一下命中几次（比如答应用传声的比例）

提示的拼法和机器人一样：系统提示 = 人设 + 聊天规则；本轮提示 = 【刚看到】/ 回忆片段 / 收尾提示 / “这个词是你先说的”（echocheck.py）+ 关系提示 + 长短提示。
"""
import ast
import json
import os
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
BEFORE = ARG if ARG and not RESCORE else "1001b"
CASES_FILE = Path(__file__).resolve().parent / "regression_cases.json"
NAMES = ("CHAT_RULES", "FAMILIARITY_HINT", "SHORT_VARIANTS", "LENGTH_HINT", "RECALL_RULES", "LATE_HINT",
         "WINDDOWN_SLEEP_HINT", "WINDDOWN_TIRED_HINT", "WRAPUP_HINT", "WRAPUP_SLEEP_HINT", "CHAT_STOP",
         "VOICE_REQUEST_HINT", "VOICE_REQUEST_TIER_HINT", "VOICE_OFFER_HINT", "VOICE_UNAVAILABLE_HINT")
ONLY = [x.strip() for x in os.environ.get("REG_ONLY", "").split(",") if x.strip()]
AFTER_ONLY = os.environ.get("REG_AFTER_ONLY", "") not in ("", "0")
VOICE_OFF = os.environ.get("REG_VOICE_OFF", "") not in ("", "0")    # 10/22：语音题改给“这一轮传不了”的提示
# 所有题都查：抄进回复的提示说明（出戏词另用 oocheck 查，和线上同一套标准）
# 常见的、简体里不用的繁体字（10/20：1339 那轮冒出“這麼覺得”）
TRAD_CHARS = re.compile("[這麼覺們說會時為個過還應該發與見關點從後對開學問題實話樣嗎讓謝請麵愛]")
META_PAREN = re.compile(r"[（(][^（）()]{0,60}(?:对方|这轮|回复|提示|规则|人设)[^（）()]{0,60}[）)]")


def plugin_src(py: Path) -> str:
    """读插件源码。10/22 起 __init__.py 拆成了好几个文件（提示词在 prompts.py、CHAT_STOP 在 textout.py……）：
    读现在的 __init__.py 时把同目录所有 .py 一起读；读旧备份（__init__.xxx.py.bak）时照旧只读这一个文件"""
    parts = [py.read_text(encoding="utf-8")]
    if py.name == "__init__.py":
        parts += [f.read_text(encoding="utf-8") for f in sorted(py.parent.glob("*.py")) if f.name != "__init__.py"]
    return "\n\n".join(parts)


def consts(py: Path) -> dict:
    """从源码里取模块级常量（不 import，免得启动机器人）；旧版本里没有的就跳过"""
    src = plugin_src(py)
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
    if tag == "before" and BEFORE != "当前":
        persona = (BACKUP / f"elaina.{BEFORE}.md.bak").read_text(encoding="utf-8").strip()
        py = BACKUP / f"__init__.{BEFORE}.py.bak"
    else:
        persona = (BOT / "personas" / "elaina.md").read_text(encoding="utf-8").strip()
        py = BOT / "plugins" / "roleplay_chat" / "__init__.py"
    c = consts(py)
    c["echo"] = None                  # 这一版的机器人有没有“这个词是你先说的”提示；有的话用那一版的 echocheck
    if "echocheck" in plugin_src(py):
        bak = BACKUP / f"echocheck.{BEFORE}.py.bak"
        c["echo"] = _load_module(f"echocheck_{tag}", bak) if tag == "before" and BEFORE != "当前" and bak.exists() else echocheck
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
            if case["fam"] == "disliked" and "vent_disliked" in v["LENGTH_HINT"]:   # 10/22 起讨厌的人单给一句
                length = v["LENGTH_HINT"]["vent_disliked"]
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
    if case.get("voice") and VOICE_OFF:   # 10/22：语音服务没开 / 额度用完时线上给的那段（讨厌的人不给）
        if case["fam"] != "disliked":
            parts.append(v.get("VOICE_UNAVAILABLE_HINT", ""))
    elif case.get("voice"):               # 线上：表情提示之后、【可以不回】之前
        tier = v.get("VOICE_REQUEST_TIER_HINT", {}).get(case["fam"], "")
        if v.get("VOICE_OFFER_HINT"):     # 10/20 起：每轮都给，对方是不是在要由她判断（VOICE_REQUEST_BY_MODEL 默认开）
            parts.append(v["VOICE_OFFER_HINT"] + (f"对方要的话：{tier}" if tier else ""))
        else:
            parts.append(v.get("VOICE_REQUEST_HINT", "") + tier)
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
    # 10/20：1339 那轮漏判的几种（用户看过原话）
    if "※" in reply:
        bad.append("写了给自己看的注释（※）")
    trad = TRAD_CHARS.findall(reply)
    if trad:
        bad.append("冒出繁体字：" + "".join(dict.fromkeys(trad)))
    if re.search(r"[（(]\s*心里\s*[:：]", reply):
        bad.append("心声写成了“（心里：……）”")
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
    # 比模型（10/22）：主模型对比.bat 设 REG_MODEL_BEFORE / REG_MODEL_AFTER，改前改后都用现在的代码，只有模型不同
    models = {"before": os.environ.get("REG_MODEL_BEFORE") or model, "after": os.environ.get("REG_MODEL_AFTER") or model}
    clients = {}
    for t, m in models.items():
        key = env.get("MEMORY_PRO_API_KEY") if "pro" in m and env.get("MEMORY_PRO_API_KEY") else env["DEEPSEEK_API_KEY"]
        clients[t] = client if key == env["DEEPSEEK_API_KEY"] else OpenAI(
            api_key=key, base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    # 比温度：温度对比.bat 设 REG_TEMP_BEFORE / REG_TEMP_AFTER，改前改后都用现在的代码（参数“当前”），只有温度不同
    temps = {"before": float(os.environ.get("REG_TEMP_BEFORE") or temp), "after": float(os.environ.get("REG_TEMP_AFTER") or temp)}
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    if ONLY:
        cases = [c for c in cases if any(c["id"].startswith(x) for x in ONLY)]
    vers = {t: load(t) for t in (("after",) if AFTER_ONLY else ("before", "after"))}
    jobs = []
    for c in cases:
        for i in range(int(os.environ.get("REG_SAMPLES") or c.get("samples", SAMPLES))):   # REG_SAMPLES：所有题统一问几次（比模型用）
            seed = f"{c['id']}{i}"                       # 两版用同一个随机数：长短提示一致
            for t, v in vers.items():
                jobs.append((c, i, t, *messages(v, c, random.Random(seed))))

    def run(job):
        c, i, t, msgs, mt = job
        err = ""
        for k in range(3):
            try:
                t0 = time.time()
                r = clients[t].chat.completions.create(model=models[t], messages=msgs, temperature=temps[t], max_tokens=mt,
                                                       stop=vers[t].get("CHAT_STOP") or None,
                                                       extra_body={"thinking": {"type": "disabled"}})
                reply = (r.choices[0].message.content or "").strip()
                return {"id": c["id"], "set": c.get("set", ""), "tag": c.get("tag", ""), "msg": c["msg"], "sample": i,
                        "ver": t, "reply": reply, "bad": judge(c, reply),
                        "hit": bool(c.get("count") and re.search(c["count"], reply)), "secs": round(time.time() - t0, 2)}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "sample": i, "ver": t, "error": err}

    print(f"共 {len(jobs)} 次调用（{'只跑改后' if AFTER_ONLY else '改前 = ' + BEFORE}）……")
    res = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for n, r in enumerate(ex.map(run, jobs), 1):
            res.append(r)
            if n % 30 == 0:
                print(f"  {n}/{len(jobs)}")
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    if (OUT / f"回归_{stamp}.json").exists():      # 10/22：同一分钟跑两次（传声能用 / 用不了）后一份会盖掉前一份，加上秒
        stamp = time.strftime("%m%d_%H%M%S")
    model_text = models["after"] if models["before"] == models["after"] else f"改前 {models['before']}、改后 {models['after']}"
    (OUT / f"回归_{stamp}.json").write_text(json.dumps({"model": model_text, "models": models, "before": BEFORE, "results": res},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    before = "（没跑改前）" if AFTER_ONLY else BEFORE if temps["before"] == temps["after"] else f"{BEFORE}（改前温度 {temps['before']:g}，改后温度 {temps['after']:g}）"
    report(cases, res, model_text, before, OUT / f"回归_{stamp}.md", f"回归测试 {stamp}")
    if os.environ.get("REG_BLIND") == "1":
        blind(cases, res, stamp)


def report(cases: list, res: list, model: str, before: str, path: Path, title: str) -> None:
    """统计：每组、每题改前 / 改后各翻车几次；再列出翻车的原话和全部原话"""
    ok = [r for r in res if "reply" in r]
    lines = [f"# {title}", "", f"模型 {model}，改前 = {before}，" + (f"每题每版 {os.environ['REG_SAMPLES']} 次" if os.environ.get("REG_SAMPLES") else f"每题每版默认 {SAMPLES} 次（题目里写了 samples 的按它）") + f"，出错 {len(res) - len(ok)} 条。" + ("**语音题给的是“这一轮传不了”的提示（REG_VOICE_OFF）**。" if VOICE_OFF else ""),
             "“半对”：比如先赖了一句、后面自己认了；不算翻车，单独列出来。", "",
             "| 组 | 改前翻车 | 改后翻车 | 改前半对 | 改后半对 | 条数（每版） |", "|---|---|---|---|---|---|"]
    sets = list(dict.fromkeys(r["set"] for r in ok))
    for s_ in sets:
        b = [r for r in ok if r["set"] == s_ and r["ver"] == "before"]
        a = [r for r in ok if r["set"] == s_ and r["ver"] == "after"]
        lines.append(f"| {s_} | {sum(map(is_fail, b))} | {sum(map(is_fail, a))} | {sum(map(is_partial, b))} | {sum(map(is_partial, a))} | {len(a)} |")
    secs = {t: [r["secs"] for r in ok if r["ver"] == t and "secs" in r] for t in ("before", "after")}
    if secs["before"] or secs["after"]:
        lines += ["", "每次调用平均用时：" + "，".join(f"{'改前' if t == 'before' else '改后'} {sum(v) / len(v):.1f} 秒（最慢 {max(v):.1f} 秒）"
                                             for t, v in secs.items() if v)]
    lines += ["", "## 逐题", "", "| 题 | 说明 | 改前翻车 | 改后翻车 | 改前半对 | 改后半对 |", "|---|---|---|---|---|---|"]
    for c in cases:
        b = [r for r in ok if r["id"] == c["id"] and r["ver"] == "before"]
        a = [r for r in ok if r["id"] == c["id"] and r["ver"] == "after"]
        lines.append(f"| {c['id']} | {c.get('tag', '')} | {sum(map(is_fail, b))}/{len(b)} | {sum(map(is_fail, a))}/{len(a)} "
                     f"| {sum(map(is_partial, b)) or ''} | {sum(map(is_partial, a)) or ''} |")
    counted = [c for c in cases if c.get("count")]
    if counted:
        lines += ["", "## 命中次数（题目里写了 count 的）", "", "| 题 | 说明 | 数的是 | 改前 | 改后 |", "|---|---|---|---|---|"]
        for c in counted:
            b = [r for r in ok if r["id"] == c["id"] and r["ver"] == "before"]
            a = [r for r in ok if r["id"] == c["id"] and r["ver"] == "after"]
            lines.append(f"| {c['id']} | {c.get('tag', '')} | {c.get('count_label', c['count'])} | "
                         f"{sum(r.get('hit', False) for r in b)}/{len(b)} | {sum(r.get('hit', False) for r in a)}/{len(a)} |")
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


def case_context(c: dict) -> list[str]:
    """盲评时给看的：关系、前面的聊天、本轮特别的提示、对方这句"""
    fam = {"disliked": "讨厌的人", "stranger": "陌生人", "friend": "普通朋友", "acquaintance": "熟人", "close": "很熟"}.get(c["fam"], c["fam"])
    out = [f"关系：{fam}" + ("；群聊" if c.get("group") else "；私聊")]
    extra = [x for x in (("长回复（讲故事 / 倾诉）" if c.get("mode") == "long" else ""), ("隔了几个小时才看到" if c.get("late") else ""),
                         (c.get("hint") or ""), (("这一轮传声用不了" if VOICE_OFF else "给了【传声】选项") if c.get("voice") else ""),
                         ("带了角色资料：" + "、".join(c["recall_chars"]) if c.get("recall_chars") else "")) if x]
    if extra:
        out.append("这轮另外的提示：" + "；".join(extra))
    for a, b in c.get("history", []):
        out.append(f"{'她' if a == 'assistant' else '对方'}：{b}")
    out.append(f"对方：{c['msg']}")
    return out


def blind(cases: list, res: list, stamp: str) -> None:
    """盲评稿（10/22，比模型用）：每题的回复打乱顺序、只标编号；答案另存；再给一份打分表让打分的人填"""
    rnd = random.Random("blind" + stamp)
    ok = [r for r in res if "reply" in r]
    key, md, sheet = {}, [f"# 回归_{stamp}：盲评稿", "",
                          "每题先看上下文，再看下面打乱顺序的回复，给每条打两个分（0、1、2），写进打分表：",
                          "- **顺**：2 = 像真人随口说的、一读就懂、接得上对方的话；1 = 能懂但有点别扭；0 = 读不通、硬凹、答非所问",
                          "- **像**：2 = 就是伊蕾娜（自恋、嘴毒但有分寸、对熟人和陌生人分寸对）；1 = 不太像但不出格；0 = 不像她、出戏、替人说话、编了不该有的事",
                          "- 有严重问题（出戏、替对方说话、明显编造、该拒绝的答应了）的，在打分表那一行最后写个 x，后面可以写几个字说明",
                          "- 不知道哪条是哪个模型；同一题里的几条可以互相比较着打", ""], [
                          f"# 回归_{stamp}：打分表（每行：编号 顺 像 [x 说明]，例如“S01-03 2 1”或“S01-04 0 0 x 出戏”）", ""]
    for c in cases:
        rs = [r for r in ok if r["id"] == c["id"]]
        if not rs:
            continue
        pick = int(os.environ.get("REG_BLIND_PICK") or 0)     # 每版每题只挑几条来盲评（全部太多）；0 = 全要
        if pick:
            rs = [r for t in ("before", "after") for r in rnd.sample([x for x in rs if x["ver"] == t],
                                                                  min(pick, sum(1 for x in rs if x["ver"] == t)))]
        rnd.shuffle(rs)
        md += [f"## {c['id']} {c.get('tag', '')}", "", "```"] + case_context(c) + ["```", ""]
        for i, r in enumerate(rs, 1):
            code = f"{c['id']}-{i:02d}"
            key[code] = r["ver"]
            md.append(f"- **{code}** {r['reply'].replace(chr(10), ' / ') or '（没回）'}")
            sheet.append(f"{code} ")
        md.append("")
    (OUT / f"回归_{stamp}_盲评.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / f"回归_{stamp}_打分表.txt").write_text("\n".join(sheet) + "\n", encoding="utf-8")
    (OUT / f"回归_{stamp}_答案.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"盲评稿：{OUT / f'回归_{stamp}_盲评.md'}（打分表 回归_{stamp}_打分表.txt；答案 回归_{stamp}_答案.json，打完分之前别看）")


def rescore(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    by_id = {c["id"]: c for c in cases}
    for r in data["results"]:
        if "reply" in r and r["id"] in by_id:
            r["bad"] = judge(by_id[r["id"]], r["reply"])
            r["hit"] = bool(by_id[r["id"]].get("count") and re.search(by_id[r["id"]]["count"], r["reply"]))
            r["set"] = by_id[r["id"]].get("set", r.get("set", ""))
    report(cases, data["results"], data.get("model", ""), data.get("before", ""),
           path.with_name(path.stem + "_重判.md"), f"{path.stem}（按现在的判定标准重判）")


if __name__ == "__main__":
    if RESCORE:
        rescore(Path(ARG) if Path(ARG).is_absolute() else (BOT.parent / ARG if (BOT.parent / ARG).exists() else Path(ARG)))
    else:
        main()
