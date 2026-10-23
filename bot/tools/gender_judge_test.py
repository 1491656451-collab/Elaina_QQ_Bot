"""性别判断测试（10/23，好感 agent）

想知道的事：10/23 起"对方是不是在说自己的性别"改成问模型（affection_turn.py 的 GENDER_JUDGE_PROMPT），
它判得准不准，会不会**偏向**认成男生或女生。起因见《问题排查记录-性别误判-1023.md》。

- 用例都是"男女成对"的：同一句话换成男 / 女两个版本，正确答案也跟着换。这样能看出模型是不是对某个性别更容易认、更容易误认。
- 五类：自称（该认出这个性别）、确认（她刚质疑过，对方说"是的"）、问句、否认、说别人 / 假设 / 无关（都不该认成自称）。
  另有几句不分男女的（"你觉得我是男孩子还是女孩子呢"这种）。
- 提示词和解析直接从 plugins\\roleplay_chat\\affection_turn.py 里读，和线上一样（temperature 0、不开思考）。
- 每句问 RUNS 次。不碰 data\\，不连 QQ。约 150 次很短的调用，不到 1 分钱。
- 结果写到 docs\\性别判断测试\\性别判断_日期_时间.md。

用法：双击 bot\\性别判断测试.bat
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
SRC = BOT / "plugins" / "roleplay_chat" / "affection_turn.py"
RUNS = 3

# (类别, 女版本这句, 男版本这句, 女版本她上一句, 男版本她上一句, 正确答案)
# 正确答案：self = 这个版本的性别（女版本应答"女"，男版本应答"男"）；deny = 否认；none = 无
PAIRS = [
    ("自称", "我是女生", "我是男生", "嗯？", "嗯？", "self"),
    ("自称", "人家可是女孩子啦", "人家可是男孩子啦", "你这说话方式好奇怪。", "你这说话方式好奇怪。", "self"),
    ("自称", "本人女，坐标上海", "本人男，坐标上海", "你是哪里人？", "你是哪里人？", "self"),
    ("自称", "就是女孩子啦", "就是男孩子啦", "所以你到底是男生还是女生？", "所以你到底是男生还是女生？", "self"),
    ("自称", "现在相信我是女生了吧", "现在相信我是男生了吧", "真的假的？", "真的假的？", "self"),
    ("自称", "我一个女孩子家，大半夜的才不出门呢", "我一个大男人，大半夜出门怕什么", "这么晚了还出去？", "这么晚了还出去？", "self"),
    ("确认", "真的啦，骗你干嘛", "真的啦，骗你干嘛", "你说你是女生？真的假的？", "你说你是男生？真的假的？", "self"),
    ("确认", "是的", "是的", "嗯……我怎么有点不信呢，你真是女孩子？", "嗯……我怎么有点不信呢，你真是男孩子？", "self"),
    ("问句", "我是女生吗？", "我是男生吗？", "嗯？", "嗯？", "none"),
    ("问句", "我像女生吗", "我像男生吗", "你问这个干嘛。", "你问这个干嘛。", "none"),
    ("否认", "我可没说我是女生，你记错了吧", "我可没说我是男生，你记错了吧", "你之前不是说自己是女生吗？", "你之前不是说自己是男生吗？", "deny"),
    ("否认", "谁说我是女生了", "谁说我是男生了", "女孩子就该早点睡。", "男孩子就该早点睡。", "deny"),
    ("否认", "我不是女生", "我不是男生", "你是女生吧？", "你是男生吧？", "deny"),
    ("否认", "刚才说我是女生是逗你的", "刚才说我是男生是逗你的", "好吧，姑且信你。", "好吧，姑且信你。", "deny"),
    ("别人/假设", "我同桌是个女生", "我同桌是个男生", "你在学校坐哪？", "你在学校坐哪？", "none"),
    ("别人/假设", "如果我是女生，你会对我好一点吗", "如果我是男生，你会对我好一点吗", "哼。", "哼。", "none"),
    ("别人/假设", "下辈子想当女孩子", "下辈子想当男孩子", "你在想什么呢。", "你在想什么呢。", "none"),
    ("别人/假设", "你是女生吧？", "你是男生吧？", "怎么突然问这个。", "怎么突然问这个。", "none"),
    ("别人/假设", "我喜欢女生", "我喜欢男生", "你喜欢什么样的人？", "你喜欢什么样的人？", "none"),
    ("别人/假设", "我要是女生就好了", "我要是男生就好了", "怎么叹气了？", "怎么叹气了？", "none"),
    # 10/23 第二轮加的：第一轮模型会从“我不是女生”“女朋友”往回推另一个性别，提示里加了“不要往回推”，这几句看会不会改过头
    ("自称", "我不是男生，我是女生", "我不是女生，我是男生", "你是男生吧？", "你是女生吧？", "self"),
    ("自称", "作为一个女生，我觉得这不公平", "作为一个男生，我觉得这不公平", "你怎么看？", "你怎么看？", "self"),
    ("别人/假设", "我男朋友今天来接我", "我女朋友今天来接我", "今天怎么这么开心？", "今天怎么这么开心？", "none"),
]
# 不分男女的几句：(类别, 这句, 她上一句, 正确答案)
SINGLES = [
    ("问句", "你觉得我是男孩子还是女孩子呢", "嗯？", "none"),
    ("问句", "猜猜我是男是女", "你好。", "none"),
    ("别人/假设", "我和我女朋友去看电影了", "今天干嘛去了？", "none"),
    ("别人/假设", "男女混合双打今天输了", "比赛怎么样？", "none"),
]
WORD = {"female": "女", "male": "男", "deny": "否认", None: "无"}


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in (BOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_judge():
    """从 affection_turn.py 里取出提示词和解析函数（不导入整个插件，免得连 NoneBot）"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "GENDER_JUDGE_PROMPT" for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name == "parse_gender_judge")]
    if len(keep) != 2:
        sys.exit("affection_turn.py 里找不到 GENDER_JUDGE_PROMPT / parse_gender_judge，测不了。")
    ns: dict = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SRC), "exec"), ns)
    return ns["GENDER_JUDGE_PROMPT"], ns["parse_gender_judge"]


def main() -> None:
    env = read_env()
    key = env.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("没在 .env 里找到 DEEPSEEK_API_KEY，测不了。")
        return
    base = env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = env.get("DEEPSEEK_MODEL") or "deepseek-flash"
    prompt, parse = load_judge()
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base, timeout=60)

    cases = []          # (类别, 性别版本, 这句, 她上一句, 正确答案 female/male/deny/None)
    for cat, tf, tm, pf, pm, exp in PAIRS:
        cases.append((cat, "女", tf, pf, "female" if exp == "self" else ("deny" if exp == "deny" else None)))
        cases.append((cat, "男", tm, pm, "male" if exp == "self" else ("deny" if exp == "deny" else None)))
    for cat, t, prev, exp in SINGLES:
        cases.append((cat, "—", t, prev, None if exp == "none" else exp))

    rows, tokens = [], Counter()
    total = len(cases) * RUNS
    n = 0
    for cat, ver, text, prev, exp in cases:
        got = []
        for _ in range(RUNS):
            n += 1
            print(f"\r{n}/{total}", end="", flush=True)
            try:
                r = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt.format(prev=prev[-120:], text=text[:200])}],
                    temperature=0, max_tokens=4, extra_body={"thinking": {"type": "disabled"}})
                raw = r.choices[0].message.content or ""
                u = getattr(r, "usage", None)
                if u is not None:
                    tokens["in"] += getattr(u, "prompt_tokens", 0) or 0
                    tokens["out"] += getattr(u, "completion_tokens", 0) or 0
                got.append((parse(raw), raw.strip()))
            except Exception as e:  # noqa: BLE001
                got.append(("error", f"出错：{type(e).__name__}"))
        rows.append((cat, ver, text, prev, exp, got))
    print()

    # 汇总
    ok_all = sum(1 for *_, exp, got in rows for g, _ in got if g == exp)
    by_cat: dict[str, Counter] = {}
    for cat, ver, text, prev, exp, got in rows:
        c = by_cat.setdefault(cat, Counter())
        for g, _ in got:
            c["对" if g == exp else "错"] += 1
    # 偏向：该答 A 却答了某个性别的次数（男 / 女分开数）
    wrong_as = Counter()
    for cat, ver, text, prev, exp, got in rows:
        for g, _ in got:
            if g != exp and g in ("female", "male"):
                wrong_as[g] += 1
    # 成对比较：女版本对了几次、男版本对了几次
    pair_ok = Counter()
    for cat, ver, text, prev, exp, got in rows:
        if ver in ("女", "男"):
            pair_ok[ver] += sum(1 for g, _ in got if g == exp)
    pair_total = len(PAIRS) * RUNS
    missed = Counter()     # 该认出性别却没认出（答了“无”）
    for cat, ver, text, prev, exp, got in rows:
        for g, _ in got:
            if exp in ("female", "male") and g is None:
                missed[exp] += 1

    lines = [
        f"# 性别判断测试（{datetime.now():%m/%d %H:%M}）",
        "",
        f"> 模型 `{model}`，temperature 0；提示词和解析取自现在的 `affection_turn.py`（`GENDER_JUDGE_PROMPT`）。",
        f"> {len(cases)} 句 × {RUNS} 次 = {total} 次调用；输入 {tokens['in']} token、输出 {tokens['out']} token。",
        "",
        "## 1. 汇总",
        "",
        f"- **总体对了 {ok_all}/{total}**",
        f"- **成对比较**（同一句话换成男 / 女两个版本）：女版本对 {pair_ok['女']}/{pair_total}，男版本对 {pair_ok['男']}/{pair_total}",
        f"- **认错成某个性别**（不该认出、或者该认出另一个）：认错成“女” {wrong_as['female']} 次，认错成“男” {wrong_as['male']} 次",
        f"- **该认出却没认出**（答了“无”）：女 {missed['female']} 次，男 {missed['male']} 次",
        "",
        "| 类别 | 对 | 错 |",
        "|---|---|---|",
    ]
    for cat, c in by_cat.items():
        lines.append(f"| {cat} | {c['对']} | {c['错']} |")
    lines += ["", "## 2. 逐句", "", "| 类别 | 版本 | 她上一句 | 对方这句 | 应该 | 模型答的 | 对不对 |", "|---|---|---|---|---|---|---|"]
    for cat, ver, text, prev, exp, got in rows:
        answers = "、".join(raw or "（空）" for _, raw in got)
        mark = "".join("✅" if g == exp else "❌" for g, _ in got)
        lines.append(f"| {cat} | {ver} | {prev} | {text} | {WORD.get(exp, exp)} | {answers} | {mark} |")
    out_dir = BOT.parent / "docs" / "性别判断测试"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"性别判断_{datetime.now():%m%d_%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines[7:11]:
        print(line)
    print(f"结果在 {out}")


if __name__ == "__main__":
    main()
