"""给 NapCat 打小补丁，让转发的聊天记录能读出来（11/06，聊天 agent）。

原因（《问题排查记录-转发和怪话-1106.md》）：NapCat 把聊天记录里的每一条按“发送人 QQ 号”整理，
QQ 本地拿到的聊天记录里没有 QQ 号，也没有名字，整理不出来就整条丢掉，最后内容是空的。
两处补丁（各自独立，打过的不会重复打）：
  1. 整理不出来的那条不丢，改成只带昵称（QQ 号写 0）和内容。            （20:06 第一版）
  2. 本地拿到的每一条都没有发送人时，改从 QQ 服务器上重新取一遍：那边带着 QQ 号和名字；
     取不到就还用本地那份（不会比现在差）。                            （20:30 第二版：第一版补上以后，内容有了，但不知道是谁说的）

用法（服务器上）：
  python3 napcat_forward_patch.py <napcat.mjs 路径>            打补丁（第一次打之前备份成 napcat.mjs.bak_forward）
  python3 napcat_forward_patch.py <napcat.mjs 路径> --revert   恢复备份（两处一起撤掉）
改完要 sudo systemctl restart napcat。"""
import shutil
import subprocess
import sys
from pathlib import Path

PATCHES = [
    ("/*qqbot-forward-patch*/",
     'await this.parseMessage(o, "array", !0))))).filter((o) => o !== void 0);',
     '(await this.parseMessage(o, "array", !0)) ?? /*qqbot-forward-patch*/ { self_id: 0, user_id: 0, time: +(o.msgTime || 0), '
     'message_id: 0, message_type: "private", sender: { user_id: 0, nickname: o.sendNickName || o.sendRemarkName || o.sendMemberName || "" }, '
     'raw_message: "", font: 14, sub_type: "friend", message: await this.parseMessageSegments(o, !0, !1, !1).catch(() => []), '
     'message_format: "array", post_type: "message" })))).filter((o) => o !== void 0);'),
    ("/*qqbot-forward-patch-2*/",
     "let s = await this.getMultiMessages(n, o);",
     "let s = await this.getMultiMessages(n, o); /*qqbot-forward-patch-2*/ "
     "if (s && s.length && s.every((m) => (!m.senderUin || m.senderUin === \"0\") && !m.senderUid)) try { "
     "const p2 = await this.core.apis.PacketApi.pkt.operation.FetchForwardMsg(e.resId); if (p2 && p2.length) s = p2; } catch (c) { }"),
]


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
    todo = [(mark, old, new) for mark, old, new in PATCHES if mark not in s]
    if not todo:
        print("两处补丁都已经打过了，不用再打")
        return 0
    ok = []
    for mark, old, new in todo:
        if s.count(old) != 1:
            print(f"没找到补丁 {mark} 要改的那一行（找到 {s.count(old)} 处），这处不打（这个版本的 NapCat 和预想的不一样）")
        else:
            ok.append((mark, old, new))
    if not ok:
        print("一处都没法打，什么都没改")
        return 1
    todo = ok
    if not bak.exists():
        shutil.copy2(f, bak)
        print(f"已备份到 {bak}")
    tmp = s
    for mark, old, new in todo:
        tmp = tmp.replace(old, new)
    prev = f.read_bytes()
    f.write_text(tmp, encoding="utf-8")
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            f.write_bytes(prev)
            print("改完语法检查没通过，已经改回打这次补丁之前的样子：\n" + r.stderr[-500:])
            return 1
        print("语法检查通过")
    else:
        print("（没找到 node，没做语法检查）")
    print(f"打上了 {len(todo)} 处：{' '.join(m for m, _, _ in todo)}\n文件：{f}\n记得 sudo systemctl restart napcat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
