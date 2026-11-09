"""
QQBot 文件夹清理（11/07，清理 agent 写）。清单和理由见 docs\\清理清单-1107.md。

不真删：把要清理的东西“挪”进 D:\\QQBot\\_待删除_1107\\（保持原来的目录结构），并记一份清单。
确认机器人、测试都正常以后，你再手动把 _待删除_1107 整个删掉（进回收站）。
挪错了可以一键还原：  python 清理-1107.py --还原

用法（一般直接双击 清理-1107.bat）：
  python 清理-1107.py                 只列出来，什么都不动（默认）
  python 清理-1107.py --档 2 --执行    挪走第 1、2 档（档是累加的：--档 3 = 1+2+3）
  python 清理-1107.py --还原           按清单把挪走的东西放回原处
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRASH = ROOT / "_待删除_1107"
MANIFEST = TRASH / "清单.json"

BAK = "docs/人设打磨备份"

# ---------------- 第 1 档：直接可删 ----------------
L1 = [
    # 服务器日志快照：留 10-27_1547、10-28_1710、10-29_2359、11-05_1610、11-06_2300 五个（题目要用的 + 覆盖 10/13 起每一天）
    ("server_logs/2026-11-01_2226", "空文件夹（那次没拉成功）"),
    ("server_logs/2026-11-05_1555", "重复快照，日志 11-05_1610 / 11-06_2300 里都有"),
    ("server_logs/2026-11-05_2246", "重复快照，日志 11-06_2300 里都有"),
    ("server_logs/2026-11-06_1751", "重复快照，日志 11-06_2300 里都有"),
    ("server_logs/2026-11-06_1824", "重复快照，日志 11-06_2300 里都有"),
    ("server_logs/2026-11-06_1912", "重复快照，日志 11-06_2300 里都有"),
    ("server_logs/2026-11-06_1914", "重复快照，日志 11-06_2300 里都有"),
    ("server_logs/2026-11-06_1937", "重复快照，日志 11-06_2300 里都有"),
    # Python 缓存，下次运行自动重建
    ("bot/plugins/roleplay_chat/__pycache__", "Python 缓存"),
    ("bot/tools/__pycache__", "Python 缓存"),
    ("bot/voice_data/tools/__pycache__", "Python 缓存"),
    (f"{BAK}/__pycache__", "Python 缓存"),
    ("voice_service/__pycache__", "Python 缓存"),
]

# ---------------- 第 2 档：没人再用的旧备份、过程稿 ----------------
_unused_tags_init = ["0929c", "0929e", "0929f", "0929h", "0929i", "0929j", "0929k", "0929l", "0929m",
                     "1001a", "1020c", "1021", "1105", "1105b", "1105c", "1105d", "1105e", "1105f"]
_unused_tags_echo = ["0929f", "0929h", "0929i", "0929j", "0929k", "0929l", "0929m",
                     "1001a", "1020c", "1021", "1105", "1105b", "1105c", "1105d", "1105e", "1105f"]
L2 = (
    [(f"{BAK}/__init__.{t}.py.bak", "中途的代码备份，没有 bat / 脚本再用它") for t in _unused_tags_init]
    + [(f"{BAK}/echocheck.{t}.py.bak", "中途的代码备份，没有 bat / 脚本再用它") for t in _unused_tags_echo]
    + [(f"{BAK}/peak.0929m.py.bak", "中途的代码备份，没有 bat / 脚本再用它")]
    + [(f"docs/{n}.md", "过程稿：最后一轮报告已覆盖") for n in [
        "人设复测报告-被夸自恋-1105_1957", "人设复测报告-被夸自恋-1105_2003",
        "人设复测报告-被夸自恋-1105_2013", "人设复测报告-被夸自恋-1105_2016",
        "人设复测报告-人机感第二批-1105_1742",
        "判断回放报告-1028_1736", "判断回放报告-1028_1755",
        "近况回放报告-1023_0417", "近况回放报告-1023_0423",
        "近况测试报告-1023_0422", "近况测试报告-1023_0443",
        "记忆改前改后对比报告-1023_2019", "性别判断测试报告-1023_1925",
        "传声倾向对比报告-1022", "好感打分对比报告-1022_2249",
        "括号规则测试报告-1020_1339", "括号规则测试报告-1020_1347",
        "传声测试报告-1020_0456", "措辞对比报告-1018_1645",
        "模型对比报告-1016_1815", "模型对比报告-1016_1825", "模型对比报告-1018_1720",
        "记忆回放测试报告-0930_0213", "记忆回放测试报告-0930_0220", "记忆回放测试报告-0930_0225",
        "记忆回放测试报告-0930_0408", "记忆回放测试报告-1004_0106",
        "记忆回归人设评估-0930_0432", "对话回放测试报告-0930_1856",
        "清理清单-1105",
    ]]
)

# ---------------- 第 3 档：退役做完的一次性实验 ----------------
_retired_bats = ["停止符实验", "温度对比", "收尾提示测试", "人设整理测试", "人设复测", "人设减肥对比", "人设减肥复测",
                 "传声倾向对比", "传声提示新旧对比", "好感打分对比", "判断回放2", "判断回放3", "判断回放4",
                 "记忆改前改后对比", "最短档对比", "括号规则测试", "问候测试", "人设回归测试"]
# 11/07 用户定：留识图完整版模型（连同用它的 识图测试.bat、量化识图模型.bat）；
# 9/30、10/18 快照已还原，对话回放测试.bat 能跑了，连同它的改前 0930a 一起留
# 只被上面这些 bat 用到的“改前”备份（人设 elaina.*.md.bak 一律不动）
_retired_tags = ["0929", "0929b", "0929d", "0929g", "1020p", "1022", "1022v", "1023o"]
L3 = (
    [(f"bot/{b}.bat", "做完的一次性实验，git 里有") for b in _retired_bats]
    + [(f"{BAK}/__init__.{t}.py.bak", "只给退役的 bat 当“改前”") for t in _retired_tags]
    + [(f"{BAK}/echocheck.{t}.py.bak", "只给退役的 bat 当“改前”") for t in _retired_tags]
    + [(f"{BAK}/prompts.1022.py.bak", "只给人设减肥对比当“改前”"),
       (f"{BAK}/__init__.py.bak", "只给人设回归测试当“改前”（9/27）"),
       (f"{BAK}/qzone_diary.py.bak", "只给人设回归测试当“改前”（9/27）"),
       ("docs/拆分前备份", "10/22 代码拆分前的备份，git 里有")]
)

# 无论清单怎么写，这些都不许动（题目在用的日志快照、识图模型、还在用的改前备份、人设备份）
PROTECT = [
    "server_logs/2026-09-30_1805", "server_logs/2026-10-18_1621", "server_logs/2026-10-27_1547",
    "server_logs/2026-10-28_1710", "server_logs/2026-10-29_2359", "server_logs/2026-11-05_1610",
    "server_logs/2026-11-06_2300", "bot/data/models", "bot/.env", "bot/personas", "server.txt",
    f"{BAK}/__init__.1001b.py.bak", f"{BAK}/__init__.1023L.py.bak", f"{BAK}/__init__.1105g.py.bak",
    f"{BAK}/__init__.0930a.py.bak", f"{BAK}/echocheck.0930a.py.bak",
]

LEVELS = {1: L1, 2: L2, 3: L3}
TITLES = {1: "第 1 档：直接可删", 2: "第 2 档：没人再用的旧备份、过程稿", 3: "第 3 档：退役做完的一次性实验"}


def size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n} B"


def resolve(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if ROOT not in p.parents:  # 防止路径写错跑出 QQBot 文件夹
        raise SystemExit(f"路径不在 {ROOT} 里面，拒绝处理：{rel}")
    for keep in PROTECT:
        k = (ROOT / keep).resolve()
        if p == k or k in p.parents or p in k.parents or p.name.startswith("elaina."):
            raise SystemExit(f"这个在保护名单里，拒绝处理：{rel}")
    return p


def preview(max_level: int) -> list[tuple[str, Path]]:
    todo, grand = [], 0
    for lv in range(1, max_level + 1):
        print(f"\n==== {TITLES[lv]} ====")
        total, n_skip = 0, 0
        for rel, why in LEVELS[lv]:
            p = resolve(rel)
            if not p.exists():
                n_skip += 1
                continue
            s = size_of(p)
            total += s
            todo.append((rel, p))
            print(f"  {human(s):>9}  {rel}    （{why}）")
        print(f"  小计 {human(total)}" + (f"；另有 {n_skip} 项已经不在了，跳过" if n_skip else ""))
        grand += total
    print(f"\n合计 {len(todo)} 项，{human(grand)}")
    return todo


def execute(todo: list[tuple[str, Path]]) -> None:
    TRASH.mkdir(exist_ok=True)
    (TRASH / ".gitignore").write_text("*\n", encoding="utf-8")  # 别让 git 把这里当成新文件
    done = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    moved, failed = 0, []
    for rel, src in todo:
        dst = TRASH / rel
        if dst.exists():
            dst = dst.with_name(dst.name + f".{datetime.now():%H%M%S}")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append({"原位置": rel, "现在在": str(dst.relative_to(ROOT)).replace("\\", "/"),
                         "时间": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
            moved += 1
        except Exception as e:  # 被占用之类：记下来，接着处理别的
            failed.append(f"{rel}：{e}")
    MANIFEST.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n挪走了 {moved} 项，都在 {TRASH} 里。")
    if failed:
        print(f"有 {len(failed)} 项没挪成（多半是文件被占用，关掉相关窗口再跑一次就行）：")
        for f in failed:
            print("  " + f)
    print("先跑一次 bot\\程序测试.bat，正常的话把 _待删除_1107 整个删掉；想撤回就运行：清理-1107.bat 还原")


def restore() -> None:
    if not MANIFEST.exists():
        print("没有找到清单，没什么可还原的。")
        return
    done = json.loads(MANIFEST.read_text(encoding="utf-8"))
    left = []
    for item in reversed(done):
        src, dst = ROOT / item["现在在"], ROOT / item["原位置"]
        if not src.exists():
            continue
        if dst.exists():
            print(f"  原位置已经有东西了，没放回：{item['原位置']}")
            left.append(item)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"  放回：{item['原位置']}")
    MANIFEST.write_text(json.dumps(list(reversed(left)), ensure_ascii=False, indent=1), encoding="utf-8")
    print("还原完了。" + ("" if left else f"现在可以把空的 {TRASH.name} 文件夹删掉。"))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="QQBot 文件夹清理（挪到 _待删除_1107，不真删）")
    ap.add_argument("--档", type=int, choices=[1, 2, 3], default=3, help="处理到第几档（累加），默认列出全部 3 档")
    ap.add_argument("--执行", action="store_true", help="真的挪走；不加就只列出来")
    ap.add_argument("--还原", action="store_true", help="把挪走的东西放回原处")
    a = ap.parse_args()
    if a.还原:
        restore()
        return
    todo = preview(a.档)
    if not a.执行:
        print("\n（只是列出来，什么都没动。要执行：加 --档 N --执行）")
        return
    if todo:
        execute(todo)


if __name__ == "__main__":
    main()
