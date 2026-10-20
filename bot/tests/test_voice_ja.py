"""语音念日语、附中文（10/21，VOICE_LANG=ja）：她要说的中文先译成日语，用日语念，语音后面再发中文文字"""
import asyncio
import time

import pytest
from nonebug import App

from test_chat import fake_create, mkbot, pev
from test_voice import WAV


@pytest.fixture
def ja_on(monkeypatch):
    import plugins.roleplay_chat as p
    spoken, asked = [], []

    async def speak(text, timeout=None, lang="zh"):
        spoken.append((text, lang))
        return (WAV if speak.ok else None), ("合成 1.0 秒" if speak.ok else "ConnectError")
    speak.ok = True

    async def translate(text, user_id=None, group_id=None):
        asked.append(text)
        return translate.ja
    translate.ja = "わかりました、また今度にしましょう。"
    monkeypatch.setattr(p.tts, "token", "t")
    monkeypatch.setattr(p.tts, "_ok", True)
    monkeypatch.setattr(p.tts, "_checked", time.monotonic() + 1e6)
    monkeypatch.setattr(p.tts, "speak", speak)
    monkeypatch.setattr(p, "translate_ja", translate)
    monkeypatch.setattr(p.cfg, "voice_enabled", True)
    monkeypatch.setattr(p.cfg, "voice_lang", "ja")
    monkeypatch.setattr(p.cfg, "voice_request_by_model", False)
    monkeypatch.setattr(p.cfg, "voice_prob_private", 1.0)
    monkeypatch.setattr(p.cfg, "voice_interjection_prob", 0.0)
    monkeypatch.setattr(p.cfg, "voice_tier_multiplier", {"disliked": 0.0, "stranger": 1.0, "friend": 1.0, "acquaintance": 1.0, "close": 1.0})
    p._voice_count.clear()
    return p, speak, spoken, translate, asked


def test_default_is_ja():
    import plugins.roleplay_chat as p
    assert p.cfg.__class__().voice_lang == "ja"


@pytest.mark.asyncio
async def test_ja_voice_then_chinese_text(app: App, ja_on):
    p, speak, spoken, translate, asked = ja_on
    reply = "知道啦知道啦，下次再说吧。"
    p.client.chat.completions.create = fake_create(reply)
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("下次再聊")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, bot=bot)       # 先发日语语音
        for b in p.split_bubbles(reply):                                         # 再发中文文字
            ctx.should_call_send(ev, b, result=None, bot=bot)
    assert asked == [reply]
    assert spoken == [("わかりました、また今度にしましょう。", "ja")]
    assert p.get_history("private_222")[-1]["content"] == "[发了语音] " + reply
    assert p._voice_count[p.voice_day()] == 1


@pytest.mark.asyncio
async def test_ja_translate_fails_text_only(app: App, ja_on):
    p, speak, spoken, translate, asked = ja_on
    translate.ja = None
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert spoken == []
    assert p.get_history("private_222")[-1]["content"] == "知道啦。"
    assert p._voice_count[p.voice_day()] == 0


@pytest.mark.asyncio
async def test_ja_tts_fails_text_only(app: App, ja_on):
    p, speak, spoken, translate, asked = ja_on
    speak.ok = False
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert p.get_history("private_222")[-1]["content"] == "知道啦。"


@pytest.mark.asyncio
async def test_ja_voice_send_fails_text_still_sent(app: App, ja_on):
    p, speak, spoken, translate, asked = ja_on
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, exception=RuntimeError("风控"), bot=bot)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert p.get_history("private_222")[-1]["content"] == "知道啦。"
    assert p._voice_count[p.voice_day()] == 0


class _R:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
        self.usage = None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,expect", [
    ("「わかりました。」", "わかりました。"),
    ("わかりました。\nまた今度。", "わかりました。また今度。"),
    ("知道了。", None),                      # 没译成日语
    ("OKです。", None),                      # 有英文字母，念不了
])
async def test_translate_ja_checks(monkeypatch, answer, expect):
    import plugins.roleplay_chat as p
    seen = {}

    async def create(**kw):
        seen.update(kw)
        return _R(answer)
    monkeypatch.setattr(p.client.chat.completions, "create", create)
    assert await p.translate_ja("知道了。") == expect
    assert "伊蕾娜" in seen["messages"][0]["content"] and "知道了。" in seen["messages"][0]["content"]


@pytest.mark.asyncio
async def test_translate_ja_timeout(monkeypatch):
    import plugins.roleplay_chat as p

    async def create(**kw):
        await asyncio.sleep(5)
    monkeypatch.setattr(p.client.chat.completions, "create", create)
    monkeypatch.setattr(p.cfg, "voice_translate_timeout", 0.1)
    assert await p.translate_ja("知道了。") is None


def test_has_kana():
    import plugins.roleplay_chat as p
    assert p.voice.has_kana("そうですね") and p.voice.has_kana("イレイナ") and not p.voice.has_kana("知道了")


@pytest.mark.asyncio
async def test_real_client_sends_lang(monkeypatch):
    import plugins.roleplay_chat as p
    sent = {}

    class _Resp:
        status_code = 200
        content = WAV
        headers = {"X-Cost": "1.0"}

    class _C:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json)
            return _Resp()
    monkeypatch.setattr(p.voice._httpx, "AsyncClient", _C)
    c = p.voice.TTSClient("http://x", "t")
    data, _ = await c.speak("こんにちは", lang="ja")
    assert data == WAV and sent == {"text": "こんにちは", "lang": "ja"}
