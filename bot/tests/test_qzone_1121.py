"""空间 agent 11/21：在别人空间里说话没礼貌、有人机感，漏出“我回一个。回复：「……」”（《问题排查记录-空间评论人机感和没礼貌-1121》）"""
import types
import pytest


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason="stop")],
                                 usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=0, total_tokens=15))


@pytest.fixture
def qd():
    import plugins.roleplay_chat as p
    return p.qzone_diary


@pytest.mark.parametrize("raw,want", [
    ("我回一个。回复：「不会的啦，多喝热水早点睡。」", "不会的啦，多喝热水早点睡。"),
    ("回一句：“恭喜米花一周年！”", "恭喜米花一周年！"),
    ("我评一句，小猫好可爱", "小猫好可爱"),
    ("这猫好可爱，恭喜一周年！", "这猫好可爱，恭喜一周年！"),
    ("说一句实话，挺好看的", "说一句实话，挺好看的"),
    ("（顺着吐槽一句好了），天上一口气挂这么多，晚上还睡得着吗", "天上一口气挂这么多，晚上还睡得着吗"),
    ("（刷到这条，忍不住想评论），掉马桶的纪念日还要庆祝啊", "掉马桶的纪念日还要庆祝啊"),
    ("（这人还真敢说啊）好看", "（这人还真敢说啊）好看"),
])
def test_unwrap_meta(qd, raw, want):
    assert qd.unwrap_meta(raw) == want
    assert not qd.meta_left(want)


def test_manners_by_tier(qd, monkeypatch):
    monkeypatch.setattr(qd, "familiarity_of", lambda q: {1: "stranger", 2: "close", 3: "friend"}.get(q, "stranger"))
    s, c, f = qd.manners_hint(1), qd.manners_hint(2), qd.manners_hint(3)
    for h in (s, c, f):
        assert "在别人空间里的分寸" in h and "别猜他为什么发这条" in h and "不算主动关心" in h
    assert "毒舌留给熟人" in s and "毒舌留给熟人" in f and "毒舌留给熟人" not in c
    assert "先接住" in c
    assert "“" not in s.split("】", 1)[1] or "例" not in s          # 只写态度，不给现成例句


async def _run_respond(qd, monkeypatch, answers, tier="stranger"):
    sent = []
    async def create(**kw):
        sent.append(kw["messages"]); return _resp(answers[min(len(sent), len(answers)) - 1])
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    monkeypatch.setattr(qd, "familiarity_of", lambda q: tier)
    monkeypatch.setattr(qd, "rate_limited", lambda *a: None)
    monkeypatch.setattr(qd.budget, "track", lambda *a, **k: None)
    out = await qd._respond(100000002, "阿明", "今天是米花掉马桶一周年！", qd.FRIEND_POST_PROMPT.format(
        nick="阿明", post="今天是米花掉马桶一周年！", pic="（配图：一只小猫）", when="刚才") + "\n" + qd.manners_hint(1))
    return out, sent


async def test_respond_unwraps(qd, monkeypatch):
    out, sent = await _run_respond(qd, monkeypatch, ["我回一个。回复：「恭喜米花！」"])
    assert out == "恭喜米花！" and len(sent) == 1
    assert "在别人空间里的分寸" in sent[0][-2]["content"]


async def test_respond_rewrites_then_drops(qd, monkeypatch):
    out, sent = await _run_respond(qd, monkeypatch, ["回复：恭喜", "恭喜米花一周年！"])
    assert len(sent) == 2 and out == "恭喜米花一周年！"
    out, sent = await _run_respond(qd, monkeypatch, ["回复：恭喜", "评论：恭喜"])
    assert out == ""


async def test_comment_friend_post_has_manners(qd, monkeypatch):
    scenes = []
    async def respond(uin, nick, text, scene):
        scenes.append(scene); return "不评"
    monkeypatch.setattr(qd, "_respond", respond)
    monkeypatch.setattr(qd, "write_block", lambda: "")
    monkeypatch.setattr(qd, "familiarity_of", lambda q: "stranger")
    r = await qd.comment_friend_post(1, 100000002, "阿明", {"content": "发烧了 好难受", "tid": "x", "created_time": 0}, "")
    assert r == "declined" and "不是冷着脸" in scenes[0]


async def test_decide_hint_only_for_not_close(qd, monkeypatch):
    prompts = []
    async def create(**kw):
        prompts.append(kw["messages"][1]["content"]); return _resp('{"like": true, "comment": false}')
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    monkeypatch.setattr(qd.budget, "track", lambda *a, **k: None)
    for tier in ("stranger", "close"):
        monkeypatch.setattr(qd, "familiarity_of", lambda q, _t=tier: _t)
        assert await qd.decide_friend_post(1, "阿明", "发烧了", "", "刚才") == {"like": True, "comment": False}
    assert "看到有意思、想接一句的，就评" in prompts[0] and "看到有意思、想接一句的，就评" not in prompts[1]


def test_drop_end_period(qd, monkeypatch):
    """11/21 用户：真人一般不会加句号——空间的评论、说说末尾的句号照聊天的概率去掉"""
    monkeypatch.setattr(qd.cfg, "drop_period_prob", 1.0)
    assert qd.drop_end_period("恭喜米花一周年。") == "恭喜米花一周年"
    assert qd.drop_end_period("好好休息……。") == "好好休息……。"
    assert qd.drop_end_period("真的假的？") == "真的假的？"
    assert qd.drop_end_period("第一句。第二句。") == "第一句。第二句"
    monkeypatch.setattr(qd.cfg, "drop_period_prob", 0.0)
    assert qd.drop_end_period("恭喜。") == "恭喜。"
