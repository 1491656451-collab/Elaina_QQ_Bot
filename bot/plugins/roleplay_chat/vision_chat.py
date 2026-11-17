"""识图纠正 /认图（10/22 从 __init__.py 拆出来）。看图本身在 vision.py。11/17 加了 /认图 描述（手写个别图的描述）"""
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from .core import cfg, vision


see_cmd = on_command("认图", rule=to_me(), permission=SUPERUSER, priority=5, block=True)


SEE_USAGE = ("用法：引用一张图（或和图一起发）+ /认图 → 看她认出了什么；"
             "/认图 是 → 以后这张图就当成她自己；/认图 不是 → 以后不当成她；"
             "/认图 描述 闭眼张嘴、一脸无奈 → 以后这张图就用这句描述（看图模型老是看错的个别图用）；"
             "/认图 取消 → 恢复自动识别（手写的描述也一起删掉）")


@see_cmd.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    images = [seg.data for seg in event.get_message() if seg.type == "image"]
    reply = getattr(event, "reply", None)
    if reply is not None:
        images += [seg.data for seg in reply.message if seg.type == "image"]
    if not images:
        await see_cmd.finish(SEE_USAGE)
    data = images[0]
    word = arg.extract_plain_text().strip()
    if word in ("是", "是她", "是伊蕾娜", "伊蕾娜"):
        ok = vision.set_override(data, True)
        await see_cmd.finish("（记下了：这张图画的是伊蕾娜。下次看到会按“她自己的画像”来描述）" if ok else "（这张图没法记，缺少图片编号）")
    if word in ("不是", "否", "不是她"):
        ok = vision.set_override(data, False)
        await see_cmd.finish("（记下了：这张图画的不是伊蕾娜）" if ok else "（这张图没法记，缺少图片编号）")
    if word.startswith("描述"):           # 11/17 手写描述
        text = word[2:].strip(" ：:")
        if not text:
            await see_cmd.finish("（在“描述”后面写上这张图该怎么描述，比如：/认图 描述 闭眼张嘴、一脸无奈）")
        if vision.tagging:
            await vision.shows_self(data)      # 先本机认一次人，手写的描述才知道要不要加“伊蕾娜本人的画像”
        ok = await vision.set_manual(data, text)
        await see_cmd.finish(f"（记下了：以后看到这张图就说“{text}”，不再让看图模型看）" if ok else "（这张图没法记，缺少图片编号）")
    if word in ("取消", "清除", "自动"):
        await vision.set_manual(data, None)
        vision.set_override(data, None)
        await see_cmd.finish("（已恢复自动识别，下次看到会重新看一遍）")
    if word:
        await see_cmd.finish(SEE_USAGE)
    if vision.tagging:
        await vision.shows_self(data)      # 还没认过的图，先本机认一次（不花钱）
    e = vision.info(data)
    if not e:
        await see_cmd.finish("（这张图她还没看过，也没法下载了）")
    who = "；".join(e.get("c", [])) or "没认出角色"
    judged = "管理员指定" + ("是" if e["o"] else "不是") + "伊蕾娜" if "o" in e else ("算伊蕾娜" if vision.is_self(e) else "不算伊蕾娜")
    lines = [
        f"本机识别：{who}（伊蕾娜置信度 {e.get('s', 0):.2f}，门槛 {cfg.vision_tagger_threshold}，再确认线 {cfg.vision_tagger_maybe}）" if e.get("t") else "本机识别：还没认过（模型没准备好时看的图）",
        f"结论：{judged}" + (f"（底片比对：和她图库里的 {e['lib']} 是同一张图）" if e.get("lib") else ""),
        f"她看到的（管理员手写）：{e['dm']}" if e.get("dm") else (f"她看到的：{e['d']}" if e.get("d") else "她看到的：还没描述过"),
    ]
    await see_cmd.finish("（" + "\n".join(lines) + "）")
