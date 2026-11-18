"""空间 agent 11/18：说说把“谁舍不得打飞猫”弄反了——见闻带上当时的几句、别把话安到对方头上、不许照抄原话"""
import json, types
import pytest

Q = 10001
ENTRIES = [
    {"role": "user", "content": "【鲨鱼】找你收过路费呢", "uid": Q, "name": "鲨鱼"},
    {"role": "assistant", "content": "一只猫收什么过路费"},
    {"role": "user", "content": "【鲨鱼】怎么不用魔法打飞它", "uid": Q, "name": "鲨鱼"},
    {"role": "assistant", "content": "打飞一只猫？"},
    {"role": "user", "content": "【路人】今天吃啥", "uid": 222, "name": "路人"},
    {"role": "assistant", "content": "面包"},
    {"role": "user", "content": "【鲨鱼 → 你】没事，忽略那句话吧\n（回复你说的“打飞一只猫？”）怎么了，不行吗，难道舍不得？", "uid": Q, "name": "鲨鱼"},
    {"role": "assistant", "content": "只是没必要为了一只猫动魔法\n它蹲那儿那么舒服，我把它打飞做什么"},
    {"role": "user", "content": "【鲨鱼 → 你】我还以为你被挡路了会生气", "uid": Q, "name": "鲨鱼"},
    {"role": "assistant", "content": "真要生气，也是冲着故意挡路的人\n它又不是"},
]
EVENT = "聊到路上被猫挡道，要不要用魔法打飞"


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason="stop")],
                                 usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=0, total_tokens=15))


@pytest.fixture
def qd():
    import plugins.roleplay_chat as p
    return p.qzone_diary


def test_moment_ctx_picks_the_exchange(qd):
    c = qd.moment_ctx(ENTRIES, Q, EVENT, ["猫", "魔法"])
    assert 2 <= len(c) <= 6
    assert all(x.startswith(("对方：", "你：")) for x in c)
    assert any("怎么不用魔法打飞它" in x for x in c) and any(x.startswith("你：") and "打飞" in x for x in c)
    assert not any("吃啥" in x or "面包" in x for x in c)            # 别人的话、她回别人的话不要
    assert not any("【" in x for x in c)                                # 群名片头去掉
    assert all(len(x) <= 3 + qd.CTX_LINE_CHARS + 1 for x in c)


def test_moment_ctx_no_match(qd):
    assert qd.moment_ctx(ENTRIES, Q, "被夸字写得好看", ["书法"]) == []
    assert qd.moment_ctx([], Q, EVENT, []) == []
    assert qd.moment_ctx(ENTRIES, 999, EVENT, ["猫"]) == []


def test_ctx_copied(qd):
    c = qd.moment_ctx(ENTRIES, Q, EVENT, ["猫"])
    assert qd.ctx_copied("那家伙问我怎么不用魔法打飞它，真是的", c)
    assert not qd.ctx_copied("有人劝我把挡路的猫打飞，我才不干", c)
    assert not qd.ctx_copied("只是没必要为了一只猫动魔法", c)           # 她自己的话不算照抄对方
    bad = qd.diary_problems("那家伙问我怎么不用魔法打飞它", set(), set(), True, c)
    assert "照抄了对方的原话" in bad


def test_save_moments_keeps_ctx(qd, monkeypatch, tmp_path):
    monkeypatch.setattr(qd, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(qd.ltm, "get_user", lambda q: {"facts": [1]})
    qd.save_moments("group_100000001", [{"qq": Q, "event": EVENT, "mood": "好笑", "keywords": ["猫", "魔法"]}],
                    {Q: "鲨鱼"}, ENTRIES)
    items = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert items[0]["event"] == EVENT and any("打飞" in x for x in items[0]["ctx"])


def test_save_moments_without_entries_reads_pending(qd, monkeypatch, tmp_path):
    monkeypatch.setattr(qd, "DAILY_DIR", tmp_path)
    pend = tmp_path / "pend.json"
    pend.write_text(json.dumps(ENTRIES, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(qd.ltm, "_pending_path", lambda key: pend, raising=False)
    qd.save_moments("group_100000001", [{"qq": Q, "event": EVENT, "mood": "好笑", "keywords": ["猫"]}], {Q: "鲨鱼"})
    items = json.loads(sorted(tmp_path.glob("2*.json"))[0].read_text(encoding="utf-8"))
    assert items[0].get("ctx")


async def test_prompt_has_ctx_and_rule(qd, monkeypatch):
    ctx = qd.moment_ctx(ENTRIES, Q, EVENT, ["猫"])
    moments = [{"qq": Q, "event": EVENT, "mood": "好笑", "tier": "close", "ctx": ctx}]
    monkeypatch.setattr(qd, "choose_moments", lambda ms: moments)
    monkeypatch.setattr(qd, "today_moments", lambda: moments)
    async def prepare(): return 0
    monkeypatch.setattr(qd.gallery, "prepare", prepare)
    monkeypatch.setattr(qd.gallery, "pick", lambda kw, days: {"file": "x.jpg", "desc": "她在窗边"})
    monkeypatch.setattr(qd, "recent_posts", lambda n=5: [])
    monkeypatch.setattr(qd.cfg, "deepseek_api_key", "x")
    monkeypatch.setattr(qd.ltm, "get_user", lambda q: {"name": "鲨鱼"})
    answers = ["那家伙问我怎么不用魔法打飞它。", "有只猫霸着路，我才懒得打飞它。"]
    sent = []
    async def create(**kw):
        sent.append(kw["messages"]); return _resp(answers[len(sent) - 1])
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    d = await qd.make_post()
    p = sent[0][1]["content"]
    assert "当时的几句" in p and "对方：怎么不用魔法打飞它" in p
    assert "别把你自己的话和想法安到对方头上" in p
    assert "一看就知道是在说他" not in p                                 # 熟人档不再暗示把对方写成主角
    assert len(sent) == 2 and "照抄" in sent[1][-1]["content"]
    assert d["text"] == "有只猫霸着路，我才懒得打飞它。"


def test_diary_context_other_person(qd, monkeypatch):
    """11/18 记忆 agent 提醒：聊天时问“今天的日记呢”，见闻里的“对方”会被当成眼前这个人"""
    ms = [{"qq": Q, "event": "对方怂恿我用魔法打飞挡路的猫，我没舍得"},
          {"qq": 20002, "event": "被对方夸善解人意，又被说太敏感"}]
    monkeypatch.setattr(qd, "today_moments", lambda: ms)
    monkeypatch.setattr(qd, "recent_posts", lambda n=1: [])
    monkeypatch.setattr(qd, "state", lambda: {"posted": False, "post_at": "21:00"})
    t = qd.diary_context("今天的日记呢")
    assert "某人怂恿我" in t and "被某人夸" in t and "对方怂恿" not in t
    t = qd.diary_context("今天的日记呢", uin=Q)
    assert "对方怂恿我" in t and "被某人夸" in t                         # 和眼前这个人的事保留“对方”
