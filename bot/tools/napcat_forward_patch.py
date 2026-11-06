"""给 NapCat 打一个小补丁：转发的聊天记录里，换算不出发送人 QQ 号的那条不再丢掉（11/06，聊天 agent）。

原因（《问题排查记录-转发和怪话-1106.md》）：NapCat 把聊天记录里的每一条按“发送人 QQ 号”整理，
聊天记录里本来就没有 QQ 号，换算不出来的整条丢掉，最后内容是空的，get_forward_msg 也是空的。
补丁：这种时候不丢，改成只带昵称（发送人 QQ 号写 0）和消息内容。只改这一处。

用法（服务器上）：
  python3 napcat_forward_patch.py <napcat.mjs 路径>            打补丁（先备份成 napcat.mjs.bak_forward）
  python3 napcat_forward_patch.py <napcat.mjs 路径> --revert   恢复备份
改完要 sudo systemctl restart napcat。"""
import shutil
import subprocess
import sys
from pathlib import Path

OLD = 'await this.parseMessage(o, "array", !0))))).filter((o) => o !== void 0);'
NEW = ('(await this.parseMessage(o, "array", !0)) ?? /*qqbot-forward-patch*/ { self_id: 0, user_id: 0, time: +(o.msgTime || 0), '
       'message_id: 0, message_type: "private", sender: { user_id: 0, nickname: o.sendNickName || o.sendRemarkName || o.sendMemberName || "" }, '
       'raw_message: "", font: 14, sub_type: "friend", message: await this.parseMessageSegments(o, !0, !1, !1).catch(() => []), '
       'message_format: "array", post_type: "message" })))).filter((o) => o !== void 0);')
MARK = "/*qqbot-forward-patch*/"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    f = Path(sys.argv[1]).expanduser()
    bak = f.with_name(f.name + ".bak_forward")
    if "--revert" in sys.argv:
        if not bak.exists():
            print(f"没有备份 {bak}，不用恢复")
            return 1
        shutil.copy2(bak, f)
        print(f"已恢复 {f}，记得 sudo systemctl restart napcat")
        return 0
    s = f.read_text(encoding="utf-8")
    if MARK in s:
        print("已经打过补丁了，不用再打")
        return 0
    n = s.count(OLD)
    if n != 1:
        print(f"没找到要改的那一行（找到 {n} 处），这个版本的 NapCat 和预想的不一样，什么都没改")
        return 1
    if not bak.exists():
        shutil.copy2(f, bak)
        print(f"已备份到 {bak}")
    f.write_text(s.replace(OLD, NEW), encoding="utf-8")
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            shutil.copy2(bak, f)
            print("改完语法检查没通过，已经恢复原样：\n" + r.stderr[-500:])
            return 1
        print("语法检查通过")
    else:
        print("（没找到 node，没做语法检查）")
    print(f"补丁已打上：{f}\n记得 sudo systemctl restart napcat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
