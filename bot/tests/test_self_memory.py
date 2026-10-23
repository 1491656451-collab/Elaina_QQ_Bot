"""她自己的近况（10/23，self_memory.py）：存、取、主动说的话整理、写信记进“她说过”、遗忘和限量、/近况"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


def _resp(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100, completion_tokens=20),
                           model="deepseek-flash")


def _client(reply_fn, calls=None):
    async def create(**kw):
        if calls is not None:
            calls.append(kw)
        r = reply_fn(kw)
        if isinstance(r, Exception):
            raise r
        return _resp(json.dumps(r, ensure_ascii=False))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _sm(tmp_path, reply_fn=lambda kw: {}, calls=None, **kw):
    from plugins.roleplay_chat.self_memory import SelfMemory
    return SelfMemory(Path(tmp_path) / "self.json", _client(reply_fn, calls), "m", **kw)


def _put(sm, text, at, mood="", source="冒泡", told_in=None):
    """测试用：直接记一条她说过的经历（正常是 add_said 整理出来的）"""
    it = sm._add(text, source=source, at=at, mood=mood, told_in=told_in)
    sm._save()
    return it


def _at(hour: int, days_ago: int = 0) -> float:
    d = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return d.timestamp()


@pytest.mark.asyncio
async def test_place_from_her_words(tmp_path):
    """现在在哪从她的话里来：第一次说了就记；说“到了……”换地方，旧的进 trail；没说换、又对不上的不改"""
    replies = iter([
        {"place": {"name": "某小镇", "kind": "城镇", "moved": False}},
        {"place": {"name": "某小镇", "kind": "城镇", "moved": False}, "new": [{"text": "在镇上的面包店门口排了半天队", "fits": True}]},
        {"place": {"name": "某片森林", "kind": "野外", "moved": False}, "new": [{"text": "在林子里捡到一只受伤的鸟", "fits": False}]},
        {"place": {"name": "某片森林", "kind": "野外", "moved": True}, "new": [{"text": "在林子里迷路绕了半天", "fits": True}]},
    ])
    sm = _sm(tmp_path, lambda kw: next(replies))
    assert sm.get_place() == {}, "一开始没有，等她自己说"
    assert sm.life_for_chat("group_1", "你好")["place"] == {"status": "还没说过"}
    assert sm.life_for_proactive("group_1")["place"] == {"status": "还没说过"}
    for text in ("我在一个小镇歇脚", "镇上的面包店门口排了好久", "刚在林子里捡到一只鸟", "今天出了镇子，进了一片林子"):
        sm._load()["said"].append({"text": text, "source": "冒泡", "key": "group_1", "at": time.time()})
    await sm.digest_said()
    p = sm.get_place()
    assert p["name"] == "某片森林" and p["kind"] == "野外"
    assert [t["name"] for t in sm._load()["trail"]] == ["某小镇"]
    texts = [i["text"] for i in sm._items()]
    assert "在林子里捡到一只受伤的鸟" not in texts, "没说换地方、又和在镇上对不上：不收"
    assert "在镇上的面包店门口排了半天队" in texts and "在林子里迷路绕了半天" in texts
    assert next(i for i in sm._items() if "迷路" in i["text"])["where"] == "某片森林", "换了地方：新的事记在新地方"
    out = _sm(tmp_path).life_for_chat("group_1", "你好")
    assert out["place"] == {"name": "某片森林", "kind": "野外", "days_here": 1, "said": "今天", "status": "现在"}


def test_event_not_before_its_time(tmp_path):
    """还没到时间的不返回（以后整理聊天时补记的事可能带时间）；今天的按时间排"""
    sm = _sm(tmp_path)
    sm._move_to("某小镇", "城镇")
    now = _at(12)
    a = _put(sm, "在集市上被一只会学人说话的猫跟了半条街", _at(9), mood="好笑")
    b = _put(sm, "下雨被困在旅馆一下午", _at(15), mood="无聊")
    assert a["id"] and a["where"] == "某小镇" and a["time"] == "上午"
    out = sm.life_for_chat("group_1", "今天干嘛了", now=now)
    assert [x["text"] for x in out["today"]] == [a["text"]]
    assert out["today"][0]["when"] == "今天上午" and out["today"][0]["mood"] == "好笑"
    later = sm.life_for_chat("group_1", "今天干嘛了", now=_at(16))
    assert [x["text"] for x in later["today"]] == [a["text"], b["text"]]


def test_dedupe_and_topic_cap(tmp_path):
    sm = _sm(tmp_path)
    assert _put(sm, "在集市上被一只猫跟了半条街", _at(9))
    assert _put(sm, "在集市上被一只猫跟了半条街", _at(10)) is None, "一样的不记两遍"
    for i, t in enumerate(["买到刚出炉的可颂", "面包店老板多送了一个面包", "吃到奶油很足的甜点"]):
        assert _put(sm, t, _at(9, i + 1))
    assert _put(sm, "又去了一家面包店", _at(10)) is None, "面包这类两周内最多 3 条"


def test_related_and_her_topics(tmp_path):
    """按这句话挑相关的；面包这类对方没提就合起来最多 1 条；不和今天的重复"""
    sm = _sm(tmp_path)
    _put(sm, "在集市上被一只会学人说话的猫跟了半条街", _at(9, 3))
    _put(sm, "面包店老板多送了一个面包", _at(9, 2))
    _put(sm, "买到刚出炉的可颂", _at(8))
    _put(sm, "在河边帮人找回了丢的渔网", _at(9))
    out = sm.life_for_chat("private_1", "你上次说的猫呢", now=_at(12))
    assert any("猫" in x["text"] for x in out["related"])
    texts = [x["text"] for x in out["today"] + out["related"]]
    assert sum(("面包" in t or "可颂" in t) for t in texts) == 0, "近况测试 J08：对方没提，一条都不带"
    pro = [x["text"] for x in sm.life_for_proactive("group_9", n=5, now=_at(12))["fresh"]]
    assert sum(("面包" in t or "可颂" in t) for t in pro) == 1, "主动说话：最多 1 条（留着）"
    out2 = sm.life_for_chat("private_1", "你今天吃面包了吗", now=_at(12))
    texts2 = [x["text"] for x in out2["today"] + out2["related"]]
    assert sum(("面包" in t or "可颂" in t) for t in texts2) == 2, "对方提了面包：可以带 2 条"


def test_chat_length_cap(tmp_path):
    sm = _sm(tmp_path)
    for i in range(6):
        _put(sm, f"第{i}件事：在镇子里帮人做了很长很长很长很长很长的一件委托{i}", _at(8 + i))
    out = sm.life_for_chat("group_1", "委托", now=_at(20))
    assert sum(len(x["text"]) for x in out["today"] + out["related"]) <= 140
    assert out["today"], "至少留一条今天的"


@pytest.mark.asyncio
async def test_said_same_and_new(tmp_path):
    """主动说的话：讲的是已有的那条只记“这里听过了”；新讲的经历记一条；和现在在哪对不上的不收"""
    calls = []

    def reply(kw):
        msg = kw["messages"][1]["content"]
        assert "她之前说自己在：某片森林（野外，今天说的）" in msg and "#1" in msg and "她刚冒泡说的" in msg
        return {"same": [1], "new": [{"text": "在溪边钓到一条小鱼", "mood": "得意", "fits": True},
                                     {"text": "刚在镇上的店里买了帽子", "mood": "", "fits": False}]}
    sm = _sm(tmp_path, reply, calls)
    sm._move_to("某片森林", "野外")
    a = _put(sm, "在林子里迷路绕了半天", time.time() - 60)
    sm._load()["said"].append({"text": "今天在林子里绕了半天，还钓到一条小鱼", "source": "冒泡", "key": "group_5", "at": time.time()})
    out = sm.life_for_proactive("group_5", now=time.time())
    assert out["told_here"][-1]["when"] == "刚才", "整理完之前：原话先算这里刚说过"
    n = await sm.digest_said()
    assert n == 1 and len(calls) == 1
    items = sm._items()
    assert next(i for i in items if i["id"] == a["id"])["told_in"] == ["group_5"]
    fish = [i for i in items if "小鱼" in i["text"]]
    assert fish and fish[0]["source"] == "冒泡" and fish[0]["told_in"] == ["group_5"] and fish[0]["mood"] == "得意"
    assert not any("帽子" in i["text"] for i in items)
    assert sm._load()["said"] == []
    pro = sm.life_for_proactive("group_5", now=time.time())
    assert pro["fresh"] == [] and len(pro["told_here"]) == 2, "这个群都听过了"
    other = sm.life_for_proactive("group_6", now=time.time())
    assert len(other["fresh"]) == 2 and other["told_here"] == []


@pytest.mark.asyncio
async def test_said_failure_kept_and_retried(tmp_path):
    state = {"fail": True}

    def reply(kw):
        return RuntimeError("超时") if state["fail"] else {"new": [{"text": "接了个送信的委托", "fits": True}]}
    sm = _sm(tmp_path, reply)
    sm._load()["said"].append({"text": "今天接了个送信的委托", "source": "搭话", "key": "private_7", "at": time.time()})
    assert await sm.digest_said() == 0
    assert len(sm._load()["said"]) == 1 and sm._retry_at > time.time(), "出错：留着，隔一阵再试"
    state["fail"] = False
    sm._retry_at = 0
    assert await sm.digest_said() == 1
    assert any("送信" in i["text"] for i in sm._items())


@pytest.mark.asyncio
async def test_letter_goes_into_told(tmp_path):
    """写信：近况记一条，收信人的“她说过”里也记一条"""
    from plugins.roleplay_chat.memory import LongTermMemory
    ltm = LongTermMemory(Path(tmp_path) / "mem", _client(lambda kw: {}), "m")
    sm = _sm(tmp_path, lambda kw: {"new": [{"text": "在山脚的小屋避了一夜雨", "mood": "", "fits": True}]})
    sm.ltm = ltm
    sm.add_said("最近在山脚的小屋避了一夜雨……你那边还好吗", source="写信", key="private_42", qq=42)
    await sm._task
    told = [t["text"] for t in ltm.get_user(42).get("told") or []]
    assert told == ["写信跟他讲过：在山脚的小屋避了一夜雨"]
    sm2 = _sm(tmp_path / "b", lambda kw: {})
    sm2.ltm = ltm
    sm2.add_said("好久不见，最近怎么样", source="写信", key="private_43", qq=43)
    await sm2._task
    assert [t["text"] for t in ltm.get_user(43).get("told")] == ["给他写过一封信"], "信里没讲经历：也记写过信"


def test_forget_old_and_cap(tmp_path):
    sm = _sm(tmp_path, max_items=5, keep_days=14)
    _put(sm, "半个多月前的事", time.time() - 20 * 86400)
    things = ["帮人找回丢的渔网", "在山路上遇到一队商人", "旅馆的床塌了一角", "看了一场街头木偶戏", "被一阵怪风吹跑了帽子", "替老奶奶送了一封信"]
    for i, t in enumerate(things):
        _put(sm, t, time.time() - (i + 1) * 86400)
    future = _put(sm, "还没到时间的下午那件事", time.time() + 3600)
    texts = [i["text"] for i in sm._items()]
    assert "半个多月前的事" not in texts, "超过两周的忘掉"
    assert len(texts) == 5 and future["text"] in texts, "超了先忘旧的；还没发生的不动"
    assert "替老奶奶送了一封信" not in texts and "被一阵怪风吹跑了帽子" not in texts


def test_admin_describe_delete_edit(tmp_path):
    sm = _sm(tmp_path)
    sm._move_to("某小镇", "城镇")
    _put(sm, "在集市上被一只猫跟了半条街", _at(9, 1), mood="好笑")
    _put(sm, "帮旅馆老板修好了漏雨的屋顶", _at(9))
    d = sm.describe()
    assert "她说现在在：某小镇（城镇，第 1 天，今天说的）" in d
    assert "1. [冒泡｜" in d and "猫" in d and "2. [冒泡｜" in d
    assert sm.edit(2, "帮旅馆老板补好了屋顶") == ("帮旅馆老板修好了漏雨的屋顶", "帮旅馆老板补好了屋顶")
    assert sm.delete(1) == "在集市上被一只猫跟了半条街"
    assert sm.delete(5) is None and sm.edit(9, "x") is None
    assert [i["text"] for i in _sm(tmp_path)._items()] == ["帮旅馆老板补好了屋顶"]
    assert sm.set_place_by_admin("某片森林")["name"] == "某片森林"
    assert _sm(tmp_path).get_place()["name"] == "某片森林"


def test_core_has_self_mem():
    from plugins.roleplay_chat import core
    assert core.self_mem.ltm is core.ltm
    assert core.self_mem.path.name == "self.json"


def test_place_gets_stale(tmp_path):
    """说在某处以后超过 5 天没再提：标成“前几天说的”；又提到了就从那天重新算"""
    sm = _sm(tmp_path)
    sm._move_to("某小镇", "城镇")
    now = time.time()
    assert sm.life_for_chat("group_1", "你好", now=now + 5 * 86400)["place"]["status"] == "现在"
    stale = sm.life_for_chat("group_1", "你好", now=now + 6 * 86400)["place"]
    assert stale["status"] == "前几天说的" and stale["said"] == "6 天前"
    assert "不一定还在" not in sm.describe()
    sm._load()["place"]["since"] = (datetime.now() - timedelta(days=8)).date().isoformat()
    assert "给她看的是“不一定还在”" in sm.describe()
    sm._apply_place({"name": "某小镇", "kind": "城镇", "moved": False}, "这镇子还是老样子")
    assert sm.life_for_chat("group_1", "你好")["place"]["status"] == "现在", "又提到了：重新算"
    assert sm.get_place()["since"] != sm.get_place()["seen"], "从哪天开始待的不变"


def test_place_guards(tmp_path):
    """10/23 回放：一家店不算在哪；原话里没提到的地方（照抄给它看的“之前在哪”、从“找棵树歇会儿”推的）不算"""
    sm = _sm(tmp_path)
    sm._apply_place({"name": "面包店", "kind": "城镇", "moved": False}, "说起来，这时间面包店该出炉了…")
    assert sm.get_place() == {}
    sm._apply_place({"name": "某片树林", "kind": "野外", "moved": True}, "中午的太阳晒得扫帚都发烫了，找棵树歇会儿。")
    assert sm.get_place() == {}
    sm._apply_place({"name": "山谷里的小镇", "kind": "城镇", "moved": True}, "这两天在一个山谷里的小镇落脚。")
    assert sm.get_place()["name"] == "山谷里的小镇"
    sm._load()["place"]["seen"] = "2026-01-01"
    sm._apply_place({"name": "山谷里的小镇", "kind": "城镇", "moved": False}, "这么晚了还没睡？")
    assert sm.get_place()["seen"] == "2026-01-01", "原话没提：不算又提到了"


def test_same_place_needs_mention(tmp_path):
    """第二次回放：只说“明天要赶路”，模型照抄了“之前在某国”——不算又提到了；说了“这国家”才算"""
    sm = _sm(tmp_path)
    sm._apply_place({"name": "某国", "kind": "城镇", "moved": False}, "大半夜的，才知道这国家宵禁时街上会有野猫")
    sm._load()["place"]["seen"] = "2026-01-01"
    sm._apply_place({"name": "某国", "kind": "城镇", "moved": False}, "这个点了还没睡，明天怕是要顶着黑眼圈赶路。")
    assert sm.get_place()["seen"] == "2026-01-01"
    sm._apply_place({"name": "某国", "kind": "城镇", "moved": False}, "这国家的面包真硬")
    assert sm.get_place()["seen"] != "2026-01-01"


def test_beijing_time(tmp_path, monkeypatch):
    """“今天”“上午”按北京时间算，不看服务器的时区"""
    from plugins.roleplay_chat import self_memory as S
    ts = datetime(2026, 10, 22, 16, 30, tzinfo=S.BEIJING).timestamp()     # 北京时间 10/22 16:30 = UTC 08:30
    assert S._day_of(ts) == "2026-10-22" and S._slot(ts) == "下午"
    late = datetime(2026, 10, 23, 0, 30, tzinfo=S.BEIJING).timestamp()     # 北京 10/23 0:30 = UTC 10/22 16:30
    assert S._day_of(late) == "2026-10-23"
    assert S._when_of({"at": ts}, late) == "昨天下午"


# ---------------------------------------------------------------- C：整理聊天时收她讲的经历、说自己在哪（10/23）
def _ltm_with_self(tmp_path, reply_fn, calls=None):
    from plugins.roleplay_chat.memory import LongTermMemory
    ltm = LongTermMemory(Path(tmp_path) / "mem", _client(reply_fn, calls), "m", batch=2, max_events=12)
    sm = _sm(tmp_path)
    sm.ltm, ltm.self_mem = ltm, sm
    return ltm, sm


@pytest.mark.asyncio
async def test_chat_summary_collects_self(tmp_path):
    calls = []

    def reply(kw):
        sys_msg, user_msg = kw["messages"][0]["content"], kw["messages"][1]["content"]
        if "记忆核对员" in sys_msg:
            return {"check": [{"n": 1, "who": "伊蕾娜"}, {"n": 2, "who": "别人"}]}
        assert "七、她自己最近的经历 self" in sys_msg
        assert "【她自己（伊蕾娜）之前说过的】" in user_msg and "#1" in user_msg and "某小镇" in user_msg
        return {"people": [{"qq": 111, "affection": 0}],
                "self": {"same": [1], "new": [{"text": "在镇口被一只大鹅追了半条街", "mood": "狼狈", "fits": True},
                                              {"text": "朋友小林失恋了", "fits": True}],
                         "place": {"name": "某小镇", "kind": "城镇", "moved": False}}}
    ltm, sm = _ltm_with_self(tmp_path, reply, calls)
    ltm.verify_enabled = True
    sm._move_to("某小镇", "城镇")
    old = _put(sm, "在集市上被一只会学人说话的猫跟了半条街", time.time() - 86400)
    sm._load()["place"]["seen"] = "2026-01-01"
    ltm.add_pending("private_111", [{"role": "user", "content": "今天干嘛了", "uid": 111, "name": "阿明"},
                                    {"role": "assistant", "content": "这镇子的鹅太凶了，追了我半条街。对了，那只学舌的猫又来了"}])
    await ltm.summarize("private_111")
    items = {i["text"]: i for i in sm._items()}
    assert items[old["text"]]["told_in"] == ["private_111"], "同一件事：只记这里讲过"
    goose = items.get("在镇口被一只大鹅追了半条街")
    assert goose and goose["source"] == "聊天" and goose["told_in"] == ["private_111"] and goose["where"] == "某小镇"
    assert "朋友小林失恋了" not in items, "核对答“别人”：不收"
    assert sm.get_place()["seen"] != "2026-01-01", "她说了“这镇子”：算又提到了"


@pytest.mark.asyncio
async def test_chat_summary_without_self_is_fine(tmp_path):
    """模型没给 self、或者近况出错：档案照常存"""
    ltm, sm = _ltm_with_self(tmp_path, lambda kw: {"people": [{"qq": 111, "add": [{"text": "是学生", "kind": "身份"}], "affection": 0}],
                                                    "self": "坏的"})
    ltm.add_pending("private_111", [{"role": "user", "content": "我是学生", "uid": 111, "name": "阿明"},
                                    {"role": "assistant", "content": "是喔"}])
    await ltm.summarize("private_111")
    assert [f["text"] for f in ltm.get_user(111)["facts"]] == ["是学生"]
    assert sm._items() == []


def test_same_needs_words_in_said(tmp_path):
    """第三次回放：模型把不相干的会话也写成“讲的就是 #1”——原话里得真有这件事的字才算讲过"""
    sm = _sm(tmp_path)
    it = _put(sm, "在山谷里的小镇夜里去看了会发光的湖", time.time() - 3600)
    nums = {1: sm._items()[0]}
    sm._apply_said({"text": "你好。有事吗？不过才刚认识吧，你倒是很直接。", "source": "聊天", "key": "private_1", "at": time.time()},
                   {"same": [1], "_nums": nums})
    assert sm._items()[0]["told_in"] == [], "不相干：不算讲过"
    sm._apply_said({"text": "前两天在山谷里的小镇，夜里那个湖真的会发光", "source": "聊天", "key": "private_2", "at": time.time()},
                   {"same": [1], "_nums": nums})
    assert sm._items()[0]["told_in"] == ["private_2"]
    assert it


def test_place_word_without_move_logged(tmp_path, caplog):
    """《近况上线前检查》3.2：原话里有地点词、这次却没记下新地方的，日志记一行（以后数漏了多少）"""
    from loguru import logger
    lines = []
    hid = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        sm = _sm(tmp_path)
        sm._apply_said({"text": "这镇上的集市今天特别热闹", "source": "冒泡", "key": "group_1", "at": time.time()}, {"_nums": {}})
        sm._apply_said({"text": "你好呀", "source": "冒泡", "key": "group_1", "at": time.time()}, {"_nums": {}})
        sm._apply_said({"text": "今天到了一个海边的小镇", "source": "冒泡", "key": "group_1", "at": time.time()},
                       {"_nums": {}, "place": {"name": "海边的小镇", "kind": "城镇", "moved": True}})
    finally:
        logger.remove(hid)
    hits = [x for x in lines if "提到地点词但没换地方" in x]
    assert len(hits) == 1 and "集市" in hits[0]


def test_summary_her_claims_and_stories_1023():
    """10/23：她对他的说法（“你之前说自己是男生”）、她讲的小说人物的事，都不算他的；核对也这么判"""
    from plugins.roleplay_chat.memory import SUMMARIZE_PROMPT, VERIFY_PROMPT
    assert "伊蕾娜对他的说法" in SUMMARIZE_PROMPT and "她自己答应过、讲过的照常写进 told" in SUMMARIZE_PROMPT and "小说里的人（美奈、沙耶、芙兰……）的事" in SUMMARIZE_PROMPT
    assert "伊蕾娜讲的故事、小说里的人（美奈、沙耶……）的事" in VERIFY_PROMPT
    assert "只是伊蕾娜这么说、对方自己没说过的" in VERIFY_PROMPT
