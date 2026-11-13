"""空间 agent 11/13：说说写得乱（《交接-说说语言混乱-空间-1113》）——口头习惯、长度、漏出要求、网名、看不懂"""
import json, time, types
import pytest

P1105 = "站在这里忽然觉得，这样的傍晚倒也不坏，前提是身边没有某个老熟人。毕竟那位美丽的魔女究竟是谁？没错，就是我。话虽如此，就算是我，被人拿身材开玩笑也是会记仇的——そう、わたし私です，说的就是你。"
P1106 = "下一站？还没定。……被这么一说，倒好像有点盼着下一回再被他夸了呢。（文字，约一百五十字。）"
P1112 = "在广场正中央停下来比了个手势，难得有闲心——那位模样不错的魔女，嗯，就是我。"
P1113 = "那位站在晚风里、扶了扶帽子还笑得这么从容的魔女究竟是谁？没错，就是我。……\n顺带一提，蛋糕是他请的，这还算有点用处。"


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason="stop")],
                                 usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=0, total_tokens=15))


@pytest.fixture
def qd():
    import plugins.roleplay_chat as p
    return p.qzone_diary


def test_habits_found(qd):
    assert qd.habits_in(P1105) == ["就是我", "话虽如此"]
    assert qd.habits_in(P1112) == ["就是我"]
    assert qd.habits_in(P1113) == ["就是我", "顺带一提"]
    assert qd.habits_in("今天在镇上买了个苹果。") == []


def test_habit_rule(qd):
    r = qd.habit_rule(True, {"话虽如此"})
    assert "一条最多用一样" in r and "就是我" in r                       # 11/14：有见闻也能用“就是我”
    assert "上一条用过“话虽如此”" in r
    assert "上一条" not in qd.habit_rule(False, set())
    assert qd.habits_recent.__defaults__ == (1,)                            # 只看上一条


def test_strip_leak(qd):
    assert qd.strip_leaks(P1106).endswith("下一回再被他夸了呢。")
    assert qd.strip_leaks("今天很冷（约 120 字）") == "今天很冷"
    assert qd.strip_leaks("买了两个（还挺便宜）") == "买了两个（还挺便宜）"


def test_name_parts_and_problems(qd):
    parts = qd._name_parts("そう、わたし私です")
    assert {"そう、わたし私です", "そう", "わたし私です"} <= parts
    bad = qd.diary_problems(P1105, parts, set(), True)
    assert any("そう" in b for b in bad) and "日文" in bad and any("2 样" in b for b in bad)
    assert qd.diary_problems(P1112, set(), set(), True) == []                 # 有见闻也能用“就是我”
    assert qd.diary_problems("今天的风很大，帽子差点飞了。", parts, set(), True) == []
    assert qd.diary_problems("话虽如此，风还是很大。", set(), {"话虽如此"}, False) == ["“话虽如此”上一条刚用过"]


@pytest.mark.parametrize("n_moments,long_allowed", [(0, False), (1, False), (2, True)])
async def test_long_only_with_two_moments(qd, monkeypatch, n_moments, long_allowed):
    moments = [{"qq": 1000 + i, "event": f"事情{i}", "mood": "无语", "tier": "stranger"} for i in range(n_moments)]
    monkeypatch.setattr(qd, "choose_moments", lambda ms: moments)
    monkeypatch.setattr(qd, "today_moments", lambda: moments)
    async def prepare(): return 0
    monkeypatch.setattr(qd.gallery, "prepare", prepare)
    monkeypatch.setattr(qd.gallery, "pick", lambda kw, days: {"file": "x.jpg", "desc": "她在窗边"})
    monkeypatch.setattr(qd, "recent_posts", lambda n=5: [{"text": P1113}])
    monkeypatch.setattr(qd.cfg, "qzone_length_weights", [0, 0, 100])
    monkeypatch.setattr(qd.cfg, "deepseek_api_key", "x")
    prompts = []
    async def create(**kw):
        prompts.append(kw["messages"][1]["content"]); return _resp("今天的风很大。")
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    await qd.make_post()
    assert ("一小段游记" in prompts[0]) == long_allowed, prompts[0][:400]
    assert "别添" in prompts[0] and "看不到对方" in prompts[0]
    assert "上一条用过" in prompts[0]                                             # 上一条用了“就是我”“顺带一提”
    assert "这次想怎么写" in prompts[0]


async def test_rewrite_when_two_habits(qd, monkeypatch):
    moments = [{"qq": 1491656451, "event": "有人掐着点算自己几点睡", "mood": "无语", "tier": "close"}]
    monkeypatch.setattr(qd, "choose_moments", lambda ms: moments)
    monkeypatch.setattr(qd, "today_moments", lambda: moments)
    async def prepare(): return 0
    monkeypatch.setattr(qd.gallery, "prepare", prepare)
    monkeypatch.setattr(qd.gallery, "pick", lambda kw, days: {"file": "x.jpg", "desc": "她在窗边"})
    monkeypatch.setattr(qd, "recent_posts", lambda n=5: [])
    monkeypatch.setattr(qd.cfg, "deepseek_api_key", "x")
    monkeypatch.setattr(qd.ltm, "get_user", lambda q: {"name": "そう、わたし私です"})
    answers = [P1105, "某个老熟人今晚又在跟我解释他没熬夜。（约八十字）"]
    sent = []
    async def create(**kw):
        sent.append(kw["messages"]); return _resp(answers[len(sent) - 1])
    monkeypatch.setattr(qd.client.chat.completions, "create", create)
    d = await qd.make_post()
    assert len(sent) == 2 and "日文" in sent[1][-1]["content"]
    assert d["text"] == "某个老熟人今晚又在跟我解释他没熬夜。"
    assert "QQ 网名" in sent[0][1]["content"] and "そう" not in sent[0][1]["content"]


def test_no_confused_hint(qd):
    assert not hasattr(qd, "CONFUSED_RE")               # 11/14 用户：别为“看不懂”专门加提示，撤掉


def test_pick_way(qd):
    import random
    random.seed(1)
    with_m = {qd.pick_way(True) for _ in range(300)}
    without = {qd.pick_way(False) for _ in range(300)}
    assert "记今天聊天里的一件事" in with_m and len(with_m) >= 6
    assert not any("今天聊" in w for w in without) and len(without) >= 5


def test_post_time_window(qd, monkeypatch):
    import random
    random.seed(2)
    monkeypatch.setattr(qd.cfg, "qzone_post_window", "09:30-23:30")
    ts = [qd._hm(qd._new_day_state("2026-11-14")["post_at"]) for _ in range(400)]
    assert min(ts) >= 570 and max(ts) < 1410
    assert sum(t < 12 * 60 for t in ts) > 20 and sum(t >= 19 * 60 for t in ts) > 160      # 白天也有，晚上多一点


async def test_tick_waits_when_asleep_or_too_soon(qd, monkeypatch, tmp_path):
    from datetime import datetime
    monkeypatch.setattr(qd, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(qd, "SELF_LIKE_FILE", tmp_path / "sl.json")
    monkeypatch.setattr(qd, "read_block", lambda: None)
    monkeypatch.setattr(qd, "write_block", lambda m="POST": None)
    monkeypatch.setattr(qd, "in_peak", lambda: False)
    now = datetime(2026, 11, 14, 10, 30, tzinfo=qd.BJ)
    monkeypatch.setattr(qd, "_now", lambda: now)
    st = qd.state(); st.update(post_at="10:00", posted=False, summary_done=False); qd.save_state(st)
    made = []
    async def make_post(): made.append(1); return {"text": "x"}
    async def publish(d): return "T"
    async def poll_all(): return ""
    monkeypatch.setattr(qd, "make_post", make_post)
    monkeypatch.setattr(qd, "publish", publish)
    monkeypatch.setattr(qd, "poll_all", poll_all)
    monkeypatch.setattr(qd.cfg, "qzone_comment_reply", False)
    monkeypatch.setattr(qd, "recent_posts", lambda n=5: [{"ts": now.timestamp() - 3600, "text": "昨晚"}])
    await qd.tick()
    assert not made and not qd.state()["posted"]          # 离上一条不到 12 小时：等着，不跳过
    monkeypatch.setattr(qd, "recent_posts", lambda n=5: [{"ts": now.timestamp() - 13 * 3600, "text": "昨晚"}])
    monkeypatch.setattr(qd.sleep_mod, "asleep", lambda now=None: True)
    await qd.tick()
    assert not made                                       # 睡着了不发
    monkeypatch.setattr(qd.sleep_mod, "asleep", lambda now=None: False)
    await qd.tick()
    assert made and qd.state()["posted"]
    assert not qd.state()["summary_done"]                 # 白天发的不提前日结


async def test_note_likers(qd, monkeypatch, tmp_path):
    monkeypatch.setattr(qd, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(qd, "POSTS_FILE", tmp_path / "posts.json")
    monkeypatch.setattr(qd.asyncio, "sleep", _nosleep)
    qd._write(qd.POSTS_FILE, [{"tid": "P1", "text": "今天在集市买了个苹果"}])
    async def likers(tid, owner=None):
        return [(71001, "心夏"), (71002, "路人")] if tid == "P1" else []
    monkeypatch.setattr(qd.qz, "likers", likers)
    monkeypatch.setattr(qd.ltm, "get_user", lambda q: {"talks": 5} if q == 71001 else {})
    pend = []
    monkeypatch.setattr(qd.ltm, "add_pending", lambda key, items: pend.append(key))
    for q in (71001, 71002):
        qd.get_history(f"qzone_{q}").clear()
    posts = [{"tid": "P1", "created_time": int(time.time()) - 3600}, {"tid": "OLD", "created_time": int(time.time()) - 5 * 86400}]
    assert await qd._note_likers(1, posts) == 2
    assert "给你的日记点了个赞" in qd.get_history("qzone_71001")[-1]["content"]
    assert pend == ["qzone_71001"]                        # 没聊过天的点赞人不交给长期记忆
    assert "点了个赞" in qd.space_memo(71001)
    assert await qd._note_likers(1, posts) == 0           # 记过的不重复记


async def _nosleep(*a, **k):
    return None
