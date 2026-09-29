import pytest, nonebot
from nonebug import NONEBOT_INIT_KWARGS
from nonebot.adapters.onebot.v11 import Adapter

def pytest_configure(config):
    config.stash[NONEBOT_INIT_KWARGS] = dict(
        deepseek_api_key="sk-test", reply_delay_min=0, reply_delay_max=0, reply_delay_per_char=0,
        user_cooldown=0, merge_wait=0, smart_min_interval=0, peak_enabled=False, multi_message=False, peak_extra_delay_min=0, peak_extra_delay_max=0, switch_gap_min=0, switch_gap_max=0, bubble_gap_min=0, bubble_gap_max=0, catchup_gap_min=0, catchup_gap_max=0, catchup_enabled=False, sleep_enabled=False, merge_wait_complete=0, merge_wait_incomplete=0, skip_filler=False, log_file="", interject_enabled=False, bubble_enabled=False, nudge_enabled=False, persona_file="personas/elaina.md", superusers={"999"}, command_start={"/"},
    )

@pytest.fixture(scope="session", autouse=True)
def load_bot(nonebug_init):
    nonebot.get_driver().register_adapter(Adapter)
    nonebot.load_plugins("plugins")


@pytest.fixture(autouse=True)
def _summary_uses_patched_client(load_bot):
    # 测试里改的是聊天客户端的 create；整理也走它（真实运行时整理用单独的客户端）
    from plugins import roleplay_chat as p
    p.ltm.summary_client = None
    p._quiet_until = 0.0
    p._private_hour.clear(); p._away.clear(); p._wrapup.clear(); p._chat_seq.clear(); p._arrival_seq.clear()
    p._same_names = {}
    p._SAME_NAMES_FILE.unlink(missing_ok=True)
    p.ltm._fails.clear(); p.ltm._retry_at.clear()
    import shutil
    shutil.rmtree(p.spend.root, ignore_errors=True)
    p.spend.reset(); p._tired_sent.clear(); p._goodnight.clear(); p._active_chats.clear(); p._farewell_at.clear()
    yield
