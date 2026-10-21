"""加好友（10/22 从 __init__.py 拆出来，代码原样搬过来，没改）：加好友时的验证消息、“已成功添加为好友”不回、不补"""
import json
import re
import time

from nonebot import logger, on_notice, on_request
from nonebot.adapters.onebot.v11 import NoticeEvent

from .core import BOT_DIR


# 加好友时对方填的验证消息（“我是某某”），通过以后会以对方的名义出现在私聊里；QQ 还会补一条“我们已成功添加为好友……”。
# 这些都不是在跟她说话，不回、不补。
FRIEND_REQ_FILE = BOT_DIR / "data" / "friend_requests.json"


_FRIEND_SYS_RE = re.compile(
    r"^(我们已成功添加为好友|我通过了你的(朋友|好友)验证请求|你已添加了|你们已成为好友|以上是打招呼的内容|现在可以开始聊天了|"
    r"我已经添加了你|我们已经是好友了)")


_friend_added: dict[int, float] = {}         # QQ -> 刚加上好友的时间（friend_add 通知）


def _friend_reqs() -> dict:
    try:
        return json.loads(FRIEND_REQ_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_friend_reqs(d: dict) -> None:
    now = time.time()
    d = {k: v for k, v in d.items() if now - float(v.get("at", 0)) <= 7 * 86400}   # 一周前的申请不留
    try:
        FRIEND_REQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FRIEND_REQ_FILE.with_name(FRIEND_REQ_FILE.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FRIEND_REQ_FILE)
    except OSError as e:
        logger.warning(f"记录好友申请失败：{e}")


def _norm(t: str) -> str:
    return re.sub(r"\s", "", t or "")


def is_friend_verify(qq: int, text: str, msg_time: float = 0) -> bool:
    """这条私聊是不是加好友带来的验证消息或系统提示"""
    t = _norm(text)
    if not t:
        return False
    if _FRIEND_SYS_RE.match(t):
        return True
    req = _friend_reqs().get(str(qq))
    # 和申请里的验证消息一字不差：加上好友 10 分钟内都算（之后对方真的再说一遍同样的话，就正常回）
    if req and _norm(req.get("comment")) == t and (not req.get("added") or (msg_time or time.time()) - req["added"] <= 600):
        return True
    # 申请发来时机器人没在线、没记下验证消息：刚加上好友前后 20 秒内的“我是……”也算
    added = _friend_added.get(int(qq))
    return bool(added and abs((msg_time or time.time()) - added) <= 20 and t.startswith("我是") and len(t) <= 30)


async def _is_friend_request(event) -> bool:
    return getattr(event, "post_type", "") == "request" and getattr(event, "request_type", "") == "friend"


friend_request = on_request(rule=_is_friend_request, priority=1, block=False)


@friend_request.handle()
async def _(event):
    d = _friend_reqs()
    d[str(event.user_id)] = {"comment": str(getattr(event, "comment", "") or "")[:100], "at": time.time()}
    _save_friend_reqs(d)


async def _is_friend_add(event: NoticeEvent) -> bool:
    return getattr(event, "notice_type", "") == "friend_add"


friend_add_notice = on_notice(rule=_is_friend_add, priority=1, block=False)


@friend_add_notice.handle()
async def _(event: NoticeEvent):
    _friend_added[int(event.user_id)] = float(event.time or time.time())
    d = _friend_reqs()
    if str(event.user_id) in d:
        d[str(event.user_id)]["added"] = float(event.time or time.time())
        _save_friend_reqs(d)
    logger.info(f"加了新好友：{event.user_id}")
