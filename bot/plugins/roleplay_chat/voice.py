"""
语音：让她能发 QQ 语音条（10/19 第 0 步：先打通发送）

- 发送：OneBot 的 record 消息段，音频用 base64 交给 NapCat。QQ 语音是 silk 格式，NapCat 会自己转：
  24kHz 等常见采样率的单声道 16 位 wav 它直接编码，不用 ffmpeg；mp3、ogg 这类要靠 ffmpeg 转，
  所以 /语音 测试（wav）和 /语音 某个.mp3 分开测，哪一路不通一眼就能看出来
- 测试音频放在 data/voice/test/（data 不上 GitHub；以后克隆用的原声素材也放 data/voice/ 下）
- 以后接 TTS：合成出来的音频同样走 segment()，这一层不用再改
"""
from __future__ import annotations

import base64
import io
import math
import struct
import wave
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment

AUDIO_EXTS = (".wav", ".mp3", ".silk", ".amr", ".ogg", ".m4a", ".flac")
MAX_BYTES = 8 * 1024 * 1024          # 再大的音频不发：QQ 语音最长 60 秒左右，正常远到不了这个数


def test_tone(rate: int = 24000, seconds: float = 2.4) -> bytes:
    """生成一段测试用的 wav（单声道、16 位、24kHz）：“叮—咚—叮”三个音，首尾淡入淡出。
    不依赖任何库和网络，只用来确认语音条能发出去、能播放"""
    notes = [(784.0, 0.6), (659.3, 0.6), (1046.5, 1.0)]       # G5 E5 C6
    gap = 0.1
    frames = bytearray()
    for freq, dur in notes:
        n = int(rate * dur)
        for i in range(n):
            t = i / rate
            env = min(1.0, i / (0.02 * rate)) * math.exp(-3.0 * t / dur)    # 起音快、慢慢衰减，像铃声
            v = 0.45 * env * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
        frames += b"\x00\x00" * int(rate * gap)
    total = len(frames) // 2
    want = int(rate * seconds)
    if total < want:
        frames += b"\x00\x00" * (want - total)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def segment(data: bytes) -> MessageSegment:
    """把一段音频（wav / mp3 / silk……的原始字节）包成语音消息段"""
    return MessageSegment.record("base64://" + base64.b64encode(data).decode())


def wav_info(data: bytes) -> str:
    """wav 的采样率、声道、时长，方便排查；不是 wav 就返回空"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            sec = w.getnframes() / float(w.getframerate())
            return f"{w.getframerate()}Hz、{'单' if w.getnchannels() == 1 else str(w.getnchannels())}声道、{w.getsampwidth() * 8} 位、{sec:.1f} 秒"
    except Exception:  # noqa: BLE001
        return ""


class VoiceFiles:
    """data/voice/test/ 里放的测试音频"""

    def __init__(self, root: Path):
        self.root = root
        self.test_dir = root / "test"

    def list_test(self) -> list[str]:
        if not self.test_dir.is_dir():
            return []
        return sorted(p.name for p in self.test_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)

    def find_test(self, name: str) -> Path | None:
        """按文件名找；只写了一部分（不带后缀、开头几个字）也行，唯一匹配才算"""
        names = self.list_test()
        if name in names:
            return self.test_dir / name
        hits = [n for n in names if n.startswith(name) or Path(n).stem == name]
        return self.test_dir / hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ 10/20：接你电脑上的语音合成服务
# 你电脑上跑 voice_service\启动语音服务.bat（GPT-SoVITS + 定稿规则），用 SSH 反向隧道接到服务器的 127.0.0.1:9881。
# 电脑关着、隧道断了：available() 为 False，她就只发文字。

import json as _json
import random as _random
import time as _time

import httpx as _httpx


class TTSClient:
    def __init__(self, url: str, token: str, timeout: float = 20.0, check_interval: float = 60.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.check_interval = check_interval
        self._ok = False
        self._checked = 0.0
        self.info: dict = {}
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def available(self, force: bool = False) -> bool:
        """问一下电脑上的服务在不在；结果缓存 check_interval 秒，免得每条消息都去问"""
        if not self.configured:
            return False
        now = _time.monotonic()
        if not force and now - self._checked < self.check_interval:
            return self._ok
        self._checked = now
        try:
            async with _httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(self.url + "/health", headers={"X-Token": self.token})
            self.info = r.json() if r.status_code == 200 else {}
            self._ok = r.status_code == 200 and bool(self.info.get("ok"))
            self.last_error = "" if self._ok else f"HTTP {r.status_code} {r.text[:100]}"
        except Exception as e:  # noqa: BLE001
            self._ok, self.info, self.last_error = False, {}, f"{type(e).__name__}: {e}"
        return self._ok

    async def speak(self, text: str) -> tuple[bytes | None, str]:
        """合成一句，返回 (wav, 说明)。失败返回 (None, 原因)，并把服务标成暂时不可用"""
        if not self.configured:
            return None, "没配置 VOICE_TTS_URL / VOICE_TTS_TOKEN"
        t0 = _time.monotonic()
        try:
            async with _httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.url + "/speak", headers={"X-Token": self.token}, json={"text": text})
        except Exception as e:  # noqa: BLE001
            self._ok, self._checked = False, _time.monotonic()
            self.last_error = f"{type(e).__name__}: {e}"
            return None, self.last_error
        if r.status_code != 200:
            try:
                err = r.json().get("err", "")
            except Exception:  # noqa: BLE001
                err = r.text[:100]
            return None, f"HTTP {r.status_code} {err}"
        return r.content, f"合成 {r.headers.get('X-Cost', '?')} 秒，往返 {_time.monotonic() - t0:.1f} 秒"


def _norm_interjection(s: str) -> str:
    return s.strip().replace("?", "？").replace("!", "！").replace("...", "……").replace("…", "……").replace("…………", "……")


class Interjections:
    """单个语气词用她的原声（data/voice/interjections/：Y02_は.wav…… + 语气词表.json）"""

    def __init__(self, root: Path):
        self.root = root
        self.table: dict[str, list[str]] = {}
        self.load()

    def load(self) -> int:
        self.table = {}
        p = self.root / "语气词表.json"
        if not p.exists():
            return 0
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0
        for item in data.get("表", []):
            files = [f for f in item.get("文件", []) if (self.root / f).exists()]
            if not files:
                continue
            for w in item.get("写法", []):
                self.table[_norm_interjection(w)] = files
        return len(self.table)

    def match(self, text: str) -> Path | None:
        files = self.table.get(_norm_interjection(text))
        return self.root / _random.choice(files) if files else None
