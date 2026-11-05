# 调研：VoiceStudio 和现在的语音克隆方案比（10/31）

> 2026-10-31 写（同日重写：用户确认说的是 debpalash/VoiceStudio，按它的 GitHub 文档重新整理）。只看了网页文档，没装、没试听。资料来源见文末。

## 1. VoiceStudio 是什么

[debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio)：自称"开源、完全本地的 ElevenLabs 替代品"，一个带界面的桌面应用，能做声音克隆、声音设计、配音、听写、有声书。

- **它本身没有模型**，是把别人的开源模型打包在一个界面里，可以切换：
  - 默认：**OmniVoice**（k2-fsa），零样本克隆，号称 600 多种语言。
  - 还能选：**CosyVoice 3**、**IndexTTS 2.5**（能克隆还能控制情绪）、VoxCPM2、MOSS-TTS 等。
  - 也能接**外部的 GPT-SoVITS 服务**——也就是可以接你现在这套。
- **只做零样本克隆**：给一段参考录音（OmniVoice 建议 3～10 秒，带文字最多 20 秒）。文档里**没有**"拿自己的素材训练"的功能。
- **中文**：支持，还能用拼音加声调纠正读音（比如"打ZHE2出售"）。**日语**：我看的几页没写（语言列表那页没看）。**拿日语录音说中文**：没明说，只说参考录音和指令冲突时以参考录音为准。
- **Windows**：有原生安装包（`VoiceStudio-0.5.7-win-x64.exe`），也有 PowerShell 一键安装。要约 10 GB 硬盘。
- **显卡**：只认 NVIDIA；OmniVoice 建议显存至少 **6 GB**。你的 4060 是 8 GB，够用，但**和现在的 GPT-SoVITS 语音服务同时开大概会挤**，试的时候先把语音服务关掉。
- **给程序调用的接口**：第三方安装文章说有和 OpenAI 一样的 `POST /v1/audio/speech`（端口 3900）；它自己的文档我只看到语音转文字的接口，合成接口那页没看到。接进机器人前要再确认。
- **许可**：应用是 AGPL-3.0；默认的 OmniVoice 模型第一次用要在界面里点同意它的许可（第三方文章说是非商用），有"AI 水印"功能。你自己用，这些都不影响。
- **活跃度**：最新 v0.5.7（10/26），提交很多、更新很快，属于还在快速变化的项目。

## 2. 和现在的方案比

现在的方案：GPT-SoVITS v2ProPlus，用广播剧里筛出来的 634 句伊蕾娜原声**专门训练过**，在你电脑上跑；念日语、后面附中文文字；短句被吞会换种子重念，名台词用原句当参考。

| 看什么 | 现在（GPT-SoVITS 训练过） | VoiceStudio（零样本） |
|---|---|---|
| **像不像她** | 用了半小时她本人的声音训练，语气、口癖学得进去；你评价日语"很完美" | 只听 3～10 秒，音色能像，语气、说话习惯一般学不到这么细。**多半不如现在** |
| **说中文** | 卷舌音、鼻音念不准，所以改成念日语 | CosyVoice 3、OmniVoice 本身中文好，还能用拼音纠音。**可能比现在好**，要试 |
| **带情绪** | 不分情绪，同一句话一个调（只按问句、名台词挑参考） | IndexTTS 2.5 能控制情绪，正好对应《DS架构评价核对》第五条"语音没有情绪" |
| 接进机器人 | 已经接好：一键开关、服务器隧道调用、各种兜底 | 要重新接；接口要先确认 |
| 显存 | 8 GB 够 | 至少 6 GB，和现在的服务不能同时开 |
| 稳定性 | 已经调好，不常改 | 新项目，版本变得快 |

## 3. 结论

- **日语语音：不换。** 现在是用她本人的声音专门训练过的，零样本工具在"像不像"上一般比不过，而你已经觉得日语很好。
- **VoiceStudio 很适合拿来"试一试"**：一个界面里就能换着试 OmniVoice、CosyVoice 3、IndexTTS 2.5，不用一个个装。值得试的两件事：
  1. **中文**：用现在的参考音频（`elaina_0025.wav` 等），合成第 9 轮样音对比里那 12 句中文，三个模型各来一遍，和现在的版本放在一起盲听。念得准、听着像她，才考虑把"中文语音"接回来。
  2. **情绪**：用 IndexTTS 2.5 同一句话合成"开心 / 嫌弃 / 得意"几种，听听有没有区别、还像不像她。
- 试之前先关掉现在的语音服务（`关闭语音服务.bat`），免得两边抢显存。
- 试出来好的，再让语音 agent 查清它的合成接口、写接入方案；不好就算了，现在的日语方案照用。
- 归语音 agent，不急。

## 资料

- [debpalash/VoiceStudio（GitHub）](https://github.com/debpalash/VoiceStudio)：README、docs/feature-catalog.md、docs/engines/README.md、docs/install/windows.md、docs/speech-platform.md、docs/engines/omnivoice.md、最新发布页
- [ComputingForGeeks：Install VoiceStudio](https://computingforgeeks.com/install-voicestudio/)（第三方安装实测，10/24 更新：显存、合成接口）
- 同名的另一个项目 [LatentForge/VoiceStudio](https://pypi.org/project/voicestudio/5.16.0/) 是 Python 工具包，不是这个
