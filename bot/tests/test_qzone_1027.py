"""空间 agent 10/27：好友说说的点赞记进记忆、私聊带最近空间往来、发完说说给自己点赞"""
import json, time, types
import pytest


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason="stop")],
                                 usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=0, total_tokens=15))


@pytest.fixture
def qd(monkeypatch, tmp_path):
    import plugins.roleplay_chat as p
    qd = p.qzone_diary
    monkeypatch.setattr(qd, "write_block", lambda method="POST": None)
    monkeypatch.setattr(qd, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(qd, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(qd, "SELF_LIKE_FILE", tmp_path / "self_like.json")
    monkeypatch.setattr(qd, "rate_limited", lambda *a, **k: None)
    monkeypatch.setattr(qd.cfg, "qzone_friend_like", True)
    monkeypatch.setattr(qd.cfg, "vision_enabled", False)
    monkeypatch.setattr(qd.asyncio, "sleep", _nosleep)
    qd._round["n"] = 0
    return qd


async def _nosleep(*a, **k):
    return None


def _fake_qz(qd, monkeypatch, posts, decide):
    calls = {"like": [], "comment": []}
    now = int(time.time())
    feeds = [{"appid": "311", "typeid": "0", "key": t, "abstime": now - 600, "opuin": o, "nick": n, "state": "", "tid": t, "owner": o}
             for o, t, n, _ in posts]
    details = {(t, o): {"code": 0, "tid": t, "name": n, "content": c, "created_time": now - 600, "commentlist": []} for o, t, n, c in posts}

    async def ctx(refresh=False):
        return types.SimpleNamespace(uin=999000)
    async def friend_feeds(count=20):
        return feeds
    async def detail(tid, owner=None):
        return details[(tid, owner)]
    async def like(tid, owner, abstime=0):
        calls["like"].append((owner, tid)); return True
    async def comment(tid, owner, content):
        calls["comment"].append((owner, tid, content)); return True
    for k, v in dict(ctx=ctx, friend_feeds=friend_feeds, detail=detail, like=like, comment=comment).items():
        monkeypatch.setattr(qd.qz, k, v)

    async def create(**kw):
        a = json.dumps(kw["messages"], ensure_ascii=False)
        if "只回一行 JSON" in a:
            for name, d in decide.items():
                if f"刷到了「{name}」" in a:
                    return _resp(d)
            return _resp('{"like": false, "comment": false}')
        return _resp("画得真好。")
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    return calls


async def test_like_only_is_remembered(qd, monkeypatch):
    calls = _fake_qz(qd, monkeypatch, [(70001, "A1", "小甲", "今天画了一张伊蕾娜")], {"小甲": '{"like": true, "comment": false}'})
    qd.get_history("qzone_70001").clear()
    out = await qd.poll_friends()
    assert calls["like"] == [(70001, "A1")] and not calls["comment"], out
    h = qd.get_history("qzone_70001")
    assert h and "点了个赞" in h[-1]["content"] and h[-1]["role"] == "user"       # 只记她做了什么，不编她说的话
    assert "画了一张伊蕾娜" in h[-1]["content"]
    memo = qd.space_memo(70001)
    assert "点了个赞" in memo and "对方没提就别主动说" in memo


async def test_like_and_comment_one_entry(qd, monkeypatch):
    calls = _fake_qz(qd, monkeypatch, [(70002, "B1", "小乙", "海边好冷")], {"小乙": '{"like": true, "comment": true}'})
    qd.get_history("qzone_70002").clear()
    await qd.poll_friends()
    assert len(calls["like"]) == 1 and len(calls["comment"]) == 1
    h = qd.get_history("qzone_70002")
    users = [x for x in h if x["role"] == "user"]
    assert len(users) == 1 and "点了赞，还在下面评论了一句" in users[0]["content"]
    assert h[-1]["role"] == "assistant" and h[-1]["content"] == "画得真好。"
    memo = qd.space_memo(70002)
    assert "你评论的是「画得真好。」" in memo           # 10/27 聊天 agent：她自己去评论的，写成“你评论的是”


def test_space_memo_only_recent(qd):
    h = qd.get_history("qzone_70003")
    h.clear()
    old = time.time() - 72 * 3600
    h.extend([{"role": "user", "content": "【小丙】（在你的日记下评论）好看", "ts": old}, {"role": "assistant", "content": "谢谢", "ts": old}])
    assert qd.space_memo(70003) == ""
    h.extend([{"role": "user", "content": "【小丙】（在你的日记下评论）又来了", "ts": time.time() - 3600},
              {"role": "assistant", "content": "嗯", "ts": time.time() - 3600}])
    m = qd.space_memo(70003)
    assert "又来了" in m and "好看" not in m and "1 小时前" in m


async def test_self_like_after_publish(qd, monkeypatch):
    liked = []
    async def ctx(refresh=False):
        return types.SimpleNamespace(uin=999000)
    async def like(tid, owner, abstime=0):
        liked.append((tid, owner)); return True
    monkeypatch.setattr(qd.qz, "ctx", ctx)
    monkeypatch.setattr(qd.qz, "like", like)
    qd._write(qd.SELF_LIKE_FILE, {"tid": "P9", "ts": int(time.time()), "at": time.time() + 300})
    await qd._self_like_due()
    assert not liked                                    # 还没到时间
    j = qd._read(qd.SELF_LIKE_FILE, {}); j["at"] = time.time() - 1; qd._write(qd.SELF_LIKE_FILE, j)
    await qd._self_like_due()
    assert liked == [("P9", 999000)] and not qd.SELF_LIKE_FILE.exists()
    await qd._self_like_due()
    assert len(liked) == 1                              # 只点一次


async def test_publish_schedules_self_like(qd, monkeypatch):
    async def list_posts(n=1): return []
    async def publish(text, images=None, right=0): return "NEWTID"
    monkeypatch.setattr(qd.qz, "list_posts", list_posts)
    monkeypatch.setattr(qd.qz, "publish", publish)
    monkeypatch.setattr(qd.gallery, "items", {"x.jpg": object()})
    monkeypatch.setattr(qd.gallery, "image_bytes", lambda img: b"x")
    monkeypatch.setattr(qd.gallery, "mark_used", lambda name: None)
    monkeypatch.setattr(qd, "POSTS_FILE", qd.SELF_LIKE_FILE.parent / "posts.json")
    await qd.publish({"image": "x.jpg", "text": "今天", "desc": "她", "moments": []})
    j = qd._read(qd.SELF_LIKE_FILE, {})
    assert j["tid"] == "NEWTID" and 170 < j["at"] - time.time() < 610
