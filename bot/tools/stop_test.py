"""停止符实验（10/01）：同一道题问很多次，一半开着“写到【就停”，一半不开，看模型原本想写什么、被截成了什么样。

起因：回归测试 1001_0100 里“伊蕾娜小姐中午好”有一次只回了一个“中”。怀疑是模型想接着写“【群友 → 你】……”替对方说话，
“写到【就停”在服务器那边把含“【”的那一整块都扔了，连带吃掉了“午好。”。

用法：双击 bot\\停止符实验.bat（每题默认 10 次 × 2 组，可以跟题号和次数：停止符实验.bat G11,G5 20）
结果：docs\\人设回归测试\\停止符实验_时间.md
"""
from __future__ import annotations

import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IDS = (sys.argv[1] if len(sys.argv) > 1 else "G11,G5,S01,G7").split(",")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
sys.argv = ["regression.py", "当前"]            # 只用现在的代码
sys.path.insert(0, str(Path(__file__).resolve().parent))
import regression as reg                        # noqa: E402

# 和线上 is_fragment 同一份“正常的一个字”（10/01 01:20：原来漏了“早”，把“早。”误算成半截）
OK_ONE = set("嗯哦喔噢蛤啊诶欸好行在早对是不哈呵嘿切哼咦唔呃额嗷喵噗")


def fragment(reply: str) -> bool:
    core = re.sub(r"[\s\W_]+", "", reply)
    return len(core) == 1 and core not in OK_ONE


def main():
    env = reg.read_env()
    from openai import OpenAI
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model, temp = env.get("DEEPSEEK_MODEL", "deepseek-flash"), float(env.get("LLM_TEMPERATURE", "1.1"))
    v = reg.load("after")
    cases = {c["id"]: c for c in reg.json.loads(reg.CASES_FILE.read_text(encoding="utf-8"))["cases"]}
    jobs = []
    for cid in IDS:
        c = cases[cid.strip()]
        for i in range(N):
            msgs, mt = reg.messages(v, c, random.Random(f"{cid}{i}"))
            for stop in (True, False):
                jobs.append((c, i, stop, msgs, mt))

    def run(job):
        c, i, stop, msgs, mt = job
        for k in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=mt,
                                                   stop=(v.get("CHAT_STOP") or ["【"]) if stop else None,
                                                   extra_body={"thinking": {"type": "disabled"}})
                ch = r.choices[0]
                return {"id": c["id"], "msg": c["msg"], "i": i, "stop": stop, "reply": ch.message.content or "",
                        "finish": ch.finish_reason}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "msg": c["msg"], "i": i, "stop": stop, "error": err}

    print(f"共 {len(jobs)} 次调用……")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(run, jobs))
    stamp = time.strftime("%m%d_%H%M")
    lines = [f"# 停止符实验 {stamp}", "", f"模型 {model}，温度 {temp:g}，每题每组 {N} 次。", "",
             "- **不停**：不设停止符，看模型原本想写什么；带“【”的就是想照聊天记录格式往下写（多半是替对方说话）",
             "- **写到【就停**：和线上一样；只剩一个字、又不是“嗯 / 哦 / 蛤 / 早”这种正常短回的，算“被截成半截”", "",
             "| 题 | 不停：写出“【”的 | 写到【就停：被截成半截的 |", "|---|---|---|"]
    for cid in IDS:
        a = [r for r in res if r["id"] == cid.strip() and not r["stop"] and "reply" in r]
        b = [r for r in res if r["id"] == cid.strip() and r["stop"] and "reply" in r]
        bad_b = [r for r in b if fragment(r["reply"])]
        lines.append(f"| {cid} | {sum('【' in r['reply'] for r in a)}/{len(a)} | {len(bad_b)}/{len(b)} |")
    for cid in IDS:
        lines += ["", f"## {cid}：{cases[cid.strip()]['msg']}", ""]
        for stop in (False, True):
            lines.append(f"**{'写到【就停' if stop else '不停'}**")
            lines.append("")
            for r in [r for r in res if r["id"] == cid.strip() and r["stop"] == stop]:
                txt = r.get("reply", "（出错：" + r.get("error", "") + "）").replace("\n", " / ")
                lines.append(f"- {txt or '（空）'}　〔{r.get('finish', '')}〕")
            lines.append("")
    reg.OUT.mkdir(parents=True, exist_ok=True)
    out = reg.OUT / f"停止符实验_{stamp}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果在 {out}")


if __name__ == "__main__":
    main()
