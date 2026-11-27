"""空间 agent 11/27：说说少写别人（别像特地写给谁看）、空间往来偶尔才写、一半说说不配图"""
import collections, json, random, types
import pytest


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason="stop")],
                                 usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=0, total_tokens=15))


@pytest.fixture
def qd():
    import plugins.roleplay_chat as p
    return p.qzone_diary


def test_choose_moments_qzone_rare(qd, monkeypatch):
    monkeypatch.setattr(qd, "familiarity_of", lambda q: "close")
    only_qzone = [{"qq": 10 + i, "event": f"空间往来{i}", "src": "qzone"} for i in range(4)]
    mixed = [{"qq": 1, "event": "群里聊猫", "src": "group"}] + only_qzone
    random.seed(7)
    counts, hit = collections.Counter(), 0
    for _ in range(2000):
        got = qd.choose_moments([dict(m) for m in only_qzone])
        hit += bool(got)
        assert len(got) <= 1                                         # 空间往来一条最多一件
    # 一天全是空间往来时：30% 进候选 × 60% 要写见闻 ≈ 18%
    assert 0.12 < hit / 2000 < 0.25
    for _ in range(2000):
        got = qd.choose_moments([dict(m) for m in mixed])
        counts[len(got)] += 1
        assert sum(m["src"] == "qzone" for m in got) <= 1 and len(got) <= 2
    assert 0.33 < counts[0] / 2000 < 0.47                            # 不写见闻约 40%


def test_ways(qd):
    ws = {w for w, *_ in qd.WRITE_WAYS}
    assert not any("隔空" in w for w in ws)
    no_pic = {qd.pick_way(True, False) for _ in range(300)} | {qd.pick_way(False, False) for _ in range(300)}
    assert qd.PIC_WAY not in no_pic
    assert qd.PIC_WAY in {qd.pick_way(False, True) for _ in range(300)}


def test_close_hint_not_for_him(qd):
    h = qd._tier_hint({"tier": "close"})
    assert "一看就知道" not in h and "不是写给他看的" in h
    assert "不是写给某一个人的" in qd.DIARY_PROMPT


def _setup(qd, monkeypatch, moments, prob):
    monkeypatch.setattr(qd, "choose_moments", lambda ms: moments)
    monkeypatch.setattr(qd, "today_moments", lambda: moments)
    async def prepare(): return 0
    monkeypatch.setattr(qd.gallery, "prepare", prepare)
    picked = []
    def pick(kw, days):
        picked.append(1); return {"file": "x.jpg", "desc": "她在窗边"}
    monkeypatch.setattr(qd.gallery, "pick", pick)
    monkeypatch.setattr(qd, "recent_posts", lambda n=5: [])
    monkeypatch.setattr(qd.cfg, "deepseek_api_key", "x")
    monkeypatch.setattr(qd.cfg, "qzone_text_only_prob", prob)
    monkeypatch.setattr(qd.cfg, "drop_period_prob", 0.0)
    monkeypatch.setattr(qd.budget, "can_background", lambda: True)
    monkeypatch.setattr(qd.budget, "track", lambda *a, **k: None)
    prompts = []
    async def create(**kw):
        prompts.append(kw["messages"][1]["content"]); return _resp("今天的风很大。")
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    return picked, prompts


async def test_make_post_text_only(qd, monkeypatch):
    picked, prompts = _setup(qd, monkeypatch, [], 1.0)
    d = await qd.make_post()
    assert picked == [] and d["image"] == "" and d["text_only"]
    p = prompts[0]
    assert "这条只写字，不配图。" in p and "配图是今天的你" not in p and qd.PIC_WAY not in p
    assert "图里的场景" not in p


async def test_make_post_with_pic(qd, monkeypatch):
    picked, prompts = _setup(qd, monkeypatch, [], 0.0)
    d = await qd.make_post()
    assert picked == [1] and d["image"] == "x.jpg" and not d["text_only"]
    assert "配图是今天的你：她在窗边" in prompts[0]


async def test_make_post_no_gallery_falls_back(qd, monkeypatch):
    picked, prompts = _setup(qd, monkeypatch, [], 0.0)
    monkeypatch.setattr(qd.gallery, "pick", lambda kw, days: None)
    d = await qd.make_post()                                         # 原来图库空了会报错，现在只写字
    assert d["image"] == "" and "这条只写字" in prompts[0]


async def test_publish_text_only(qd, monkeypatch, tmp_path):
    monkeypatch.setattr(qd, "POSTS_FILE", tmp_path / "posts.json")
    monkeypatch.setattr(qd, "SELF_LIKE_FILE", tmp_path / "self_like.json")
    sent = []
    async def list_posts(n): return []
    async def publish(text, images=None, right=None):
        sent.append(images); return "tid1"
    monkeypatch.setattr(qd.qz, "list_posts", list_posts)
    monkeypatch.setattr(qd.qz, "publish", publish)
    used = []
    monkeypatch.setattr(qd.gallery, "mark_used", lambda f: used.append(f))
    tid = await qd.publish({"text": "只写字", "image": "", "desc": "", "moments": [], "text_only": True})
    assert tid == "tid1" and sent == [None] and used == []
    posts = json.loads((tmp_path / "posts.json").read_text(encoding="utf-8"))
    assert posts[-1]["text_only"] is True


def test_post_pic_note(qd):
    assert qd._post_pic_note({"desc": "她在窗边"}) == "（配图是你自己：她在窗边）"
    assert qd._post_pic_note({"desc": "", "text_only": True}) == "（没配图）"
    assert qd._post_pic_note({"desc": ""}) == "（配图是你自己：记不清了）"      # 手动补记的，不知道有没有图
    assert qd._post_pic_note(None) == "（配图是你自己：记不清了）"
