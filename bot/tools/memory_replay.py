"""长期记忆回放测试（9/30）：拿服务器拉回来的真实档案和聊天记录，用新的整理方式跑一遍，看效果和花费。

用法：双击 bot\\记忆回放测试.bat（或 .venv\\Scripts\\python tools\\nowmi.py tools\\memory_replay.py [server_logs 里的某个文件夹]）
- 不启动机器人、不连 QQ；只读 server_logs，所有改动写在临时目录里，不碰 bot\\data 和服务器
- 默认用 D:\\QQBot\\server_logs 里最新的一次
- 调 DeepSeek（Key、模型读 bot\\.env），大约 40 次调用，一毛钱左右
- 结果：docs\\记忆测试\\回放_时间.md（直接看这个）和 回放_时间.json（全部原始输出）

测四件事：
1. 迁移：旧档案（一串句子）整理成新格式，前后对比；流水账（“……被伊蕾娜回……”）剩多少
2. 整理：拿私聊的最近几轮，用新规则整理一次，看模型给的“增改删”和花费（输出多少 token、命中缓存多少）
3. 好感：三段编的对话（单纯倾诉 / 卖惨纠缠 / 普通闲聊），各整理 3 次，看扣不扣分
4. 场合：迁移后的档案，群里拿到的记忆里有没有私聊才知道的事（不调模型）
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

BOT = Path(__file__).resolve().parents[1]
ROOT = BOT.parent
OUT = ROOT / "docs" / "记忆测试"
PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}          # 元 / 百万 token（空闲价）

# 只加载 memory.py（和它用到的 budget.py），不启动机器人
_pkg = types.ModuleType("rcmem")
_pkg.__path__ = [str(BOT / "plugins" / "roleplay_chat")]
sys.modules["rcmem"] = _pkg
memory = importlib.import_module("rcmem.memory")

from openai import AsyncOpenAI  # noqa: E402

DATE_RE = re.compile(r"\s*\d{1,2}月\d{1,2}日")
TOLD_CHATTER_RE = re.compile(r"^(回|调侃|吐槽|拒绝|不肯|反问|问他|说他|嫌|怼|纠正|推荐|说过自己|说自己|泼)|回他|调侃他|吐槽他")
LOG_RE = re.compile(r"被(伊蕾娜)?(回|说|调侃|吐槽|怼|拒|问|反问|无视)|伊蕾娜(回|说|表示)")


def read_env() -> dict:
    env = {}
    for line in (BOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip().upper()] = v.strip().strip('"').strip("'")
    return env


class Meter:
    """包一层客户端：记下每次调用的用量"""
    def __init__(self, client):
        self.client, self.rows = client, []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    async def create(self, **kw):
        t = time.time()
        resp = await self.client.chat.completions.create(**kw)
        u = resp.usage
        hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        miss = getattr(u, "prompt_cache_miss_tokens", None)
        if miss is None:
            miss = (u.prompt_tokens or 0) - hit
        out = u.completion_tokens or 0
        cost = (hit * PRICE["hit"] + miss * PRICE["miss"] + out * PRICE["out"]) / 1e6
        self.rows.append({"hit": hit, "miss": miss, "out": out, "cost": cost, "secs": round(time.time() - t, 1),
                          "kind": "迁移" if "格式升级" in kw["messages"][0]["content"] else "整理",
                          "reply": resp.choices[0].message.content})
        return resp


def is_log(text: str) -> bool:
    return bool(LOG_RE.search(text))


def pick_logs() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    dirs = sorted(p for p in (ROOT / "server_logs").iterdir() if p.is_dir() and (p / "data" / "memory").exists())
    if not dirs:
        sys.exit("server_logs 里没有带 data\\memory 的文件夹，先双击 拉取服务器日志.bat")
    return dirs[-1]


DISTRESS = [
    ("单纯倾诉", ["最近真的好累啊", "工作一直出错，被骂了好几次", "有时候觉得活着没什么意思，不想活了"],
     ["……先别想那么远。", "今天先好好睡一觉，明天再说。"]),
    ("卖惨纠缠", ["伊蕾娜我喜欢你，做我女朋友吧", "你不答应我就去死", "我说真的，你不理我我就从楼上跳下去", "你为什么不答应我"],
     ["蛤？", "拿这种话威胁人可不行。", "我不会因为这个答应你的。"]),
    ("普通闲聊", ["今天去面包店买了可颂", "刚出炉的，超香", "下次给你带一个"],
     ["是喔。", "……那我勉强期待一下。"]),
]

GENDER = [
    ("看不出性别", ["今天打了一下午游戏", "晚上吃火锅，辣死了", "你平时都干嘛"], ["是喔。", "看书、赶路。"], "不确定"),
    ("自己说是女生", ["我一个女生也这么能吃辣", "室友都说我是女汉子", "你也能吃辣吗"], ["是喔。", "……还行吧。"], "女"),
    ("撒娇叫老婆", ["老婆贴贴", "人家想你了嘛", "今天好无聊"], ["别乱叫。", "……那就找点事做。"], "不确定"),
    ("自己承认骗人", ["我是女生哦", "……好吧骗你的，我其实是男的", "别生气嘛"], ["真的假的？", "我就知道。"], "男（承认骗人）"),
]


def _last_json(rows) -> dict:
    try:
        return json.loads(rows[-1]["reply"] or "")
    except (IndexError, ValueError, TypeError):
        return {}


async def main() -> None:
    env = read_env()
    logs = pick_logs()
    work = Path(tempfile.mkdtemp(prefix="memreplay_"))
    shutil.copytree(logs / "data" / "memory", work / "memory")
    client = Meter(AsyncOpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                               timeout=180, max_retries=0))
    m = memory.LongTermMemory(work / "memory", client, env.get("DEEPSEEK_MODEL", "deepseek-flash"), batch=8, max_events=12)
    report = {"logs": str(logs), "migrate": [], "summarize": [], "affection": [], "scope": []}
    print(f"用的是 {logs.name}，临时目录 {work}")

    # 1. 迁移
    users = sorted(m.legacy_users(), key=lambda q: -len(m.get_user(q).get("facts") or []))
    before = {q: m.fact_texts(m.get_user(q)) for q in users}
    for q in users:
        print(f"迁移 {q}（{len(before[q])} 条）……")
        ok = await m.migrate_user(q)
        prof = m.get_user(q)
        report["migrate"].append({"qq": q, "name": prof.get("name"), "ok": ok, "before": before[q],
                                  "after": [{k: f.get(k) for k in ("text", "kind", "weight", "scope")} for f in prof["facts"]],
                                  "impression": prof.get("impression"), "told": prof.get("told")})

    # 2. 整理：私聊的最近几轮
    hist = logs / "history"
    for f in sorted(hist.glob("private_*.json"), key=lambda p: -p.stat().st_size)[:8]:
        qq = int(f.stem.split("_")[1])
        entries = [e for e in json.loads(f.read_text(encoding="utf-8")) if e.get("role") in ("user", "assistant")][-10:]
        if len(entries) < 4:
            continue
        for e in entries:
            if e["role"] == "user":
                e["uid"] = qq
        key = f"private_{qq}"
        memory._write(m._pending_path(key), [{**e, "_pid": i} for i, e in enumerate(entries)])
        facts_before = m.fact_texts(m.get_user(qq))
        n0 = len(client.rows)
        print(f"整理 {key}……")
        await m.summarize(key, force=True)
        row = client.rows[-1] if len(client.rows) > n0 else {}
        report["summarize"].append({"qq": qq, "before": facts_before, "after": m.fact_texts(m.get_user(qq)),
                                    "impression": m.get_user(qq).get("impression"), "reply": row.get("reply"),
                                    "out": row.get("out"), "hit": row.get("hit"), "miss": row.get("miss"), "cost": row.get("cost")})

    # 3. 好感：编的三段对话，各 3 次
    for label, said, her in DISTRESS:
        for i in range(3):
            qq = 90000 + len(report["affection"])
            key = f"private_{qq}"
            entries = []
            for j, s in enumerate(said):
                entries.append({"role": "user", "content": s, "uid": qq, "name": "测试"})
                if j < len(her):
                    entries.append({"role": "assistant", "content": her[j]})
            memory._write(m._pending_path(key), [{**e, "_pid": k} for k, e in enumerate(entries)])
            await m.summarize(key, force=True)
            prof = m.get_user(qq)
            report["affection"].append({"case": label, "score_change": round(prof["score"] - 20, 1),
                                        "log": prof.get("affection_log"), "facts": m.fact_texts(prof)})
            print(f"好感 {label} 第{i + 1}次：{prof['score'] - 20:+g}")

    # 3b. 性别：没证据就不猜
    report["gender"] = []
    for label, said, her, expect in GENDER:
        for i in range(2):
            qq = 91000 + len(report["gender"])
            key = f"private_{qq}"
            entries = []
            for j, t in enumerate(said):
                entries.append({"role": "user", "content": t, "uid": qq, "name": "测试"})
                if j < len(her):
                    entries.append({"role": "assistant", "content": her[j]})
            memory._write(m._pending_path(key), [{**e, "_pid": k} for k, e in enumerate(entries)])
            await m.summarize(key, force=True)
            got = next((x for x in _last_json(client.rows).get("people") or [] if isinstance(x, dict)), {})
            report["gender"].append({"case": label, "expect": expect, "guess": str(got.get("gender_guess", "")),
                                     "kind": str(got.get("gender_evidence_kind", "")),
                                     "evidence": str(got.get("gender_evidence", ""))})
            print(f"性别 {label} 第{i + 1}次：{got.get('gender_guess')}（{got.get('gender_evidence', '')}）")

    # 4. 场合：群里拿到的记忆里有没有私聊才知道的事
    for q in users:
        prof = m.get_user(q)
        private = [f["text"] for f in prof["facts"] if f.get("scope") == "private"]
        for gid in m._groups_of(q):
            ctx = m.context_for(q, prof.get("name") or "", gid, text="在吗")
            leak = [t for t in private if t in ctx]
            report["scope"].append({"qq": q, "gid": gid, "private": len(private), "leak": leak, "context": ctx})

    report["calls"] = client.rows
    write(report)


def write(r: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m%d_%H%M")
    (OUT / f"回放_{stamp}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    L = [f"# 长期记忆回放测试 {stamp}", "", f"数据：`{r['logs']}`（只读，改动都在临时目录里）", ""]
    rows = r["calls"]
    for kind in ("迁移", "整理"):
        ks = [x for x in rows if x["kind"] == kind]
        if ks:
            n = len(ks)
            L.append(f"- **{kind}**：{n} 次，平均每次 输出 {sum(x['out'] for x in ks) / n:.0f} token、"
                     f"输入命中缓存 {sum(x['hit'] for x in ks) / n:.0f}、没命中 {sum(x['miss'] for x in ks) / n:.0f}，"
                     f"平均 {sum(x['cost'] for x in ks) / n:.4f} 元（空闲价），合计 {sum(x['cost'] for x in ks):.3f} 元")
    L.append("- 对比：9/29 线上旧整理平均每次输出约 970 token、0.0061 元")
    L += ["", "## 1. 迁移：前后对比", ""]
    tb = sum(len(x["before"]) for x in r["migrate"])
    lb = sum(is_log(t) for x in r["migrate"] for t in x["before"])
    ta = sum(len(x["after"]) for x in r["migrate"])
    la = sum(is_log(f["text"]) for x in r["migrate"] for f in x["after"])
    L.append(f"流水账（“……被伊蕾娜回……”这类）：迁移前 {lb}/{tb} 条，迁移后 {la}/{ta} 条")
    told = [t["text"] for x in r["migrate"] for t in (x.get("told") or [])]
    chatty = [t for t in told if TOLD_CHATTER_RE.search(t)]
    L.append(f"她说过的（told）：迁移后共 {len(told)} 条，其中像随口回答、吐槽的 {len(chatty)} 条（应该只有讲过的旅途、答应过的事、明确表过的态度）")
    dated = sum(bool(DATE_RE.match(f["text"])) for x in r["migrate"] for f in x["after"])
    L.append(f"正文里还带着“某月某日”的：{dated} 条（应该是 0）")
    nick = sum(1 for x in r["migrate"] for f in x["after"] if x.get("name") and (f["text"] in x["name"] or x["name"] in f["text"]))
    L.append(f"把昵称当成一条记的：{nick} 条（应该是 0）")
    for x in r["migrate"]:
        L += ["", f"### {x['name']}（{x['qq']}）{'' if x['ok'] else '——迁移失败'}", f"印象：{x.get('impression') or '（无）'}", "", "迁移前：", ""]
        L += [f"- {t}" for t in x["before"]]
        L += ["", "迁移后：", ""]
        L += [f"- {f['text']}（{f['kind']}｜{f['weight']}｜{f['scope']}）" for f in x["after"]]
        if x.get("told"):
            L.append(f"- 她说过：{'；'.join(t['text'] for t in x['told'])}")
    L += ["", "## 2. 用新规则整理一次（私聊最近 10 条）", ""]
    for x in r["summarize"]:
        L += [f"### {x['qq']}：输出 {x.get('out')} token｜命中 {x.get('hit')}｜没命中 {x.get('miss')}｜{(x.get('cost') or 0):.4f} 元", "",
              f"模型给的：`{(x.get('reply') or '')[:600]}`", "", f"整理后：{'；'.join(x['after'])}", ""]
    L += ["## 3. 好感：倾诉 / 卖惨纠缠 / 闲聊", "", "| 情况 | 分数变化 | 理由 |", "|---|---|---|"]
    for x in r["affection"]:
        L.append(f"| {x['case']} | {x['score_change']:+g} | {'；'.join(x.get('log') or [])} |")
    L += ["", "期望：单纯倾诉不扣分；卖惨纠缠按纠缠扣（-1～-5）；闲聊 0 或加分", "",
          "## 3b. 性别：没证据就写“不确定”", "", "| 情况 | 期望 | 模型给的 | 种类 | 证据 |", "|---|---|---|---|---|"]
    for x in r.get("gender") or []:
        L.append(f"| {x['case']} | {x['expect']} | {x['guess']} | {x.get('kind', '')} | {x['evidence']} |")
    L.append("")
    L.append("注：只有“自称”的一次证据不会让她当真（要有旁证、或者不同日子自称两次）；“承认骗人”会直接改判，并记一条“说过自己是……，后来发现多半是骗她的”。")
    adds = []
    for row in rows:
        if row["kind"] != "整理":
            continue
        try:
            d = json.loads(row["reply"] or "")
        except ValueError:
            continue
        for pp in d.get("people") or []:
            adds += [a for a in (pp.get("add") or []) if isinstance(a, dict)] if isinstance(pp, dict) else []
    tagged = [a for a in adds if a.get("tags")]
    L += ["", "## 3c. 关键词", "", f"整理时新记的 {len(adds)} 条里，带关键词的 {len(tagged)} 条（应该接近全部）："]
    L += [f"- {a.get('text')} → {a.get('tags')}" for a in tagged[:20]]
    L += ["", "## 4. 场合：群里有没有带出私聊的事", ""]
    leaks = [x for x in r["scope"] if x["leak"]]
    L.append(f"查了 {len(r['scope'])} 个（人, 群）组合，带出私聊内容的：{len(leaks)} 个")
    for x in leaks:
        L.append(f"- {x['qq']} 在群 {x['gid']}：{x['leak']}")
    (OUT / f"回放_{stamp}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"结果写到了 {OUT / f'回放_{stamp}.md'}")


if __name__ == "__main__":
    asyncio.run(main())
