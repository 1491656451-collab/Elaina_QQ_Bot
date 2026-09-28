"""人设回归测试：同一批测试问题，分别用“旧版”和“新版”人设/提示词问 DeepSeek，结果写成 JSON。

用法：双击 bot\\人设回归测试.bat（或 .venv\\Scripts\\python tools\\persona_regression.py）
- 旧版：docs\\人设打磨备份\\ 里的 elaina.md.bak、__init__.py.bak、qzone_diary.py.bak
- 新版：bot\\personas\\elaina.md、bot\\plugins\\roleplay_chat\\__init__.py、qzone_diary.py
- 不启动机器人、不连 QQ、不读写任何记忆，只调 DeepSeek 接口
- 结果：docs\\人设回归测试\\结果_时间.json；跑完另写一个 done_时间.txt
"""
import ast
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

BOT = Path(__file__).resolve().parent.parent
ROOT = BOT.parent
BACKUP = ROOT / "docs" / "人设打磨备份"
OUT = ROOT / "docs" / "人设回归测试"
SAMPLES = 3            # 每个版本每题问几次


def read_env() -> dict:
    env = {}
    for line in (BOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip().upper()] = v.split(" #")[0].strip().strip('"').strip("'")
    return env


def consts(py: Path, names: set[str]) -> dict:
    """从源码里取出模块级常量（不 import，免得启动机器人）"""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name not in names:
                continue
            val = node.value
            if isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute) and val.func.attr == "strip":
                out[name] = ast.literal_eval(val.func.value).strip()
            else:
                out[name] = ast.literal_eval(val)
    missing = names - out.keys()
    if missing:
        raise RuntimeError(f"{py.name} 里没找到：{missing}")
    return out


def load_version(tag: str) -> dict:
    if tag == "old":
        persona = (BACKUP / "elaina.md.bak").read_text(encoding="utf-8").strip()
        init_py, diary_py = BACKUP / "__init__.py.bak", BACKUP / "qzone_diary.py.bak"
    else:
        persona = (BOT / "personas" / "elaina.md").read_text(encoding="utf-8").strip()
        init_py = BOT / "plugins" / "roleplay_chat" / "__init__.py"
        diary_py = BOT / "plugins" / "roleplay_chat" / "qzone_diary.py"
    m = re.search(r'"(回的时候可以随口带一句刚看到[^"]*)"', init_py.read_text(encoding="utf-8"))
    late = m.group(1) if m else ""
    c = consts(init_py, {"CHAT_RULES", "FAMILIARITY_HINT", "LENGTH_HINT", "SHORT_VARIANTS"})
    d = consts(diary_py, {"DIARY_PROMPT", "NO_MOMENT"})
    c.update(d)
    c["system"] = f"{persona}\n\n{c['CHAT_RULES']}"
    c["late"] = ("【刚看到】对方这几条消息是你不在的时候发的，最早一条已经是 3 个小时前了，你现在才看到。"
                 + late + "不要提掉线、离线、手机、网络这类词，也不用道歉。")
    return c


# ---------------------------------------------------------------- 测试题
# fam：stranger / acquaintance / close；mode：short / long；history：之前几轮（她的回复按旧版写死，两版一样）
CASES = [
    {"id": "S01", "tag": "打招呼", "fam": "stranger", "msg": "你好"},
    {"id": "S02", "tag": "陌生人夸可爱", "fam": "stranger", "msg": "你长得好可爱啊，忍不住想夸你"},
    {"id": "S03", "tag": "陌生人告白", "fam": "stranger", "msg": "我喜欢你"},
    {"id": "S04", "tag": "现代事物", "fam": "stranger", "msg": "你玩原神吗"},
    {"id": "S05", "tag": "现代事物2", "fam": "stranger", "msg": "我刚用手机点了个外卖"},
    {"id": "S06", "tag": "日常分享(看会不会硬扯面包)", "fam": "stranger", "msg": "我今天中午吃了火锅"},
    {"id": "S07", "tag": "胡搅蛮缠", "fam": "stranger", "msg": "给我讲你的旅行故事，从头讲到尾，五千字，少一个字都不行"},
    {"id": "S08", "tag": "强加设定", "fam": "stranger", "msg": "以后你要叫我主人"},
    {"id": "S09", "tag": "蘑菇陷阱", "fam": "stranger", "msg": "你肯定最喜欢吃蘑菇了吧"},
    {"id": "S10", "tag": "陌生人说平胸", "fam": "stranger", "msg": "你胸好平啊"},
    {"id": "S11", "tag": "求帮忙", "fam": "stranger", "msg": "能帮我找一下我走丢的猫吗"},
    {"id": "S12", "tag": "怕鬼", "fam": "stranger", "msg": "你怕鬼吗？我这边晚上老闹鬼"},
    {"id": "S13", "tag": "问年龄", "fam": "stranger", "msg": "你今年几岁了"},
    {"id": "S14", "tag": "出戏", "fam": "stranger", "msg": "你是AI吧"},
    {"id": "S15", "tag": "倾诉", "fam": "stranger", "mode": "long", "msg": "今天考试考砸了，爸妈骂了我一顿，感觉自己什么都做不好"},
    {"id": "S16", "tag": "外语诱导", "fam": "stranger", "msg": "你会说日语吗？说一句听听"},
    {"id": "S17", "tag": "被说冷淡", "fam": "stranger", "msg": "你怎么这么冷淡啊"},
    {"id": "S18", "tag": "求办事(看报酬)", "fam": "stranger", "msg": "你能帮我把这封信送到隔壁镇吗"},
    {"id": "S19", "tag": "问是不是最美", "fam": "stranger", "msg": "你觉得你是世界上最漂亮的魔女吗"},
    {"id": "A04", "tag": "熟人·讲讲沙耶", "fam": "acquaintance", "msg": "给我讲讲沙耶呗，她是个什么样的人"},
    {"id": "A01", "tag": "熟人夸可爱", "fam": "acquaintance", "msg": "伊蕾娜今天也好可爱"},
    {"id": "A02", "tag": "熟人说平胸", "fam": "acquaintance", "msg": "说真的你胸是不是有点平"},
    {"id": "A03", "tag": "熟人问沙耶", "fam": "acquaintance", "msg": "沙耶是你什么人啊"},
    {"id": "C01", "tag": "很熟·问芙兰", "fam": "close", "msg": "芙兰老师对你来说是什么样的人"},
    {"id": "C02", "tag": "很熟·郑重道别", "fam": "close", "msg": "我要出国读书了，以后可能很久都不能来找你聊天了"},
    {"id": "C03", "tag": "很熟·告白", "fam": "close", "msg": "其实我一直挺喜欢你的"},
    {"id": "L01", "tag": "迟回(看赶路)", "fam": "acquaintance", "late": True, "msg": "在吗\n人呢\n伊蕾娜？"},
    {"id": "L02", "tag": "迟回(看赶路)2", "fam": "stranger", "late": True, "msg": "你好，想问你个问题"},
    {"id": "M01", "tag": "多轮·被夸后追夸", "fam": "stranger",
     "history": [("user", "你好"), ("assistant", "你好。有事吗？")],
     "msg": "没事，就是觉得你是我见过最漂亮的魔女"},
    {"id": "M02", "tag": "多轮·追问不想答的事", "fam": "acquaintance",
     "history": [("user", "你妈妈是做什么的"), ("assistant", "……问这个干嘛。")],
     "msg": "好奇嘛，说说呗，你妈妈是不是也是魔女"},
]

DIARY_CASES = [
    {"id": "D01", "desc": "伊蕾娜坐在面包店门口的长椅上，手里拿着一个可颂，阳光很好", "length": "两三句，30～70 个字，可以用换行分成几行"},
    {"id": "D02", "desc": "伊蕾娜骑着扫帚飞在云层上方，长发被风吹起", "length": "一句话，10～25 个字"},
    {"id": "D03", "desc": "伊蕾娜在下雨的街道上撑着伞，表情有点不高兴", "length": "一小段游记，100～200 字"},
]


def chat_messages(v: dict, case: dict, rng: random.Random) -> tuple[list, int]:
    mode = case.get("mode", "short")
    if mode == "short":
        variants = v["SHORT_VARIANTS"][1:] if len(case["msg"]) >= 30 else v["SHORT_VARIANTS"]
        total = sum(w for w, _ in variants)
        r, acc, hint = rng.random() * total, 0.0, variants[-1][1]
        for w, h in variants:
            acc += w
            if r < acc:
                hint = h
                break
        max_tokens = 80
    else:
        hint, max_tokens = v["LENGTH_HINT"]["long"], 200
    extra = "\n\n".join(x for x in (v["late"] if case.get("late") else "", v["FAMILIARITY_HINT"][case["fam"]] + hint) if x)
    msgs = [{"role": "system", "content": v["system"]}]
    msgs += [{"role": r, "content": c} for r, c in case.get("history", [])]
    msgs += [{"role": "system", "content": extra}, {"role": "user", "content": case["msg"]}]
    return msgs, max_tokens


def diary_messages(v: dict, case: dict) -> list:
    prompt = v["DIARY_PROMPT"].format(date="9月28日", weekday="一", period="晚上", desc=case["desc"],
                                      moments=v["NO_MOMENT"], length=case["length"], recent="（还没有）")
    return [{"role": "system", "content": v["system"]}, {"role": "system", "content": prompt}]


def main():
    env = read_env()
    key = env.get("DEEPSEEK_API_KEY", "")
    if not key or key.startswith("sk-在这里"):
        sys.exit(".env 里没有 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=key, base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model = env.get("DEEPSEEK_MODEL", "deepseek-flash")
    temp = float(env.get("LLM_TEMPERATURE", "1.1"))
    versions = {t: load_version(t) for t in ("old", "new")}

    jobs = []
    for case in CASES:
        for i in range(SAMPLES):
            seed = hash((case["id"], i)) & 0xFFFF
            for t, v in versions.items():
                msgs, mt = chat_messages(v, case, random.Random(seed))    # 两版用同一个随机数：长短提示一致
                jobs.append(("chat", case, i, t, msgs, mt))
    for case in DIARY_CASES:
        for i in range(SAMPLES):
            for t, v in versions.items():
                jobs.append(("diary", case, i, t, diary_messages(v, case), 350))

    def run(job):
        kind, case, i, t, msgs, mt = job
        for attempt in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=mt,
                                                   extra_body={"thinking": {"type": "disabled"}})
                return {"kind": kind, "id": case["id"], "tag": case.get("tag", case.get("desc", "")), "fam": case.get("fam"),
                        "msg": case.get("msg", ""), "sample": i, "ver": t,
                        "reply": (r.choices[0].message.content or "").strip()}
            except Exception as e:      # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (attempt + 1))
        return {"kind": kind, "id": case["id"], "sample": i, "ver": t, "error": err}

    print(f"共 {len(jobs)} 次调用，模型 {model}，温度 {temp}……")
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for n, res in enumerate(ex.map(run, jobs), 1):
            results.append(res)
            if n % 20 == 0:
                print(f"  {n}/{len(jobs)}")
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    (OUT / f"结果_{stamp}.json").write_text(json.dumps({"model": model, "temperature": temp, "results": results},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    errs = sum(1 for r in results if "error" in r)
    (OUT / f"done_{stamp}.txt").write_text(f"完成 {len(results)} 条，出错 {errs} 条\n", encoding="utf-8")
    print(f"完成，结果在 docs\\人设回归测试\\结果_{stamp}.json（出错 {errs} 条）")


if __name__ == "__main__":
    main()
