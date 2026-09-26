"""识图测试：看本机角色识别对几张图给出的分数（不连 QQ、不花钱）

用法：把一张或几张图片拖到 bot 文件夹里的「识图测试.bat」上。
会列出每张图最像的 5 个角色和伊蕾娜的置信度，并按 .env 里的门槛说明机器人会怎么处理。
"""
import importlib.util
import re
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent


def env(name: str, default: float) -> float:
    try:
        text = (BOT / ".env").read_text(encoding="utf-8")
        m = re.search(rf"^\s*{name}\s*=\s*([0-9.]+)", text, re.M)
        return float(m.group(1)) if m else default
    except OSError:
        return default


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print("把图片拖到「识图测试.bat」上就能测试。")
        return
    spec = importlib.util.spec_from_file_location("tagger", BOT / "plugins" / "roleplay_chat" / "tagger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    threshold = env("VISION_TAGGER_THRESHOLD", 0.75)
    maybe = env("VISION_TAGGER_MAYBE", 0.35)
    t = mod.Tagger(BOT / "data" / "models", "SmilingWolf/wd-swinv2-tagger-v3", [], threshold=0.0, auto_download=False)
    try:
        t._load()
    except Exception as e:  # noqa: BLE001
        print(f"模型加载失败：{e}\n请确认 data\\models\\wd-swinv2-tagger-v3\\ 里有 model.onnx 和 selected_tags.csv")
        return
    t.threshold = 0.01
    for p in paths:
        print("\n" + "=" * 60 + f"\n{p.name}")
        try:
            found, self_score = t._detect_sync(p.read_bytes())
        except Exception as e:  # noqa: BLE001
            print(f"  读不了这张图：{e}")
            continue
        found = sorted(found, key=lambda x: -x[1])
        for name, score in found[:5]:
            print(f"  {score:5.2f}  {name}")
        if not found:
            print("  （没有哪个角色超过 0.01）")
        if self_score >= threshold:
            verdict = "直接认成伊蕾娜"
        elif self_score >= maybe:
            verdict = "交给看图模型再确认是不是她"
        else:
            verdict = "当作不是伊蕾娜"
        print(f"  伊蕾娜：{self_score:.2f}（门槛 {threshold}，再确认线 {maybe}）→ {verdict}")


if __name__ == "__main__":
    main()
