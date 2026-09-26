# 伊蕾娜 QQ 角色扮演机器人

一个基于 **NapCat + NoneBot2 + DeepSeek** 的 QQ 角色扮演聊天机器人。原本是为了扮演《魔女之旅》的伊蕾娜写的，但角色设定、知识库都可以换成你自己的。

她会像一个真人群友一样聊天：记得每个人、对陌生人冷淡对熟人随意、看得懂图、会发表情包，还会每天发一条 QQ 空间说说。

> ⚠️ **使用前请读完第 6 节“风险说明”。** 本项目通过非官方协议端登录 QQ，**有被封号的风险**，请只用小号。

---

## 1. 功能

| 功能 | 说明 |
|---|---|
| 群聊 / 私聊 | 被 @、被回复、被叫名字时回复；没 @ 也能看出是不是在跟她说话；同一个人分几条发会等说完再回 |
| 人设 | 一个 Markdown 文件写角色设定，改完发 `/重载人设` 即时生效 |
| 记忆 | 短期记忆（最近几条对话）+ 长期记忆（每个人的档案、每个群的往事），重启不丢 |
| 知识库 | 从角色档案、章节摘要、原文里检索相关内容，让角色“记得”原作 |
| 好感度 | 按聊天内容打分，分讨厌 / 陌生人 / 熟人 / 很熟四档，态度随之变化 |
| 拟人节奏 | 按字数算打字时间、拆成几条发、一次只回一个人、水话有时不接、掉线期间的私聊上线后补回 |
| 识图 | 本机模型认出图里是不是角色本人（免费），再用 DeepSeek 写一句描述 |
| 表情包 | 从小号的收藏表情里自动拉取、打情绪标签，聊天时偶尔甩一张 |
| QQ 空间日记 | 每天一条说说（图 + 根据当天聊天写的文案），回复说说下的评论和别人空间里的 @ |
| 主动写信 | 给“很熟”的好友偶尔私聊寄一封信 |
| 风控 | 个人冷却、全局 / 每群限速、高峰时段“很忙”模式、空间接口遇到限流自动熔断 24 小时 |
| 管理员指令 | 查看和修改记忆、好感、性别、表情标签，手动发说说等 |

## 2. 仓库里有什么、没有什么

**有**：全部代码（`bot/plugins/roleplay_chat/`）、配置模板（`bot/.env.example`）、启动脚本、示例人设（`bot/personas/default.md`），以及 `docs/` 里的部署指南和设计文档。

**没有**（出于版权或隐私原因，需要你自己准备）：

| 内容 | 放在哪 | 没有的话 |
|---|---|---|
| 角色人设 | `bot/personas/你的角色.md`，并在 `.env` 里设 `PERSONA_FILE` | 用 `personas/default.md` 示例角色，或者程序内置的一句话默认人设 |
| 角色档案 | `bot/knowledge/characters.md` | 知识库里少了角色资料 |
| 章节摘要 | `bot/knowledge/summaries/vol01.md`、`vol02.md`…… | 知识库里少了剧情摘要 |
| 原作文本 | `novel/*.txt`（和 `bot/` 同级） | 知识库里少了原文检索 |
| 说说图库 | `bot/qzone/images/` | 空间日记没图可发 |
| NapCat | `NapCat.Shell/`，去 NapCat 官方仓库下载 | 连不上 QQ |

知识库三样全都没有时，程序会跳过知识库、照常聊天。

## 3. 快速开始（Windows）

详细步骤、截图位置和常见问题见 **[docs/QQ机器人方案与部署指南.md](docs/QQ机器人方案与部署指南.md)**，这里是简版：

1. 安装 **Python 3.10+**（勾选 Add to PATH）和最新版 **Windows QQ**。
2. 下载 [NapCat](https://github.com/NapNeko/NapCatQQ/releases) 的 `NapCat.Shell.zip`，解压到仓库根目录的 `NapCat.Shell/`，运行 `launcher.bat` 用**小号**扫码登录。
3. 在 NapCat WebUI 里新建 **Websocket Client**：URL 填 `ws://127.0.0.1:8081/onebot/v11/ws`，Token 自己定一串。
4. 双击 `bot/start.bat`。第一次会自动建虚拟环境、装依赖，并生成 `bot/.env`。
5. 在 `.env` 里至少填好：
   ```
   ONEBOT_ACCESS_TOKEN=和 NapCat 里一致
   SUPERUSERS=["你的大号QQ"]
   DEEPSEEK_API_KEY=sk-...
   PERSONA_FILE=personas/你的角色.md
   GROUP_WHITELIST=[你的测试群号]
   ```
6. 再次双击 `start.bat`，看到 `Bot xxxx connected` 就连上了。

以后想免扫码：在 `NapCat.Shell/config/webui.json` 里把 `autoLoginAccount` 填成小号 QQ 号（部署指南 4.2 节）。

## 4. 写自己的角色

- **人设**：参考 `bot/personas/default.md`。建议写清楚：身份和处境、性格、喜好和雷点、说话方式（长度、口头禅）、对不同人的态度、几句示例台词、底线。
- **角色档案 `characters.md`**：每个角色一个二级标题（`## 角色名`），下面写外貌、说话方式、喜好等。话里提到这个名字时会自动带上。
- **章节摘要 `summaries/volNN.md`**：一卷一个文件，二级标题 `## 卷概要`、`## 章节摘要`；章节摘要下面每章一个三级标题（`### 第一章 标题`）。
- **原作文本**：放在 `novel/` 里的 `.txt`，程序会自动切段、建索引。

## 5. 文档

| 文档 | 内容 |
|---|---|
| [部署指南](docs/QQ机器人方案与部署指南.md) | 架构、部署步骤、全部功能说明、常见问题 |
| [版本说明与管理员指令](docs/版本说明与管理员指令.md) | 版本演进、全部管理员指令、全部参数 |
| [表情包功能方案](docs/表情包功能方案.md) | 表情包的设计 |
| [QQ 空间日记功能方案](docs/QQ空间日记功能方案.md) | 空间日记的设计、接口、熔断 |
| [问题排查记录：沙耶设定错误](docs/问题排查记录-沙耶设定错误.md) | 一次角色设定幻觉的排查过程 |

所有参数的默认值都在 `bot/plugins/roleplay_chat/config.py`，`bot/.env.example` 里有常用项的说明。

## 6. 风险说明

- **封号风险**：NapCat 是非官方的 QQ 协议端，腾讯可能检测到并下线、限制甚至封禁账号。实际遇到过“设备存在外挂或其他软件影响QQ正常使用”的下线。请**只用小号**，不要用常用号。
- **降低风险的做法**：先只在自己的小群里用（设 `GROUP_WHITELIST`）；不要让机器人主动加群、加好友、群发；不要公开号召别人加它好友；保持默认的限速；QQ 空间的写操作（发说说、评论）风险比聊天高，出问题时先关掉 `QZONE_ENABLED`。
- **隐私**：机器人会把聊天记录和对每个人的印象存在本地 `bot/data/` 里，请告知你的群友，也不要把这个文件夹传到网上。
- **费用**：DeepSeek 按用量收费，小群日常聊天一个月通常几块钱。
- **版权**：如果你扮演的是有版权的角色，人设、知识库和原作文本请只自用，不要公开分发。

## 7. 致谢

- [NoneBot2](https://github.com/nonebot/nonebot2)：机器人框架
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)：QQ 协议端
- [DeepSeek](https://platform.deepseek.com/)：大模型
- [SmilingWolf/wd-swinv2-tagger-v3](https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3)：本机角色识别模型（首次启动自动下载）

## 8. 许可证

[MIT](LICENSE)
