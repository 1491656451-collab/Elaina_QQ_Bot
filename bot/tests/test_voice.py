"""语音（10/20）：聊天时自己发语音、对方要求时她自己决定、失败改发文字、每天上限、原声语气词"""
import time
from pathlib import Path

import pytest
from nonebug import App

from test_chat import CALLS, fake_create, mkbot, pev, gev

WAV = b"RIFF" + b"\0" * 40 + b"\1\2" * 100


@pytest.fixture
def voice_on(monkeypatch):
    import plugins.roleplay_chat as p
    spoken = []

    async def speak(text, timeout=None):
        spoken.append(text)
        return (WAV if speak.ok else None), ("合成 1.0 秒" if speak.ok else "ConnectError")
    speak.ok = True
    monkeypatch.setattr(p.tts, "token", "t")
    monkeypatch.setattr(p.tts, "_ok", True)
    monkeypatch.setattr(p.tts, "_checked", time.monotonic() + 1e6)
    monkeypatch.setattr(p.tts, "speak", speak)
    monkeypatch.setattr(p.cfg, "voice_enabled", True)
    monkeypatch.setattr(p.cfg, "voice_lang", "zh")      # 这两个文件测的是直接念中文；念日语附中文的见 test_voice_ja.py
    monkeypatch.setattr(p.cfg, "voice_request_by_model", False)   # 这个文件测的是认关键词的老办法；她自己判断的见 test_voice_marks.py
    monkeypatch.setattr(p.cfg, "voice_prob_private", 0.0)
    monkeypatch.setattr(p.cfg, "voice_prob_group", 0.0)
    monkeypatch.setattr(p.cfg, "voice_interjection_prob", 0.0)
    monkeypatch.setattr(p.cfg, "voice_tier_multiplier", {"disliked": 0.0, "stranger": 1.0, "friend": 1.0, "acquaintance": 1.0, "close": 1.0})
    p._voice_count.clear()
    return p, speak, spoken


def _sys_text(kw):
    """本轮提示（不含第一条：人设 + 聊天规则。人设里本来就写着“【传声】”，10/20 部署时这里误报过）"""
    return "\n".join(str(m["content"]) for m in kw["messages"][1:] if m["role"] == "system")


@pytest.mark.asyncio
async def test_voice_random_private(app: App, voice_on, monkeypatch):
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.cfg, "voice_prob_private", 1.0)
    p.client.chat.completions.create = fake_create("知道啦知道啦，下次再说吧。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("下次再聊")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, bot=bot)
    assert spoken == ["知道啦知道啦，下次再说吧。"]
    assert p.get_history("private_222")[-1]["content"] == "[发了语音] 知道啦知道啦，下次再说吧。"
    assert p._voice_count[p.voice_day()] == 1
    assert "【传声】" not in _sys_text(CALLS[-1])


@pytest.mark.asyncio
async def test_voice_random_not_drawn_sends_text(app: App, voice_on):
    p, speak, spoken = voice_on
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert spoken == []


@pytest.mark.asyncio
async def test_voice_request_she_agrees(app: App, voice_on):
    p, speak, spoken = voice_on
    p.client.chat.completions.create = fake_create("[传声]哼，才不要呢。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("发个语音听听嘛")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, bot=bot)
    assert "【传声】对方想听你的声音" in _sys_text(CALLS[-1])
    assert spoken == ["哼，才不要呢。"]
    assert p.get_history("private_222")[-1]["content"] == "[发了语音] 哼，才不要呢。"


@pytest.mark.asyncio
async def test_voice_request_she_declines(app: App, voice_on):
    p, speak, spoken = voice_on
    p.client.chat.completions.create = fake_create("才不给你听。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("用语音说一句")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "才不给你听。", result=None, bot=bot)
    assert spoken == []


@pytest.mark.asyncio
async def test_voice_mark_without_request_ignored(app: App, voice_on):
    """没人要求、也没抽中，她自己写了「[传声]」：标记去掉，照常发文字"""
    p, speak, spoken = voice_on
    p.client.chat.completions.create = fake_create("[传声]你好呀。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("你好")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "你好呀。", result=None, bot=bot)
    assert spoken == []


@pytest.mark.asyncio
async def test_voice_tts_fails_falls_back(app: App, voice_on, monkeypatch):
    p, speak, spoken = voice_on
    speak.ok = False
    monkeypatch.setattr(p.cfg, "voice_prob_private", 1.0)
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert spoken == ["知道啦。"]
    assert p.get_history("private_222")[-1]["content"] == "知道啦。"
    assert p._voice_count[p.voice_day()] == 0


@pytest.mark.asyncio
async def test_voice_daily_cap(app: App, voice_on, monkeypatch):
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.cfg, "voice_prob_private", 1.0)
    p._voice_count[p.voice_day()] = p.cfg.voice_daily_max
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("发个语音")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert spoken == [] and "【传声】" not in _sys_text(CALLS[-1])


@pytest.mark.asyncio
async def test_voice_offline_no_hint(app: App, voice_on, monkeypatch):
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.tts, "_ok", False)
    p.client.chat.completions.create = fake_create("不要。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("发个语音")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "不要。", result=None, bot=bot)
    assert "【传声】" not in _sys_text(CALLS[-1]) and spoken == []


@pytest.mark.asyncio
async def test_voice_interjection_clip(app: App, voice_on, monkeypatch, tmp_path):
    p, speak, spoken = voice_on
    (tmp_path / "Y02_は.wav").write_bytes(b"CLIP")
    (tmp_path / "语气词表.json").write_text('{"表":[{"写法":["哈？"],"文件":["Y02_は.wav"]}]}', encoding="utf-8")
    monkeypatch.setattr(p.voice_chat, "interjections", p.voice.Interjections(tmp_path))
    monkeypatch.setattr(p.cfg, "voice_interjection_prob", 1.0)
    p.client.chat.completions.create = fake_create("哈？")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("我是你男朋友")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(b"CLIP"), result=None, bot=bot)
    assert spoken == []
    assert p.get_history("private_222")[-1]["content"] == "[发了语音] 哈？"


@pytest.mark.asyncio
async def test_voice_group(app: App, voice_on, monkeypatch):
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.cfg, "voice_prob_group", 1.0)
    p.client.chat.completions.create = fake_create("早上好。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = gev("早", to_me=True)
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, bot=bot)
    assert spoken == ["早上好。"]


@pytest.mark.asyncio
async def test_voice_disliked_no_hint(app: App, voice_on, monkeypatch):
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.core, "familiarity_of", lambda qq: "disliked")
    monkeypatch.setattr(p.cfg, "dislike_ignore_prob", 0.0)
    monkeypatch.setattr(p.cfg, "voice_prob_private", 1.0)
    p.client.chat.completions.create = fake_create("哦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("发个语音")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "哦。", result=None, bot=bot)
    assert "【传声】" not in _sys_text(CALLS[-1]) and spoken == []


def test_voice_text_and_mark():
    import plugins.roleplay_chat as p
    assert p.split_voice_mark("[传声]哼。") == ("哼。", True)
    assert p.split_voice_mark("【传声】 好吧") == ("好吧", True)
    assert p.split_voice_mark("好吧") == ("好吧", False)
    assert p.voice_text("知道啦~", 120) == "知道啦"
    assert p.voice_text("好吧（叹气）", 120) == "好吧"
    assert p.voice_text("OK啦", 120) is None
    assert p.voice_text("嗯……这家店的面包，\n确实还不错", 120) == "嗯……这家店的面包，确实还不错"
    assert p.voice_text("你好。\n再见", 120) == "你好。再见"
    assert p.voice_text("😊", 120) is None
    assert p.voice_text("一" * 121, 120) is None
    for t in ["发个语音听听", "你能用语音说吗", "想听听你的声音", "说句话给我听", "来段语音"]:
        assert p.VOICE_REQUEST_RE.search(t), t
    assert not p.VOICE_REQUEST_RE.search("今天去哪了")
