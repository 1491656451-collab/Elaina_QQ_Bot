r"""把电脑上的人设 bot\personas\elaina.md 传到服务器（11/05，部署 agent）。

由 D:\QQBot\上传人设.bat 调用，也可以手动：bot\.venv\Scripts\python bot\tools\upload_persona.py
步骤：
  1. 比一下服务器上的和电脑上的是不是同一份（sha256），一样就不传；
  2. 不一样：先在服务器上备份旧的（elaina.md.bak_月日_时分秒，只留最近 5 份），再 scp 上传；
  3. 传完再比一次，确认服务器上的就是电脑上这份；
  4. 告诉你人设有多少字（和服务器日志“人设已加载（N 字）”对得上），以及下一步做什么。
服务器地址读 D:\QQBot\server.txt（一行，admin@IP），不写在脚本里。
不重启机器人：接着要跑 ~/update.sh 的话它会重启；只改了人设就在 QQ 里发 /重载人设。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # D:\QQBot
PERSONA = ROOT / "bot" / "personas" / "elaina.md"
REMOTE_DIR = "QQBot/bot/personas"
REMOTE_NAME = "elaina.md"
KEEP_BACKUPS = 5


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def remote_sha(server: str) -> str | None:
    """服务器上那份的 sha256；文件不存在返回 None。连不上抛 RuntimeError。"""
    r = run(["ssh", server, f"cd {REMOTE_DIR} && (sha256sum {REMOTE_NAME} 2>/dev/null || echo MISSING)"])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"ssh 退出码 {r.returncode}")
    out = r.stdout.strip().split()
    if not out or out[0] == "MISSING":
        return None
    return out[0].lower()


def main() -> int:
    server_txt = ROOT / "server.txt"
    if not server_txt.exists():
        print(f"找不到 {server_txt}，请新建这个文件，里面写一行：admin@你的服务器IP")
        return 1
    server = server_txt.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
    if not PERSONA.exists():
        print(f"找不到人设文件 {PERSONA}")
        return 1

    data = PERSONA.read_bytes()
    local_sha = hashlib.sha256(data).hexdigest()
    chars = len(data.decode("utf-8-sig").strip())       # 和机器人 load_persona 的算法一样（去掉首尾空白再数）
    print(f"电脑上的人设：{PERSONA}（{chars} 字）")

    print("\n[1/3] 看服务器上的是不是已经是这一份……")
    try:
        before = remote_sha(server)
    except RuntimeError as e:
        print(f"  连不上服务器：{e}\n  请把这个窗口截图发给 Claude")
        return 1
    if before == local_sha:
        print("  服务器上已经是这一份，不用传。")
        return 0

    print("[2/3] 备份服务器上的旧人设，然后上传……")
    if before is not None:
        backup = (f"cd {REMOTE_DIR} && b={REMOTE_NAME}.bak_$(date +%m%d_%H%M%S) && cp -p {REMOTE_NAME} $b && echo $b"
                  f" && ls -1t {REMOTE_NAME}.bak_* | tail -n +{KEEP_BACKUPS + 1} | xargs -r rm -f --")
        r = run(["ssh", server, backup])
        if r.returncode != 0:
            print(f"  备份失败，没有上传：{(r.stderr or r.stdout).strip()}\n  请把这个窗口截图发给 Claude")
            return 1
        print(f"  旧的备份成了 {REMOTE_DIR}/{r.stdout.strip().splitlines()[0]}（只留最近 {KEEP_BACKUPS} 份）")
    r = subprocess.run(["scp", str(PERSONA), f"{server}:{REMOTE_DIR}/{REMOTE_NAME}"])
    if r.returncode != 0:
        print("  上传失败（scp 出错），服务器上还是旧的那份。请把这个窗口截图发给 Claude")
        return 1

    print("[3/3] 核对服务器上的文件……")
    try:
        after = remote_sha(server)
    except RuntimeError as e:
        print(f"  传完了，但核对时连不上服务器：{e}")
        return 1
    if after != local_sha:
        print("  ！！服务器上的和电脑上的不一样，可能没传完整。请把这个窗口截图发给 Claude")
        return 1

    print(f"\n上传成功，和电脑上的一模一样（{chars} 字）。")
    print("下一步：")
    print("  · 接着要跑 ~/update.sh 的话，什么都不用做，它重启机器人时会加载新人设；")
    print("  · 只改了人设，就在 QQ 里给她发 /重载人设。")
    print(f"  · 服务器日志里应该看到“人设已加载……（{chars} 字）”。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
