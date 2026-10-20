"""去掉句尾的“哈气”。

GPT-SoVITS 合成的句子，最后一个字的元音唱完以后，常拖着一段 0.1～0.3 秒、没有音高的气流声，
听起来像说完话呼一口气。做法：
  1. 把声音低通到 900Hz 以下，在这里面找“有音高（周期性强）”的帧——气声是噪声，低频里没有周期；
  2. 最后一个有音高的帧就是元音结束的地方；
  3. 从那里往后留 keep_ms，再用 fade_ms 淡出，后面补一点静音。
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt


def _voicing(x, sr):
    xl = sosfiltfilt(butter(4, 900, "low", fs=sr, output="sos"), x)
    hop, win = int(sr * 0.01), int(sr * 0.04)
    w = np.hanning(win)
    lo, hi = int(sr / 500), int(sr / 70)
    n = max(0, (len(x) - win) // hop)
    e = np.empty(n)
    v = np.empty(n)
    for i in range(n):
        f = xl[i * hop:i * hop + win] * w
        ac = np.correlate(f, f, "full")[win - 1:]
        e[i] = 10 * np.log10(np.mean(f * f) + 1e-12)
        v[i] = ac[lo:hi].max() / (ac[0] + 1e-12)
    return hop, win, e, v


def trim_tail(x: np.ndarray, sr: int, keep_ms: float = 20, fade_ms: float = 80, pad_ms: float = 150,
              vthr: float = 0.75) -> tuple[np.ndarray, float]:
    """x：单声道 float。返回 (处理后的音频, 被淡掉的尾巴有多少秒)。找不到就原样返回。"""
    if x.ndim > 1:
        x = x.mean(axis=1)
    hop, win, e, v = _voicing(x, sr)
    if not len(e):
        return x, 0.0
    good = np.where((v >= vthr) & (e > e.max() - 30))[0]
    if not len(good):
        return x, 0.0
    end_voiced = good[-1] * hop + win // 2              # 帧中心
    nz = np.nonzero(np.abs(x) > 1e-4)[0]
    end_sound = nz[-1] + 1 if len(nz) else len(x)
    cut = min(end_sound, end_voiced + int(sr * keep_ms / 1000))
    stop = min(end_sound, cut + int(sr * fade_ms / 1000))
    y = x[:stop].copy()
    if stop > cut:
        y[cut:stop] *= np.cos(np.linspace(0, np.pi / 2, stop - cut)) ** 2
    return np.concatenate([y, np.zeros(int(sr * pad_ms / 1000))]), max(0.0, (end_sound - cut) / sr)


# ---------------------------------------------------------------- 档位（第 8 轮你听出来的）
# 陈述句用重剪；问句用轻剪（句尾上扬，剪太狠会把语气剪没）
PRESETS = {
    "重": dict(keep_ms=0, fade_ms=40, vthr=0.85),
    "中": dict(keep_ms=20, fade_ms=80, vthr=0.75),
    "轻": dict(keep_ms=60, fade_ms=150, vthr=0.75),
}


def preset_for(text: str) -> str:
    return "轻" if text.rstrip().endswith(("？", "?")) else "重"


def trim_wav_bytes(data: bytes, preset: str = "重") -> tuple[bytes, float]:
    """输入/输出都是 16 位 PCM 的 wav 字节（GPT-SoVITS api_v2 给的就是这种）。"""
    import io
    import wave
    with wave.open(io.BytesIO(data)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        return data, 0.0
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    y, removed = trim_tail(x, sr, **PRESETS[preset])
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return out.getvalue(), removed


def trim_wav_file(path: str, preset: str = "重") -> float:
    with open(path, "rb") as f:
        data = f.read()
    new, removed = trim_wav_bytes(data, preset)
    with open(path, "wb") as f:
        f.write(new)
    return removed
