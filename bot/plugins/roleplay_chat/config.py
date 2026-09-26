from pydantic import BaseModel


class Config(BaseModel):
    # ---- DeepSeek / OpenAI 兼容接口 ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"     # 可选 deepseek-flash / deepseek-v4-pro
    llm_temperature: float = 1.1          # 角色扮演建议 1.0~1.3，更有个性
    long_reply_max_tokens: int = 200      # “长回复”时的上限（约 150 字）：讲故事、详细解释、倾诉等
    # 注：旧配置项 LLM_MAX_TOKENS 已不再使用（留在 .env 里也没关系）
    short_reply_max_tokens: int = 80      # 日常闲聊时的上限；提示词要求约 10～30 字，这里留一点余量
    max_input_chars: int = 300            # 对方一条消息最多取前多少字发给模型，防止长文刷费用
    llm_timeout: float = 60.0
    llm_thinking: bool = False            # 思考模式：开启后回复更慢、更贵，且 temperature 失效；聊天建议关闭

    # ---- 人设 ----
    persona_file: str = "personas/default.md"

    # ---- 识图 ----
    vision_enabled: bool = True           # 看懂对方发的图片 / 表情包（deepseek-flash 支持）
    vision_model: str = "deepseek-flash"  # 看图用的模型（deepseek-v4-pro 不支持图片，所以单独设置）
    vision_max_images: int = 2            # 一条消息最多看几张图
    vision_in_peak: bool = False          # 高峰时段也看图吗（默认不看，省钱）
    vision_cache: str = "data/vision_cache.json"   # 同一张图只看一次
    # 本机角色识别（WD14 Tagger）：认出画里是不是伊蕾娜。第一次启动自动下载模型（约 470 MB）
    vision_tagger: bool = True
    vision_tagger_repo: str = "SmilingWolf/wd-swinv2-tagger-v3"   # 想更准可换 SmilingWolf/wd-eva02-large-tagger-v3（约 1.2 GB，更慢）
    vision_tagger_mirrors: list[str] = ["https://hf-mirror.com", "https://huggingface.co"]   # 按顺序尝试下载
    vision_tagger_dir: str = "data/models"
    vision_tagger_threshold: float = 0.75  # 角色置信度门槛：误认成伊蕾娜就调高，认不出就调低
    vision_tagger_maybe: float = 0.35     # 伊蕾娜置信度在这个值～门槛之间：让看图模型再确认一次（设成 1 就关掉）
    vision_tagger_threads: int = 2        # 识别时最多占几个 CPU 核
    # 群里有人发伊蕾娜的图（没 @ 她）时，她也可能看一眼、说一句
    vision_self_react: bool = True
    vision_self_react_prob: float = 0.6   # 每次遇到时搭话的概率
    vision_self_react_interval: float = 600.0   # 同一个群至少隔多少秒才再搭话一次（群友常拿她的表情包斗图）

    # ---- 发表情包（用小号“收藏表情”里的伊蕾娜表情表达情绪）----
    sticker_enabled: bool = True
    sticker_prob: float = 0.15            # 一轮回复可以带表情的基础概率（再乘下面按关系的倍数）
    sticker_min_interval: float = 180.0   # 同一个群（私聊按人）两次表情之间最少隔几秒
    sticker_only_prob: float = 0.5        # 抽中表情、且这轮是“只回几个字”时，提示她可以只甩一个表情的概率
    sticker_self_react_prob: float = 0.5  # 有人发了她的画像（斗图）时，这一轮可以带表情的概率
    sticker_recent_avoid: int = 5         # 同一个会话最近发过的几张不重复
    sticker_refresh_hours: float = 6.0    # 多久重新拉一次收藏表情
    sticker_fetch_count: int = 200        # 一次最多拉多少张收藏
    sticker_private: bool = True          # 私聊也发
    sticker_sub_type: int = 1             # 1 = 显示成表情样式（小图）；发出来不对就改成 -1，按普通图片发
    sticker_dir: str = "data/stickers"
    sticker_tier_multiplier: dict[str, float] = {"disliked": 0.5, "stranger": 0.7, "acquaintance": 1.0, "close": 1.3}

    # ---- 小说知识库（摘要 + 原文检索）----
    knowledge_enabled: bool = True
    knowledge_summary_dir: str = "knowledge/summaries"   # 章节摘要（相对 bot 目录）
    knowledge_novel_dir: str = "../novel"                # 小说 txt 所在文件夹
    knowledge_characters_file: str = "knowledge/characters.md"   # 角色档案（经原文核实）
    knowledge_cache: str = "data/novel_index.pkl"
    knowledge_top_characters: int = 2                    # 话里点名的角色，最多带几人的档案
    knowledge_top_summaries: int = 2                     # 每次最多带几条摘要
    knowledge_top_chunks: int = 1                        # 每次最多带几段原文
    knowledge_character_chars: int = 800                 # 每份角色档案最多带多少字（只取外貌/说话/喜好/误解等关键部分）
    knowledge_summary_chars: int = 500                   # 每条摘要最多带多少字
    knowledge_chunk_chars: int = 600                     # 每段原文最多带多少字
    knowledge_min_summary_score: float = 14.0            # 相关度门槛：低于它就不带（闲聊时不打扰）
    knowledge_min_chunk_score: float = 20.0

    # ---- 记忆 ----
    history_max_turns: int = 20           # 私聊保留的最近消息条数（一问一答算 2 条）
    history_max_turns_group: int = 10     # 群聊保留的最近消息条数（群里人多，少记一点更省 token）
    history_dir: str = "data/history"     # 记忆落盘目录，重启不丢
    group_shared_memory: bool = True      # True: 同一个群共用一份记忆（角色记得群里每个人）
    passive_buffer: int = 5               # 没 @ 机器人的群消息暂存最近 N 条，作为“旁听”背景；0 关闭
    # 注：旧配置项 GROUP_PASSIVE_BUFFER 已不再使用（留在 .env 里也没关系）

    # ---- 长期记忆 ----
    memory_enabled: bool = True
    memory_dir: str = "data/memory"
    memory_batch: int = 8                 # 被挤出短期记忆的消息攒够几条就整理一次
    memory_max_facts: int = 12            # 每人档案最多几条
    memory_max_events: int = 8            # 每个群往事最多几条

    # ---- 好感度（决定她对人冷淡还是随意）----
    # 分数 -100～100：长期记忆整理时按对话内容 +5～-15；踩雷立刻扣分；很久不聊慢慢回落到 0
    affection_chat_gain: int = 0          # 每条正常聊天加几分；0 = 光聊天不加好感，只看聊了什么
    affection_dislike: int = -20          # 低于这个分：讨厌（爱答不理）
    affection_acquaintance: int = 30      # 达到这个分：熟人
    affection_close: int = 70             # 达到这个分：很熟
    affection_daily_cap: int = 5          # 靠“正常聊天”每天最多涨几分（AFFECTION_CHAT_GAIN 为 0 时没用）
    affection_decay_after_days: int = 7   # 多少天没聊开始回落
    affection_decay_per_day: float = 2.0  # 之后每天回落几分
    dislike_ignore_prob: float = 0.4      # 被讨厌的人叫她时，有多大概率直接不理（不调用模型）
    close_friends: list[int] = []         # 直接当作“很熟”的 QQ 号（管理员指定，不受性别限制）
    # 性别：只有确认是女生才能到“很熟”；男生和没确认的人好感最高到熟人（封顶在 AFFECTION_CLOSE - 1）
    # 她不会主动问性别；对方自己说了，她会先质疑一句，对方再确认才算数
    gender_cap: bool = True
    gender_confirm_window: int = 1800     # 她质疑之后，多少秒内对方再确认才算数
    # 说到她的雷点时立刻扣分（不等整理、不调用模型）
    taboo_words: list[str] = ["平胸", "飞机场", "贫乳", "洗衣板", "搓衣板", "太平公主", "没胸", "胸小", "胸平"]
    taboo_penalty: float = 4.0            # 每次扣几分
    taboo_daily_max: float = 16.0         # 每人每天因此最多扣几分（陌生人的标准）
    # 按关系远近打折：越熟越当成打闹，扣得越少（每次扣分和每日上限都乘这个倍数）
    taboo_tier_multiplier: dict[str, float] = {"disliked": 1.5, "stranger": 1.0, "acquaintance": 0.5, "close": 0.25}

    # ---- 时间感：知道隔了多久没聊 ----
    gap_notice_hours: float = 6.0         # 对方隔了这么久才来找她，就告诉她隔了多久（她会按关系远近决定提不提）
    context_stale_hours: float = 3.0      # 上一段聊天过去这么久了，就提醒她这是新的一段对话，别硬接旧话题

    # ---- 主动写信（只写给“很熟”的 QQ 好友，私聊发送）----
    letter_enabled: bool = True
    letter_daily_prob: float = 0.25       # 每个符合条件的人，每天收到信的概率
    letter_min_days: float = 3.0          # 同一个人两封信至少隔几天
    letter_min_silence_hours: float = 24.0   # 对方至少这么久没来找她，她才会写
    letter_hours: str = "10:00-22:00"     # 只在这段时间寄信（北京时间）；高峰时段也不寄
    letter_max_per_day: int = 3           # 所有人合计每天最多寄几封（风控）
    letter_check_interval: int = 1800     # 每隔多少秒看一次要不要写信
    letter_max_tokens: int = 250

    # ---- QQ 空间日记：每天一条说说（图 + 文案），回复说说下的评论 ----
    qzone_enabled: bool = False           # 总开关。先用 /空间测试 测通接口，再在 .env 里改成 true
    qzone_post_window: str = "20:30-22:30"   # 每天在这段时间里随机挑一个时刻发（北京时间）
    qzone_daily_summary_at: str = "20:00"    # “日结”：把白天还没整理的聊天先整理成见闻
    qzone_image_dir: str = "qzone/images"    # 图库（相对 bot 目录）
    qzone_data_dir: str = "data/qzone"
    qzone_image_reuse_days: int = 60      # 同一张图多少天内不重复用
    qzone_length_weights: list[float] = [35, 45, 20]   # 一句话 / 两三句 / 小游记 的概率
    qzone_max_moments: int = 3            # 一条说说最多写几件今天的见闻
    qzone_private_moments: bool = True    # 私聊（只算有长期档案的人）也算见闻素材
    qzone_max_tokens: int = 350
    qzone_comment_reply: bool = True      # 回复说说下的评论
    qzone_comment_poll_minutes: float = 120.0   # 每隔多久查一次评论（空间没有评论推送，只能定时查；查太勤容易被风控）
    qzone_comment_quiet: str = "01:00-09:00"   # 这段时间不查也不回（像在睡觉）
    qzone_comment_days: int = 7           # 只管最近几天发的说说
    qzone_comment_daily_max: int = 10     # 每天最多回几条（自己说说下的评论 + 别人空间里的 @ 合计）
    qzone_comment_max_rounds: int = 3     # 同一个人在同一条说说下最多来回几轮
    qzone_comment_max_age_hours: float = 24.0  # 太久以前的评论不补回（刚开功能时，旧评论不会被翻出来回一遍）
    qzone_max_replies_per_poll: int = 2   # 每一轮最多回几条（评论和 @ 合计），剩下的留到下一轮
    qzone_reply_gap_min: float = 120.0    # 同一轮里两条回复之间隔多久（秒）
    qzone_reply_gap_max: float = 300.0
    qzone_risk_pause_hours: float = 24.0  # 熔断：空间一出现风控信号（限流码 -10049、验证页、403），空间功能自动停这么久
    qzone_mention_reply: bool = True      # 别人在自己的说说里 @ 她、在别人说说下 @ 她或回她：也按聊天规则回（查评论时顺便读“与我相关”）

    # ---- DeepSeek 高峰时段：伊蕾娜“很忙”，少回、短回 ----
    peak_enabled: bool = True
    peak_ranges: str = "09:00-12:00,14:00-18:00"   # 北京时间，仅周一至周五
    peak_holidays: list[str] = []         # 额外的法定节假日（YYYY-MM-DD），2026 年的已内置
    peak_user_interval: int = 600         # 高峰时每个人最多每隔多少秒得到一次正经回复
    peak_busy_prob: float = 0.5           # 高峰时被叫到，有多大概率只回一句“在忙”（不调用模型，零花费）
    peak_max_tokens: int = 50             # 高峰时正经回复的上限
    peak_extra_delay_min: float = 5.0     # 高峰时额外的回复延迟（秒），显得在忙
    peak_extra_delay_max: float = 20.0

    # ---- 出错通知 ----
    admin_alert_interval: int = 3600      # 余额不足/Key 失效时私信管理员，同类提醒最短间隔（秒）

    # ---- 没 @ 也回复（群聊）----
    smart_reply: bool = True              # 看得出是在跟她说话 / 提到她时也回复
    smart_names: list[str] = ["伊蕾娜", "灰之魔女", "魔女小姐", "Elaina", "elaina"]   # 消息里出现这些词算“提到她”（NICKNAME 里的名字也算）
    smart_window: int = 90                # 她在群里说完话后多少秒内，接着说的话也可能是在跟她说
    smart_min_interval: float = 60.0      # 同一个群“没 @ 也回复”的最短间隔（秒），防止刷屏
    smart_judge: bool = True              # 让模型先判断一下是不是在跟她说话（每次判断约 200 token）；关掉则提到名字就回
    followup_window: int = 30             # 某人叫了她之后，这么多秒内他接着发的话直接算在跟她说（不用判断）
    # 分条发送：收到消息先等一等，同一个人接着发的合成一条再回；每来一条新的重新计时
    merge_wait: float = 4.0               # 看不出话说完没说完时，等几秒
    merge_wait_complete: float = 3.0      # 看起来话说完了（问号结尾、完整的一句），等几秒
    merge_wait_incomplete: float = 8.0    # 看起来还没说完（“我跟你说”“然后”、逗号结尾、只叫了她一声），等几秒
    merge_wait_max: float = 20.0          # 从第一条算起最多等多久，到点就回

    # 不回水话：对方只说“哈哈”“嗯”“好的”或者话题自然结束时，她有时候不接话
    skip_filler: bool = True
    skip_filler_prob: dict[str, float] = {"disliked": 0.9, "stranger": 0.7, "acquaintance": 0.5, "close": 0.3}   # 明显的水话，直接不回的概率（不调用模型）
    skip_by_model: bool = True            # 短消息让她自己判断要不要接话（觉得没必要就不回）

    # ---- 触发范围 ----
    enable_group: bool = True             # 群聊是否回复（关掉后群里完全不说话，也不旁听、不判断）
    enable_private: bool = True           # 私聊是否回复
    private_friends_only: bool = True     # 私聊只回好友，不回群里发起的临时会话（临时会话更容易触发风控）
    group_whitelist: list[int] = []       # 只在这些群里工作；留空 = 所有群
    private_whitelist: list[int] = []     # 只回复这些 QQ 的私聊；留空 = 所有人

    # ---- 拟人：分条发送、长短变化 ----
    multi_message: bool = True            # 一次回复拆成几条消息发（像真人连发）
    max_bubbles: int = 3                  # 最多拆成几条
    split_prob: float = 0.6               # 模型没主动换行时，有几句话就按句拆开的概率
    drop_period_prob: float = 0.7         # 每条消息末尾的句号去掉的概率（真人聊天很少打句号）
    bubble_gap_min: float = 1.5           # 两条消息之间先停顿一下（秒），再按下一条的字数算打字时间
    bubble_gap_max: float = 3.5
    bubble_gap_cap: float = 12.0          # 两条之间最长隔多久

    # ---- 风控 / 拟人节奏 ----
    user_cooldown: float = 5.0            # 同一人两次触发的最小间隔（秒）
    global_rate_per_minute: int = 12      # 全局每分钟最多回复条数
    global_rate_per_hour: int = 60        # 全局每小时最多回复条数（降低被风控的概率）
    group_rate_per_hour: int = 40         # 每个群每小时最多回复条数
    farewell_on_limit: bool = True        # 快用完每小时限额时，最后补一句“我要上路了”之类的告别（不调用模型）
    reply_delay_min: float = 1.5          # 回复前随机“打字”延迟（秒）
    reply_delay_max: float = 4.0
    reply_delay_per_char: float = 0.25    # 打字速度：每个字多少秒（0.25 ≈ 每秒 4 个字，手机打字差不多这个速度）
    reply_delay_cap: float = 20.0
    switch_gap_min: float = 2.0           # 刚回完一个人、接着回另一个人时，中间再停一下（秒）
    switch_gap_max: float = 5.0
    queue_max_wait: float = 90.0          # 每分钟限额满了时，最多排队等多久再回（秒）；等不到就不回

    # ---- 未读消息：机器人不在线时别人发来的私聊，上线后补回 ----
    catchup_enabled: bool = True
    catchup_max_age_hours: float = 12.0   # 超过这么久的未读就不回了（太久了，回了也尴尬）
    catchup_max_people: int = 5           # 一次上线最多补回几个人
    catchup_gap_min: float = 20.0         # 补回两个人之间隔多久（秒）
    catchup_gap_max: float = 90.0
