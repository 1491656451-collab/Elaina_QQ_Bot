"""表情：挑图、全局不重复、重打标签、不按关系限制情绪（10/22 表情 agent 加，依据《表情抽查-1022.md》）"""
import random
from types import SimpleNamespace

import pytest


def _store(tmp_path, items):
    from plugins.roleplay_chat.stickers import StickerStore
    s = StickerStore(tmp_path / "stk", None, "m")
    (tmp_path / "stk").mkdir(parents=True, exist_ok=True)
    for i, tags in enumerate(items, 1):
        f = f"{i:02d}.png"
        (tmp_path / "stk" / f).write_bytes(b"x")
        s.items[f"h{i}"] = {"no": i, "file": f, "tags": list(tags), "labeled": True, "label_v": 2, "desc": f"图{i}"}
    return s


def test_pick_prefers_first_tag(tmp_path):
    # 想表达“敷衍”：第一个标签是敷衍的那张优先，不挑“得意、敷衍”（主要是得意脸）
    s = _store(tmp_path, [("得意", "敷衍"), ("敷衍", "无语"), ("得意", "敷衍")])
    for _ in range(20):
        assert s.pick("敷衍", record=False)["no"] == 2
    # 第一个标签都对不上时，才用第二个标签对得上的
    s2 = _store(tmp_path / "b", [("得意", "敷衍"), ("开心", "敷衍")])
    assert s2.pick("敷衍", record=False)["no"] in (1, 2)


def test_pick_excludes_whole_sticker_with_disallowed_tag(tmp_path):
    # 陌生人不给委屈：“委屈、无语”那张整张不给，即使她想表达的是“无语”
    s = _store(tmp_path, [("委屈", "无语"), ("无语", "嫌弃")])
    stranger = {"得意", "嫌弃", "无语", "生气", "开心", "疑惑", "震惊", "敷衍"}
    for _ in range(20):
        assert s.pick("无语", allowed=stranger, record=False)["no"] == 2
    assert "委屈" not in s.emotions(stranger)
    s_only = _store(tmp_path / "b", [("委屈", "无语")])
    assert s_only.pick("无语", allowed=stranger, record=False) is None
    assert s_only.emotions(stranger) == []


def test_pick_global_recent_and_usage_persist(tmp_path):
    s = _store(tmp_path, [("得意",), ("得意",), ("得意",)])
    s.recent_max = 2
    seen = [s.pick("得意")["no"] for _ in range(3)]
    assert len(set(seen)) == 3                       # 最近发过的不再挑，三次挑了三张不同的
    assert all(it["used"] == 1 for it in s.items.values())
    from plugins.roleplay_chat.stickers import StickerStore
    s2 = StickerStore(tmp_path / "stk", None, "m")   # 重启：最近发过的、发过几次都还在
    assert len(s2.recent) == 2 and all(it.get("used") == 1 for it in s2.items.values())


def test_pick_record_false_changes_nothing(tmp_path):
    s = _store(tmp_path, [("得意",)])
    s.pick("得意", record=False)
    assert s.recent == [] and "used" not in s.items["h1"]


def test_pick_prefers_less_used(tmp_path):
    s = _store(tmp_path, [("得意",), ("得意",)])
    s.recent_max = 0
    s.items["h1"]["used"] = 50
    random.seed(1)
    picks = [s.pick("得意", record=False)["no"] for _ in range(200)]
    assert picks.count(2) > 150                     # 发得多的那张很少再被挑中


async def test_relabel_old_version(tmp_path):
    s = _store(tmp_path, [("无语", "敷衍"), ("嫌弃",), ("开心",)])
    del s.items["h1"]["label_v"]                     # 第 1 版提示打的旧标签
    del s.items["h2"]["label_v"]
    s.items["h2"]["manual"] = True                   # 手动改过的不重打
    assert [it["no"] for it in s.unlabeled()] == [1]
    assert s.usable()                                # 等重打期间旧标签照常能用
    assert "旧标签待重打 1 张" in s.summary()

    async def ask(data, mime):
        return "情绪：嫌弃\n描述：斜眼看人"
    s._ask = ask
    assert await s.label_one(s.items["h1"])
    assert s.items["h1"]["tags"] == ["嫌弃"] and s.items["h1"]["label_v"] == 2
    assert s.unlabeled() == []

    # 重打时没认出情绪：留着旧标签
    s.items["h3"].pop("label_v")

    async def ask2(data, mime):
        return "看不出来"
    s._ask = ask2
    assert await s.label_one(s.items["h3"])
    assert s.items["h3"]["tags"] == ["开心"] and s.items["h3"]["label_v"] == 2


def test_label_prompt_asks_main_emotion_first():
    from plugins.roleplay_chat.stickers import LABEL_PROMPT
    assert "第一个写这张图最主要" in LABEL_PROMPT


def test_roll_no_tier_limit(tmp_path, monkeypatch):
    """10/22 用户定的：不按关系限制情绪，陌生人、讨厌的人也能用全部情绪；同一个群里给不同的人连发也可以"""
    from plugins import roleplay_chat as p
    sc = p.stickers_chat
    s = _store(tmp_path, [("委屈", "无语"), ("害羞",), ("得意",)])
    monkeypatch.setattr(sc.stickers, "items", s.items)
    monkeypatch.setattr(sc.stickers, "root", s.root)
    monkeypatch.setattr(sc.cfg, "sticker_prob", 1.0)
    monkeypatch.setattr(sc, "quota_left", lambda *a, **k: 99)
    monkeypatch.setattr(sc.random, "random", lambda: 0.0)     # 讨厌的人倍数 0.5，抽签固定抽中
    sc._last_sticker.clear()
    for fam in ("disliked", "stranger", "friend", "acquaintance", "close"):
        hit, allowed, emo = sc.sticker_roll("group_55", fam, True, 55, False)
        assert hit and allowed is None and set(emo) == {"委屈", "无语", "害羞", "得意"}
    assert sc.stickers.pick("无语", allowed=None, record=False)["no"] == 1


def test_sticker_only_prob_default():
    from plugins.roleplay_chat.config import Config
    assert Config().sticker_only_prob == 1.0
