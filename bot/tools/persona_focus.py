"""重点复测：挑几道题，改前 / 改后各多问几次（默认 10 次），看变化是不是偶然。

用法：.venv\\Scripts\\python tools\\persona_focus.py <改前版本名> [题号,题号,...] [次数]
  例：.venv\\Scripts\\python tools\\persona_focus.py 0929d N08,S17,S03 10
- 题目、提示拼法、翻车判定都和 tools\\regression.py 一样（直接调用它的函数）
- 结果：docs\\人设回归测试\\重点_时间.json 和 重点_时间.md
- 除了 regression_cases.json 里的题，也能用 tools\\persona_extra_cases.json 里人设这边自己加的题（题号不重复就行）
"""
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BEFORE = sys.argv[1] if len(sys.argv) > 1 else "0929d"
IDS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["N08", "S17", "S03", "S02", "C03", "S15", "N02", "N03", "C02"]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 10

sys.argv = [sys.argv[0], BEFORE]            # regression.py 在导入时读改前版本名
sys.path.insert(0, str(Path(__file__).resolve().parent))
import regression as rg  # noqa: E402
from openai import OpenAI  # noqa: E402


def main():
    env = rg.read_env()
    client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), timeout=60)
    model, temp = env.get("DEEPSEEK_MODEL", "deepseek-flash"), float(env.get("LLM_TEMPERATURE", "1.1"))
    cases = {c["id"]: c for c in json.loads(rg.CASES_FILE.read_text(encoding="utf-8"))["cases"]}
    extra = Path(__file__).resolve().parent / "persona_extra_cases.json"      # 人设这边自己加的题
    if extra.exists():
        cases.update({c["id"]: c for c in json.loads(extra.read_text(encoding="utf-8"))["cases"]})
    vers = {t: rg.load(t) for t in ("before", "after")}
    jobs = []
    for cid in IDS:
        c = cases[cid]
        for i in range(N):
            for t, v in vers.items():
                jobs.append((c, i, t, *rg.messages(v, c, random.Random(f"{cid}{i}"))))

    def run(job):
        c, i, t, msgs, mt = job
        err = ""
        for k in range(3):
            try:
                r = client.chat.completions.create(model=model, messages=msgs, temperature=temp, max_tokens=mt,
                                                   extra_body={"thinking": {"type": "disabled"}})
                reply = (r.choices[0].message.content or "").strip()
                return {"id": c["id"], "tag": c.get("tag", ""), "msg": c["msg"], "sample": i, "ver": t,
                        "reply": reply, "bad": rg.judge(c, reply)}
            except Exception as e:  # noqa: BLE001
                err = repr(e)
                time.sleep(3 * (k + 1))
        return {"id": c["id"], "sample": i, "ver": t, "error": err}

    print(f"共 {len(jobs)} 次调用（改前 = {BEFORE}，每题每版 {N} 次）……")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(run, jobs))
    stamp = time.strftime("%m%d_%H%M")
    rg.OUT.mkdir(parents=True, exist_ok=True)
    (rg.OUT / f"重点_{stamp}.json").write_text(json.dumps({"model": model, "before": BEFORE, "n": N, "results": res},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    ok = [r for r in res if "reply" in r]
    lines = [f"# 重点复测 {stamp}", "", f"改前 = {BEFORE}，每题每版 {N} 次，出错 {len(res) - len(ok)} 条。", "",
             "| 题 | 说明 | 改前翻车 | 改后翻车 | 改前不同说法 | 改后不同说法 |", "|---|---|---|---|---|---|"]
    for cid in IDS:
        row = [cid, cases[cid].get("tag", "")]
        for t in ("before", "after"):
            rs = [r for r in ok if r["id"] == cid and r["ver"] == t]
            row.append(f"{sum(bool(r['bad']) for r in rs)}/{len(rs)}")
        for t in ("before", "after"):
            rs = [r for r in ok if r["id"] == cid and r["ver"] == t]
            row.append(str(len({r['reply'].split(chr(10))[0].strip('。 ') for r in rs})))
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "“不同说法”按第一句去重，越接近次数越好。", ""]
    for cid in IDS:
        lines.append(f"### {cid} {cases[cid].get('tag', '')}：{cases[cid]['msg'][:40]}")
        for t, name in (("before", "改前"), ("after", "改后")):
            for r in (r for r in ok if r["id"] == cid and r["ver"] == t):
                mark = f"　〔{'；'.join(r['bad'])}〕" if r["bad"] else ""
                lines.append(f"- {name}：{r['reply'].replace(chr(10), ' / ')}{mark}")
        lines.append("")
    (rg.OUT / f"重点_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"完成，结果在 docs\\人设回归测试\\重点_{stamp}.md")


if __name__ == "__main__":
    main()
