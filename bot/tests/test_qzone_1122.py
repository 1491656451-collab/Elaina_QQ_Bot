"""空间 agent 11/22：说说配图交给识图时带上说说原文（《交接-说说配文给识图-空间-1122》）"""
import pytest


@pytest.fixture
def qd():
    import plugins.roleplay_chat as p
    return p.qzone_diary


async def test_pic_hint_passes_caption(qd, monkeypatch):
    got = []
    async def describe(data, caption=None):
        got.append((data, caption)); return "两只拓印的兔子"
    monkeypatch.setattr(qd.vision, "describe", describe)
    monkeypatch.setattr(qd.cfg, "vision_enabled", True)
    assert await qd._pic_hint(["http://x/1.jpg"], "兔？！") == "（配图：两只拓印的兔子）"
    assert got[-1] == ({"url": "http://x/1.jpg"}, "兔？！")


async def test_pic_hint_without_text_old_call(qd, monkeypatch):
    """没有原文时照老样子只传一个参数（替换 describe 的老假函数也不会报错）"""
    async def describe(data):
        return "一张图"
    monkeypatch.setattr(qd.vision, "describe", describe)
    monkeypatch.setattr(qd.cfg, "vision_enabled", True)
    assert await qd._pic_hint(["http://x/1.jpg"]) == "（配图：一张图）"
    assert await qd._pic_hint(["http://x/1.jpg"], "  ") == "（配图：一张图）"
    assert await qd._pic_hint([], "兔") == ""


import types


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason="stop")],
                                 usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=0, total_tokens=15))


POST = {"content": "兔？！", "tid": "t1", "created_time": 0, "pic": [{"url1": "http://x/rabbit.jpg"}]}


@pytest.fixture
def env(qd, monkeypatch):
    sent = []
    async def create(**kw):
        sent.append(kw["messages"]); return _resp("这两只兔子挺可爱的")
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    monkeypatch.setattr(qd, "familiarity_of", lambda q: "stranger")
    monkeypatch.setattr(qd, "rate_limited", lambda *a: None)
    monkeypatch.setattr(qd.budget, "track", lambda *a, **k: None)
    monkeypatch.setattr(qd, "post_pics", lambda d: ["http://x/rabbit.jpg"] if d.get("pic") else [])
    asked = []
    async def image_parts(datas, limit=2):
        asked.append(datas); return [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]
    monkeypatch.setattr(qd.vision, "image_parts", image_parts)
    return sent, asked


async def test_comment_gets_original_image(qd, monkeypatch, env):
    """11/22 用户定：要评论时才给原图（决定赞 / 评那步只给描述）"""
    sent, asked = env
    monkeypatch.setattr(qd.cfg, "vision_chat_sees_image", True)
    out = await qd._respond(1, "阿明", "兔？！", "场景", qd._post_image_datas(POST))
    assert out and asked == [[{"url": "http://x/rabbit.jpg"}]]
    last = sent[-1][-1]
    assert isinstance(last["content"], list) and last["content"][0] == {"type": "text", "text": "兔？！"}
    assert last["content"][1]["type"] == "image_url"
    assert "【原图】" in sent[-1][-2]["content"]


async def test_no_image_when_switch_off_or_no_pic(qd, monkeypatch, env):
    sent, asked = env
    monkeypatch.setattr(qd.cfg, "vision_chat_sees_image", False)
    assert qd._post_image_datas(POST) == []
    monkeypatch.setattr(qd.cfg, "vision_chat_sees_image", True)
    assert qd._post_image_datas({"content": "没图"}) == []
    await qd._respond(1, "阿明", "没图", "场景", [])
    assert asked == [] and isinstance(sent[-1][-1]["content"], str) and "【原图】" not in sent[-1][-2]["content"]


async def test_decide_has_no_image(qd, monkeypatch, env):
    sent, asked = env
    monkeypatch.setattr(qd.cfg, "vision_chat_sees_image", True)
    async def create(**kw):
        sent.append(kw["messages"]); return _resp('{"like": true, "comment": true}')
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    await qd.decide_friend_post(1, "阿明", "兔？！", "（配图：两只兔子）", "刚才")
    assert asked == [] and all(isinstance(m["content"], str) for m in sent[-1])


async def test_comment_friend_post_passes_image(qd, monkeypatch):
    got = []
    async def respond(uin, nick, text, scene, image_datas=None):
        got.append(image_datas); return "不评"
    monkeypatch.setattr(qd, "_respond", respond)
    monkeypatch.setattr(qd, "write_block", lambda: "")
    monkeypatch.setattr(qd, "familiarity_of", lambda q: "stranger")
    monkeypatch.setattr(qd, "post_pics", lambda d: ["http://x/rabbit.jpg"])
    monkeypatch.setattr(qd.cfg, "vision_chat_sees_image", True)
    await qd.comment_friend_post(1, 100000002, "阿明", POST, "（配图：两只兔子）")
    assert got == [[{"url": "http://x/rabbit.jpg"}]]
