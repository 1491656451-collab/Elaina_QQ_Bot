"""语音标记清理（10/20，聊天 agent，接《人设交接说明.md》第 25 节）：
她照着提示写「【传声】」、照着聊天记录写「[发了语音] ……」、写「（发了一段语音）」这类旁白，都不能当文字发出去。"""
import pytest
from nonebug import App

from test_chat import fake_create, mkbot, pev
from test_voice import WAV, voice_on  # noqa: F401


@pytest.mark.parametrize("raw,text,wish", [
    ("[传声]好吧", "好吧", True),
    ("【传声】好吧", "好吧", True),                       # 提示的标题就是【传声】，她容易照着写
    ("［传声］ 好吧", "好吧", True),
    ("[语音] 好吧", "好吧", True),
    ("[发了语音] 哼，只此一次", "哼，只此一次", True),     # 照着聊天记录里自己的话写
    ("（发了一段语音）听好了", "听好了", True),
    ("（用传声魔法）听好了", "听好了", True),
    ("好吧\n[传声]就一句", "好吧\n就一句", True),
    ("传声魔法可是很费魔力的", "传声魔法可是很费魔力的", False),   # 正常说到传声魔法：不动
    ("（传声魔法很费魔力的，别老要）", "（传声魔法很费魔力的，别老要）", False),
    ("你那条语音我听了", "你那条语音我听了", False),
])
def test_voice_marks_cleaned(raw, text, wish):
    import plugins.roleplay_chat as p
    r, _ = p.split_sticker(p.clean_reply(raw, keep_sticker=True))
    assert p.split_voice_mark(r) == (text, wish)


def test_voice_marks_removed_outside_chat():
    """写信、插话这些地方发不了语音：标记去掉，不留「[传声]」"""
    import plugins.roleplay_chat as p
    assert p.clean_reply("【传声】好久不见") == "好久不见"
    assert p.clean_reply("[发了语音] 好久不见") == "好久不见"
    assert p.clean_reply("（发了一段语音）好久不见") == "好久不见"


@pytest.mark.asyncio
async def test_voice_request_bracket_title(app: App, voice_on):  # noqa: F811
    """对方要语音，她照着提示标题写了「【传声】」：照样念"""
    p, speak, spoken = voice_on
    p.client.chat.completions.create = fake_create("【传声】哼，就这一次。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("发个语音听听嘛")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, bot=bot)
    assert spoken == ["哼，就这一次。"]


@pytest.mark.asyncio
async def test_copied_history_mark_not_sent_as_text(app: App, voice_on):  # noqa: F811
    """没人要语音，她照着聊天记录写了「[发了语音] ……」：标记去掉，发文字"""
    p, speak, spoken = voice_on
    p.client.chat.completions.create = fake_create("[发了语音] 知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert spoken == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fam,extra", [("stranger", "多半会客气地婉拒"), ("friend", "看你心情"), ("acquaintance", None), ("close", None)])
async def test_voice_request_tier_hint(app: App, voice_on, monkeypatch, fam, extra):  # noqa: F811
    """对方要语音：陌生人、普通朋友在【传声】提示后面补一句倾向，熟人、很熟不补（10/20；补过“偶尔不给”，没用，撤了）"""
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.core, "familiarity_of", lambda qq: fam)
    hint = p.voice_roll(fam, False, "发个语音听听嘛")[1]
    assert hint.startswith("【传声】对方想听你的声音")
    if extra:
        assert extra in hint
    else:
        assert hint == p.VOICE_REQUEST_HINT
    assert p.voice_roll("disliked", False, "发个语音听听嘛")[1] == ""


# ---------------------------------------------------------------- 10/20：对方是不是在要语音，由她自己判断
@pytest.fixture
def by_model(voice_on, monkeypatch):  # noqa: F811
    p, speak, spoken = voice_on
    monkeypatch.setattr(p.cfg, "voice_request_by_model", True)
    return p, speak, spoken


def _sys(kw):
    return "\n".join(str(m["content"]) for m in kw["messages"] if m["role"] == "system")


def test_by_model_default_on():
    import plugins.roleplay_chat as p
    assert p.cfg.voice_request_by_model is True


@pytest.mark.asyncio
async def test_by_model_hint_every_turn_and_she_decides(app: App, by_model):
    """没有关键词（“再来一条”）也给提示；她写了「[传声]」就念"""
    from test_chat import CALLS
    p, speak, spoken = by_model
    assert not p.VOICE_REQUEST_RE.search("刚才那条没听够，再来一条")
    p.client.chat.completions.create = fake_create("[传声]贪心。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("刚才那条没听够，再来一条")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, p.voice.segment(WAV), result=None, bot=bot)
    assert p.VOICE_OFFER_HINT in _sys(CALLS[-1])
    assert spoken == ["贪心。"]


@pytest.mark.asyncio
async def test_by_model_plain_reply_is_text(app: App, by_model):
    from test_chat import CALLS
    p, speak, spoken = by_model
    p.client.chat.completions.create = fake_create("今天去了趟集市。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("今天去哪了")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "今天去了趟集市。", result=None, bot=bot)
    assert p.VOICE_OFFER_HINT in _sys(CALLS[-1]) and spoken == []


@pytest.mark.parametrize("fam,tier", [("stranger", "对方要的话：你们还不熟"), ("friend", "对方要的话：你们只是普通朋友"),
                                      ("acquaintance", None), ("close", None)])
def test_by_model_tier_and_disliked(by_model, fam, tier):
    p, speak, spoken = by_model
    hint = p.voice_roll(fam, False, "今天去哪了")[1]
    assert hint.startswith(p.VOICE_OFFER_HINT)
    assert (tier in hint) if tier else hint == p.VOICE_OFFER_HINT
    assert p.voice_roll("disliked", False, "发个语音")[1] == ""


def test_by_model_offline_or_capped_no_hint(by_model, monkeypatch):
    p, speak, spoken = by_model
    p._voice_count[p.voice_day()] = p.cfg.voice_daily_max
    assert p.voice_roll("close", False, "发个语音") == (False, "")
    p._voice_count.clear()
    monkeypatch.setattr(p.tts, "_ok", False)
    assert p.voice_roll("close", False, "发个语音") == (False, "")


# ---------------------------------------------------------------- 10/20 13:04 线上：speak() 不认 timeout，整条回复丢了
@pytest.mark.asyncio
async def test_real_tts_client_accepts_timeout():
    """用真的 TTSClient（不是测试里的假 speak）：带 timeout 调用不报错，连不上就返回 (None, 原因)"""
    import plugins.roleplay_chat as p
    c = p.voice.TTSClient("http://127.0.0.1:9", "t", timeout=20.0)
    data, note = await c.speak("你好", timeout=0.5)
    assert data is None and note


@pytest.mark.asyncio
async def test_speak_error_falls_back_to_text(app: App, voice_on, monkeypatch):  # noqa: F811
    """念的时候出了任何错：这条改发文字，不能整条丢掉"""
    p, speak, spoken = voice_on

    async def boom(text, timeout=None):
        raise TypeError("boom")
    monkeypatch.setattr(p.tts, "speak", boom)
    monkeypatch.setattr(p.cfg, "voice_prob_private", 1.0)
    p.client.chat.completions.create = fake_create("知道啦。")
    async with app.test_matcher() as ctx:
        bot = mkbot(ctx)
        ev = pev("好的")
        ctx.receive_event(bot, ev)
        ctx.should_call_send(ev, "知道啦。", result=None, bot=bot)
    assert p.get_history("private_222")[-1]["content"] == "知道啦。"
