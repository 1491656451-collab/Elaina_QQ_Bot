"""QQ 空间网页接口（发说说、上传图片、读评论、回复评论、删说说）

这些不是腾讯的公开接口，是 QQ 空间网页版自己用的接口，登录态直接向 NapCat 要
（OneBot 的 get_cookies），不用抓包。接口地址和参数参考了社区里几个开源插件的实测结论：
  - 回复评论的接口成功时也只回一段 HTML，不是 JSON，所以是否成功要回查评论详情来确认；
  - 空间没有真正的“楼中楼套楼”：回复楼里任何一条，commentId 都填这一楼顶层评论的 tid，
    再在正文前加 @{uin:…,nick:…,who:1,auto:1} 表示在回谁。

所有请求都会写一行日志到 data/qzone/qzone.log（不含 cookie），接口出问题时拿这个排查。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Awaitable, Callable

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

URL_UPLOAD = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
URL_PUBLISH = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
URL_DELETE = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
URL_LIST = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
URL_DETAIL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
URL_FEEDS = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
URL_REPLY = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"

# 说说可见范围（ugc_right）
RIGHT_PUBLIC = 1        # 所有人可见
RIGHT_FRIENDS = 4       # QQ 好友可见
RIGHT_SELF = 64         # 仅自己可见

_MENTION = re.compile(r"@\{uin:(\d+),nick:([^,}]*)[^}]*\}\s*")
_EM = re.compile(r"\[em\].*?\[/em\]")
_JSONP = re.compile(r"^[^(){]{0,64}\(\s*(\{.*\})\s*\)\s*;?\s*$", re.S)
_LOGIN_HINTS = ("ptlogin", "请先登录", "请登录", "重新登录", "登录态已失效", "登录已失效")
_VERIFY_HINTS = ("安全验证", "验证码", "身份验证", "操作过于频繁", "操作频繁", "异常访问")


class QzoneError(Exception):
    """kind：login（登录态失效）/ verify（验证、风控页）/ forbidden（403）/ page（回的是页面）/ api（业务错误）/ format"""

    def __init__(self, kind: str, msg: str, snippet: str = ""):
        super().__init__(msg)
        self.kind, self.msg, self.snippet = kind, msg, snippet

    def __str__(self) -> str:
        return f"[{self.kind}] {self.msg}"


def gtk_of(key: str) -> str:
    h = 5381
    for ch in key:
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def mentions_in(content: str) -> list[int]:
    return [int(m.group(1)) for m in _MENTION.finditer(content or "")]


def plain_text(content: str) -> str:
    """去掉 @标记 和 [em] 表情，给模型看的正文"""
    text = _MENTION.sub(lambda m: f"@{m.group(2)} ", content or "")
    return _EM.sub("", text).strip()


def _snip(text: str, n: int = 400) -> str:
    return (text or "")[:n].replace("\r", "").replace("\n", "\\n")


def parse_body(text: str) -> dict:
    """空间接口的返回：可能是 JSON、JSONP、带 undefined 的 JS 对象，或者整页 HTML"""
    if not text or not text.strip():
        raise QzoneError("format", "响应为空")
    s = text.strip()
    # 有些接口（删说说、回复评论）回的是一段 HTML，数据包在 frameElement.callback({...}) 里：先把它挖出来
    if "callback(" in s and not s.startswith("{"):
        dec = json.JSONDecoder()
        for m in reversed(list(re.finditer(r"callback\(\s*", s))):
            if s[m.end():m.end() + 1] == "{":
                try:
                    data, _ = dec.raw_decode(s[m.end():])
                    if isinstance(data, dict):
                        return data
                except ValueError:
                    pass
    m = _JSONP.match(s)
    body = m.group(1) if m else ""
    if not body:
        a, b = s.find("{"), s.rfind("}")
        body = s[a:b + 1] if a != -1 and b > a else ""
    if body:
        for cand in (body, re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)", r'\1"\2"\3', body)):
            try:
                data = json.loads(cand.replace("undefined", "null"))
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
    low = s.lower()
    if any(h in low for h in _LOGIN_HINTS):
        raise QzoneError("login", "登录态失效", _snip(s))
    if any(h in s for h in _VERIFY_HINTS):
        raise QzoneError("verify", "空间返回了验证/风控页面", _snip(s))
    if any(h in low for h in ("<html", "<script", "document.domain", "frameelement")):
        raise QzoneError("page", "返回的是页面而不是数据", _snip(s))
    raise QzoneError("format", "响应格式无法识别", _snip(s))


# ------------------------------------------------------------------ 评论结构
@dataclass
class Comment:
    tid: str                  # 评论 id（顶层评论和楼里的回复各自编号）
    uin: int
    nick: str
    content: str              # 原始正文（含 @{…} 标记）
    time: int
    replies: list["Comment"] = field(default_factory=list)   # 只有顶层评论有

    @property
    def text(self) -> str:
        return plain_text(self.content)

    @property
    def mentions(self) -> list[int]:
        return mentions_in(self.content)

    @classmethod
    def from_raw(cls, raw: dict, top: bool = True) -> "Comment":
        c = cls(
            tid=str(raw.get("tid") or raw.get("commentid") or "").strip(),
            uin=int(raw.get("uin") or 0),
            nick=str(raw.get("name") or raw.get("nickname") or "").strip(),
            content=str(raw.get("content") or ""),
            time=int(raw.get("create_time") or raw.get("createTime") or 0),
        )
        if top:
            for r in raw.get("list_3") or []:
                if isinstance(r, dict):
                    c.replies.append(cls.from_raw(r, top=False))
        return c


def parse_comments(items) -> list[Comment]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            c = Comment.from_raw(it)
            if c.tid:
                out.append(c)
    return out


def addressed_to_me(me: int, root: Comment, item: Comment | None = None) -> bool:
    """这条评论是不是在直接跟她说话（item 为 None 时判断顶层评论 root 本身）

    - 顶层评论：没 @ 别人就是在跟她（说说作者）说；@ 了别人而没 @ 她，就是在跟别人说。
    - 楼里的回复：@ 了她 → 是；@ 了别人 → 不是；
      没 @ 任何人时，空间默认是回这一楼的楼主——
      楼主本人接着说、而且她在这楼里回过、她的回复在这条之前 → 算在跟她说；
      其他人没 @ 就是在跟楼主说，不插嘴。
    """
    c = item or root
    if c.uin == me or not c.uin:
        return False
    ms = c.mentions
    if me in ms:
        return True
    if ms:
        return False
    if item is None:
        return True
    if item.uin != root.uin:
        return False
    mine_before = [r for r in root.replies if r.uin == me and r.time <= item.time]
    return bool(mine_before)


# ------------------------------------------------------------------ 客户端
@dataclass
class _Ctx:
    uin: int
    skey: str
    p_skey: str
    at: float

    @property
    def gtk(self) -> str:
        return gtk_of(self.p_skey or self.skey)

    def cookie_header(self) -> str:
        return f"uin=o{self.uin}; p_uin=o{self.uin}; skey={self.skey}; p_skey={self.p_skey}"


_LIMIT_CODES = (-10049,)                       # 实测：-10049「使用人数过多，请稍后再试」= 这个号被空间限流了
_LIMIT_WORDS = ("使用人数过多", "操作过于频繁", "操作频繁", "请稍后再试")


class Qzone:
    # 登录态一直复用，接口说失效了才重新向 NapCat 要（以前每 30 分钟要一次，等于 QQ 客户端每半小时去服务器要一次授权，不自然）
    CTX_TTL = 0

    def __init__(self, cookie_getter: Callable[[], Awaitable[str]], log_path: Path):
        self._get_cookie = cookie_getter
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._ctx: _Ctx | None = None
        self._lock = asyncio.Lock()
        self.cookie_source = ""
        # 风控信号（限流码、验证页、403）出现时调用：await on_risk(种类, 说明)。由空间日记模块接上“熔断”
        self.on_risk: Callable[[str, str], Awaitable[None]] | None = None
        self.last_write: tuple[float, str] | None = None     # 最近一次写操作（时间, 做了什么），对照下线时间用

    # -------------------------------------------------------------- 登录态
    async def ctx(self, refresh: bool = False) -> _Ctx:
        async with self._lock:
            if refresh or not self._ctx or (self.CTX_TTL and time.time() - self._ctx.at > self.CTX_TTL):
                raw = await self._get_cookie()
                jar = {k: v.value for k, v in SimpleCookie(raw).items()}
                uin = str(jar.get("uin") or jar.get("p_uin") or "").lstrip("oO").lstrip("0")
                skey = jar.get("skey", "")
                if not uin.isdigit() or not skey:
                    raise QzoneError("login", f"NapCat 返回的 cookie 里没有 uin/skey（字段：{sorted(jar)}）")
                if not jar.get("p_skey"):
                    self._log("cookie", "-", 0, "警告：没有 p_skey，改用 skey 算 g_tk，空间接口可能不认")
                self._ctx = _Ctx(int(uin), skey, jar.get("p_skey", "") or skey, time.time())
            return self._ctx

    # -------------------------------------------------------------- 日志
    def _log(self, op: str, url: str, status: int, note: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}｜{op}｜{url.rsplit('/', 1)[-1]}｜HTTP {status}｜{note}\n"
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    # -------------------------------------------------------------- 请求
    async def _req(self, op: str, method: str, url: str, *, params=None, data=None,
                   headers=None, timeout: float = 20, page_ok: bool = False, _retry: int = 0) -> dict:
        c = await self.ctx()
        h = {
            "User-Agent": UA,
            "Referer": f"https://user.qzone.qq.com/{c.uin}",
            "Origin": "https://user.qzone.qq.com",
            "Cookie": c.cookie_header(),
        }
        h.update(headers or {})
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as http:
                r = await http.request(method, url, params=params, data=data, headers=h)
        except httpx.HTTPError as e:
            self._log(op, url, 0, f"网络错误：{e!r}")
            raise QzoneError("network", f"网络错误：{e!r}") from e
        text = r.text
        if method == "POST":
            self.last_write = (time.time(), op)
        if r.status_code == 403:
            self._log(op, url, 403, f"被拒绝｜{_snip(text, 200)}")
            await self._risk("forbidden", f"{op}：请求被拒绝（403）")
            raise QzoneError("forbidden", "请求被拒绝（403）", _snip(text))
        try:
            data_ = parse_body(text)
        except QzoneError as e:
            if e.kind == "page" and page_ok:
                self._log(op, url, r.status_code, f"返回页面（这个接口成功时也回页面）｜{_snip(text, 150)}")
                return {"_page": True, "_snippet": e.snippet}
            if (e.kind == "login" or r.status_code == 401) and _retry < 1:
                self._log(op, url, r.status_code, "登录态失效，重新向 NapCat 要 cookie 再试一次")
                await self.ctx(refresh=True)
                return await self._req(op, method, url, params=params, data=data, headers=headers,
                                       timeout=timeout, page_ok=page_ok, _retry=_retry + 1)
            self._log(op, url, r.status_code, f"{e}｜{e.snippet}")
            if e.kind == "verify":
                await self._risk("verify", f"{op}：空间返回了验证/风控页面")
            raise
        code = data_.get("code", data_.get("ret"))
        if code == -3000 and _retry < 1:
            self._log(op, url, r.status_code, "code=-3000 登录态失效，重取 cookie 再试")
            await self.ctx(refresh=True)
            return await self._req(op, method, url, params=params, data=data, headers=headers,
                                   timeout=timeout, page_ok=page_ok, _retry=_retry + 1)
        msg = data_.get("message") or data_.get("msg") or ""
        self._log(op, url, r.status_code, f"code={code} {msg}"[:200])
        if code in _LIMIT_CODES or (code not in (0, None) and any(w in str(msg) for w in _LIMIT_WORDS)):
            await self._risk("limit", f"{op}：code={code} {msg}")
            raise QzoneError("limit", f"被空间限流了（code={code} {msg}）")
        return data_

    async def _risk(self, kind: str, note: str) -> None:
        self._log("⚠️风控信号", "-", 0, f"{kind}｜{note}")
        if self.on_risk:
            try:
                await self.on_risk(kind, note)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- 读
    async def list_posts(self, num: int = 5) -> list[dict]:
        """自己最近几条说说（带评论）。返回原始 msglist，每项有 tid、content、created_time、cmtnum、commentlist"""
        c = await self.ctx()
        d = await self._req("读说说列表", "GET", URL_LIST, params={
            "g_tk": c.gtk, "uin": c.uin, "hostUin": c.uin, "ftype": 0, "sort": 0, "pos": 0, "num": num,
            "replynum": 100, "callback": "_preloadCallback", "code_version": 1, "format": "json",
            "need_comment": 1, "need_private_comment": 1,
        })
        if d.get("code") not in (0, None):
            raise QzoneError("api", f"读说说列表失败：code={d.get('code')} {d.get('message', '')}")
        return [m for m in d.get("msglist") or [] if isinstance(m, dict)]

    async def detail(self, tid: str, owner: int | None = None) -> dict:
        """一条说说的详情（含全部评论）。owner 是发说说的人，不填就是她自己"""
        c = await self.ctx()
        d = await self._req("读说说详情", "GET", URL_DETAIL, params={
            "g_tk": c.gtk, "uin": owner or c.uin, "tid": tid, "format": "json", "num": 100,
            "callback": "_preloadCallback", "code_version": 1, "need_comment": 1, "need_private_comment": 1,
        }, headers={"Referer": "https://user.qzone.qq.com/", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        if d.get("code") not in (0, None):
            raise QzoneError("api", f"读说说详情失败：code={d.get('code')} {d.get('message', '')}")
        return d

    async def comments_of(self, post: dict) -> list[Comment]:
        """一条说说的全部评论：列表里带的不全（cmtnum 比实际带回来的多）时再查一次详情"""
        items = post.get("commentlist") or []
        if int(post.get("cmtnum") or 0) > len(items):
            items = (await self.detail(str(post.get("tid")))).get("commentlist") or items
        return parse_comments(items)

    async def feeds_raw(self, scope: int, count: int = 10) -> str:
        """好友动态 / 与我相关（ic2 feeds3_html_more）的原始返回，先拿来看格式。scope=0 好友动态，scope=1 与我相关"""
        c = await self.ctx()
        params = {
            "uin": c.uin, "scope": scope, "view": 1, "daylist": "", "uinlist": "", "gid": "", "flag": 1,
            "filter": "all", "applist": "all", "refresh": 1, "aisortEndTime": 0, "aisortOffset": 0,
            "getAisort": 0, "aisortBeginTime": 0, "pagenum": 1, "firstGetGroup": 0, "icServerTime": 0,
            "mixnocache": 0, "scene": 0, "begintime": "", "dayspac": 5, "sidomain": "qzonestyle.gtimg.cn",
            "useutf8": 1, "outputhtmlfeed": 1, "count": count, "g_tk": c.gtk, "format": "json",
            "rd": str(time.time()), "usertime": str(int(time.time() * 1000)), "windowId": str(time.time()),
        }
        h = {"User-Agent": UA, "Referer": f"https://user.qzone.qq.com/{c.uin}", "Cookie": c.cookie_header()}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as http:
            r = await http.get(URL_FEEDS, params=params, headers=h)
        self._log(f"读动态 scope={scope}", URL_FEEDS, r.status_code, f"{len(r.text)} 字符")
        return r.text

    # -------------------------------------------------------------- 写
    async def upload_image(self, image: bytes) -> tuple[str, str]:
        c = await self.ctx()
        d = await self._req("上传图片", "POST", URL_UPLOAD, timeout=90, data={
            "filename": "filename", "uploadtype": "1", "albumtype": "7", "exttype": "0",
            "skey": c.skey, "zzpaneluin": c.uin, "zzpanelkey": "", "uin": c.uin, "p_skey": c.p_skey,
            "output_type": "json", "qzonetoken": "", "refer": "shuoshuo", "charset": "utf-8",
            "output_charset": "utf-8", "upload_hd": "1", "hd_width": "2048", "hd_height": "10000",
            "hd_quality": "96", "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                                         "http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
            "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={c.gtk}",
            "base64": "1", "picfile": base64.b64encode(image).decode(),
        })
        if d.get("ret") != 0:
            raise QzoneError("api", f"上传图片失败：ret={d.get('ret')} {d.get('msg', '')}")
        info = d.get("data") or {}
        url = str(info.get("url") or "")
        m = re.search(r"[?&]bo=([^&]+)", url)
        if not m:
            raise QzoneError("api", f"上传结果里没有 bo 参数：{url[:120]}")
        richval = ",{},{},{},{},{},{},,{},{}".format(
            info.get("albumid", ""), info.get("lloc", ""), info.get("sloc", ""), info.get("type", ""),
            info.get("height", ""), info.get("width", ""), info.get("height", ""), info.get("width", ""))
        return m.group(1), richval

    async def publish(self, text: str, images: list[bytes] | None = None, right: int = RIGHT_PUBLIC) -> str:
        """发说说，返回 tid"""
        c = await self.ctx()
        data = {
            "syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": text, "feedversion": "1",
            "ver": "1", "ugc_right": str(right), "to_sign": "0", "hostuin": c.uin, "code_version": "1",
            "format": "json", "qzreferrer": f"https://user.qzone.qq.com/{c.uin}",
        }
        if images:
            bos, rich = [], []
            for img in images:
                bo, rv = await self.upload_image(img)
                bos.append(bo)
                rich.append(rv)
            data.update(pic_bo=",".join(bos), richtype="1", richval="\t".join(rich))
        d = await self._req("发说说", "POST", URL_PUBLISH, params={"g_tk": c.gtk, "uin": c.uin}, data=data)
        tid = str(d.get("tid") or (d.get("data") or {}).get("tid") or "").strip()
        if not tid:
            raise QzoneError("api", f"发说说没有返回 tid：code={d.get('code')} {d.get('message', '')}")
        return tid

    async def delete(self, tid: str) -> None:
        c = await self.ctx()
        d = await self._req("删说说", "POST", URL_DELETE, params={"g_tk": c.gtk}, data={
            "uin": c.uin, "topicId": f"{c.uin}_{tid}__1", "feedsType": 0, "feedsFlag": 0, "feedsKey": tid,
            "feedsAppid": 311, "feedsTime": int(time.time()), "fupdate": 1, "ref": "feeds",
            "qzreferrer": f"https://user.qzone.qq.com/{c.uin}",
        })
        if d.get("code") not in (0, None):
            raise QzoneError("api", f"删说说失败：code={d.get('code')} {d.get('message', '')}")

    async def reply(self, tid: str, root: Comment, target: Comment, content: str, owner: int | None = None) -> bool:
        """回复一条评论。target 是被回复的那条（顶层评论本身，或楼里的某条回复）；owner 是发说说的人，不填就是她自己。
        接口成功时只回页面，所以发完回查详情，找到自己刚发的回复才算成功。"""
        c = await self.ctx()
        host = owner or c.uin
        body = content
        if target is not root:        # 回楼里的某个人：加上空间自己的 @ 标记，对方会收到提醒
            body = f"@{{uin:{target.uin},nick:{target.nick},who:1,auto:1}}{content}"
        start = int(time.time()) - 120
        await self._req("回复评论", "POST", URL_REPLY, params={"g_tk": c.gtk}, page_ok=True, data={
            "topicId": f"{host}_{tid}__1", "uin": c.uin, "hostUin": host, "feedsType": 100,
            "inCharset": "utf-8", "outCharset": "utf-8", "plat": "qzone", "source": "ic",
            "platformid": 52, "format": "fs", "ref": "feeds", "content": body,
            "commentId": root.tid, "commentUin": target.uin, "richval": "", "richtype": "",
            "private": "0", "paramstr": 2, "qzreferrer": f"https://user.qzone.qq.com/{c.uin}/main",
        })
        await asyncio.sleep(2)
        for thread in parse_comments((await self.detail(tid, host)).get("commentlist")):
            if thread.tid == root.tid:
                ok = any(r.uin == c.uin and (r.time == 0 or r.time >= start) for r in thread.replies)
                self._log("回复评论·回查", URL_DETAIL, 200, "找到了自己的回复" if ok else "没找到自己的回复")
                return ok
        self._log("回复评论·回查", URL_DETAIL, 200, "回查时没找到这一楼")
        return False

    async def comment(self, tid: str, owner: int, content: str) -> bool:
        """在别人的说说下发一条一级评论；发完回查，找到自己刚发的评论才算成功"""
        c = await self.ctx()
        start = int(time.time()) - 120
        await self._req("发评论", "POST", URL_REPLY, params={"g_tk": c.gtk}, page_ok=True, data={
            "topicId": f"{owner}_{tid}__1", "uin": c.uin, "hostUin": owner, "feedsType": 100,
            "inCharset": "utf-8", "outCharset": "utf-8", "plat": "qzone", "source": "ic",
            "platformid": 52, "format": "fs", "ref": "feeds", "content": content,
            "private": "0", "paramstr": 1, "qzreferrer": f"https://user.qzone.qq.com/{owner}",
        })
        await asyncio.sleep(2)
        ok = any(r.uin == c.uin and (r.time == 0 or r.time >= start)
                 for r in parse_comments((await self.detail(tid, owner)).get("commentlist")))
        self._log("发评论·回查", URL_DETAIL, 200, "找到了自己的评论" if ok else "没找到自己的评论")
        return ok

    async def about_me(self, count: int = 20) -> list[dict]:
        """“与我相关”里的说说类动态（谁、在谁的哪条说说、什么动作），见 parse_about_me"""
        return parse_about_me(await self.feeds_raw(1, count))


# ------------------------------------------------------------------ “与我相关”
# 这个接口回的是一大段 JS 对象，每条动态的 html 里是网页片段。只从里面取定位用的几样东西：
# 动作（提到我 / 评论提到我 / 回复 / 评论……）、说说是谁的、说说 tid、时间。
# 具体谁说了什么，再用说说详情接口（JSON，带 @{uin:…} 标记）去读，比从网页里抠可靠。
_ITEM = re.compile(r"\{ver:'1',appid:'(\d+)',typeid:'(\d+)',key:'([^']*)'")


def _unescape_feeds(text: str) -> str:
    return (text.replace("\\x22", '"').replace("\\x3C", "<").replace("\\/", "/")
            .replace("\\t", " ").replace("\\n", " "))


def parse_about_me(text: str) -> list[dict]:
    s = _unescape_feeds(text)
    marks = list(_ITEM.finditer(s))
    out = []
    for i, m in enumerate(marks):
        seg = s[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(s)]

        def meta(k: str) -> str:
            mm = re.search(rf"[{{,]{k}:'([^']*)'", seg)
            return mm.group(1) if mm else ""

        fd = re.search(r'name="feed_data"[^>]*>', seg)
        fd = fd.group(0) if fd else ""
        tid = re.search(r'data-tid="([^"]+)"', fd)
        owner = re.search(r'data-uin="(\d+)"', fd)
        state = re.search(r'class="\s*ui-mr10 state"\s*>\s*([^<]*?)\s*</span>', seg)
        try:
            out.append({
                "appid": m.group(1), "typeid": m.group(2), "key": m.group(3),
                "abstime": int(meta("abstime") or 0), "opuin": int(meta("opuin") or 0),
                "nick": meta("nickname"), "state": state.group(1).strip() if state else "",
                "tid": tid.group(1) if tid else "", "owner": int(owner.group(1)) if owner else 0,
            })
        except ValueError:
            continue
    return out


def mention_targets(me: int, d: dict) -> tuple[bool, list[tuple[Comment, Comment | None]]]:
    """别人的说说里，哪些地方在对她说话。d 是说说详情。
    返回 (说说正文是否 @ 了她, [(楼, 楼里的那条或 None=楼本身)])

    - 一级评论：@ 了她才算（说说不是她的，没 @ 就是在跟楼主说）
    - 楼里的回复：@ 了她 → 算；没 @ 任何人、而这一楼是她开的 → 算（在回她）；其余不插嘴
    """
    post_at_me = me in mentions_in(str(d.get("content") or ""))
    hits = []
    for root in parse_comments(d.get("commentlist")):
        if root.uin != me and me in root.mentions:
            hits.append((root, None))
        for r in root.replies:
            if r.uin == me:
                continue
            ms = r.mentions
            if me in ms or (not ms and root.uin == me):
                hits.append((root, r))
    return post_at_me, hits


def post_pics(d: dict) -> list[str]:
    urls = []
    for p in d.get("pic") or []:
        if isinstance(p, dict):
            for k in ("url2", "url3", "url1", "smallurl"):
                if p.get(k):
                    urls.append(str(p[k]))
                    break
    return urls
