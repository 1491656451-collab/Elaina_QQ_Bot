r"""
伊蕾娜语音合成服务（在你电脑上跑，10/20）

平时双击 开启语音服务.bat（后台运行）/ 关闭语音服务.bat；想看着窗口跑就双击 启动语音服务.bat。它做三件事：
1. 启动 GPT-SoVITS 的 api_v2（只在本机 127.0.0.1:9880），载入定稿的模型
2. 开一个小服务（127.0.0.1:9881），把"念什么"按定稿规则变成合成参数：
   去掉句尾句号；问句用 r11 参考、其余用 r8；整句合成；种子 3
   （规则和依据见 voice_data\样音对比报告-第6轮.md）
3. 用 SSH 反向隧道把 9881 接到服务器上的 127.0.0.1:9881，机器人就能调用；断了自动重连
窗口开着，她就能发语音；关掉或者电脑睡眠，她就只发文字。

本机设置在同目录的 本机配置.json（第一次运行自动生成，不上 GitHub，里面有和服务器对暗号用的 token）
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                   # D:\QQBot
CONF_PATH = os.path.join(HERE, "本机配置.json")
DATASET = os.path.join(ROOT, "bot", "voice_data", "elaina_dataset")

DEFAULT_CONF = {
    "gsv_dir": r"D:\GPT-SoVITS\GPT-SoVITS-v2pro-20250604",
    "gpt_weights": r"GPT_weights_v2ProPlus\elaina-e10.ckpt",
    "sovits_weights": r"SoVITS_weights_v2ProPlus\elaina_e2_s254.pth",
    "ref_statement": [os.path.join(DATASET, "wavs", "elaina_0025.wav"), "素直になる薬を超えるすごい薬を作りましたよ。"],
    "ref_question": [os.path.join(DATASET, "wavs", "elaina_0298.wav"), "この街に何があったのですか？それは記憶喪失ですか？"],
    "seed": 3,
    "top_k": 15,
    "api_port": 9880,
    "service_port": 9881,
    "remote_port": 9881,
    "server": "",            # 空着就读 D:\QQBot\server.txt（admin@服务器IP）
    "tunnel": True,
    "token": "",             # 第一次运行自动生成；服务器 .env 里的 VOICE_TTS_TOKEN 要和它一样
    "max_chars": 120,
}

LOG_LOCK = threading.Lock()
SYNTH_LOCK = threading.Lock()
STATE = {"api_ok": False, "tunnel": "未连接", "count": 0, "last": ""}


def log(msg: str):
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    with LOG_LOCK:
        print(line, flush=True)
        try:
            with open(os.path.join(HERE, "语音服务.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def load_conf() -> dict:
    conf = dict(DEFAULT_CONF)
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH, encoding="utf-8") as f:
            conf.update(json.load(f))
    changed = not os.path.exists(CONF_PATH)
    if not conf.get("token"):
        conf["token"] = secrets.token_hex(16)
        changed = True
    if changed:
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=1)
        log(f"已生成 {CONF_PATH}")
    if not conf.get("server"):
        p = os.path.join(ROOT, "server.txt")
        if os.path.exists(p):
            conf["server"] = open(p, encoding="utf-8").read().strip()
    return conf


# ------------------------------------------------------------------ GPT-SoVITS api_v2
def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def api_get(conf, path: str, timeout=120):
    url = f"http://127.0.0.1:{conf['api_port']}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def api_post(conf, path: str, body: dict, timeout=120):
    req = urllib.request.Request(f"http://127.0.0.1:{conf['api_port']}{path}",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def start_api(conf):
    if port_open(conf["api_port"]):
        log(f"{conf['api_port']} 端口已经有 api_v2 在跑，直接用")
        return None
    gsv = conf["gsv_dir"]
    py = os.path.join(gsv, "runtime", "python.exe")
    if not os.path.exists(py):
        raise SystemExit(f"没找到 {py}，在 本机配置.json 里改 gsv_dir")
    logf = open(os.path.join(HERE, "api_v2.log"), "w", encoding="utf-8", errors="replace")
    log("启动 GPT-SoVITS api_v2 ……（第一次大约要半分钟到一分钟）")
    p = subprocess.Popen([py, "api_v2.py", "-a", "127.0.0.1", "-p", str(conf["api_port"]), "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
                         cwd=gsv, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(100):
        time.sleep(3)
        if p.poll() is not None:
            raise SystemExit("api_v2 启动失败，看 voice_service\\api_v2.log 最后几行")
        if port_open(conf["api_port"]):
            return p
    raise SystemExit("等了 5 分钟 api_v2 还没起来，看 voice_service\\api_v2.log")


def load_models(conf):
    for kind, key in (("gpt", "gpt_weights"), ("sovits", "sovits_weights")):
        path = conf[key] if os.path.isabs(conf[key]) else os.path.join(conf["gsv_dir"], conf[key])
        code, body = api_get(conf, f"/set_{kind}_weights?weights_path=" + urllib.parse.quote(path))
        if code != 200:
            raise SystemExit(f"载入模型失败（{path}）：{body[:300]!r}")
        log(f"已载入 {os.path.basename(path)}")


def prepare(text: str) -> tuple[str, bool]:
    """定稿规则：去掉句尾句号；问号结尾算问句"""
    t = " ".join(text.split()).strip()
    question = t.endswith(("？", "?"))
    while t.endswith(("。", ".")):
        t = t[:-1]
    return t, question


def synth(conf, text: str) -> tuple[bytes, float]:
    t, question = prepare(text)
    ref, prompt = conf["ref_question"] if question else conf["ref_statement"]
    body = dict(text=t, text_lang="zh", ref_audio_path=ref, prompt_text=prompt, prompt_lang="ja",
                top_k=conf["top_k"], top_p=1.0, temperature=1.0, seed=conf["seed"], text_split_method="cut0",
                batch_size=1, speed_factor=1.0, repetition_penalty=1.35, media_type="wav", streaming_mode=False)
    t0 = time.time()
    with SYNTH_LOCK:                          # 显卡一次只念一句
        code, data = api_post(conf, "/tts", body)
    cost = time.time() - t0
    if code != 200:
        raise RuntimeError(data[:300].decode("utf-8", "replace"))
    return data, cost


# ------------------------------------------------------------------ 对外的小服务（只听本机，服务器经隧道过来）
def make_handler(conf):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _auth(self) -> bool:
            if self.headers.get("X-Token", "") != conf["token"]:
                self._json(403, {"ok": False, "err": "token 不对"})
                return False
            return True

        def do_GET(self):
            if self.path.startswith("/health"):
                if not self._auth():
                    return
                self._json(200, {"ok": port_open(conf["api_port"]), "gpt": os.path.basename(conf["gpt_weights"]),
                                 "sovits": os.path.basename(conf["sovits_weights"]), "count": STATE["count"]})
            else:
                self._json(404, {"ok": False})

        def do_POST(self):
            if not self.path.startswith("/speak"):
                return self._json(404, {"ok": False})
            if not self._auth():
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                text = str(req.get("text", "")).strip()
            except Exception:  # noqa: BLE001
                return self._json(400, {"ok": False, "err": "请求格式不对"})
            if not text or len(text) > conf["max_chars"]:
                return self._json(400, {"ok": False, "err": f"文字为空或超过 {conf['max_chars']} 字"})
            try:
                data, cost = synth(conf, text)
            except Exception as e:  # noqa: BLE001
                log(f"合成失败：{text}｜{e}")
                return self._json(500, {"ok": False, "err": str(e)})
            STATE["count"] += 1
            log(f"念了：{text}（{cost:.1f} 秒，{len(data) // 1024} KB）")
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Cost", f"{cost:.2f}")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):          # 不往窗口里刷每条访问记录
            pass
    return H


# ------------------------------------------------------------------ SSH 反向隧道
def tunnel_loop(conf, stop: threading.Event):
    ssh = shutil.which("ssh")
    if not ssh:
        log("没找到 ssh 命令，隧道开不了（Windows 设置 → 可选功能 → 安装 OpenSSH 客户端）")
        return
    if not conf.get("server"):
        log("没写服务器地址（server.txt 或 本机配置.json 的 server），隧道开不了")
        return
    fwd = f"127.0.0.1:{conf['remote_port']}:127.0.0.1:{conf['service_port']}"
    wait = 10
    while not stop.is_set():
        log(f"连接服务器隧道 {conf['server']} ……")
        STATE["tunnel"] = "连接中"
        t0 = time.time()
        p = subprocess.Popen([ssh, "-N", "-R", fwd, "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                              "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", conf["server"]],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        time.sleep(5)
        if p.poll() is None:
            STATE["tunnel"] = "已连接"
            log("隧道已连上，机器人现在可以发语音了")
        while p.poll() is None and not stop.is_set():
            time.sleep(1)
        if stop.is_set():
            p.terminate()
            break
        out = (p.stdout.read() or "").strip().splitlines()[-3:] if p.stdout else []
        STATE["tunnel"] = "断开"
        lived = time.time() - t0
        wait = 10 if lived > 120 else min(wait * 2, 300)
        log(f"隧道断了（{'；'.join(out) or '没有输出'}），{wait} 秒后重连")
        if any("remote port forwarding failed" in x for x in out):
            log("  （服务器上的 9881 还被上一次连接占着，一般等一两分钟服务器那边超时就好了）")
        stop.wait(wait)


def main():
    conf = load_conf()
    log("=" * 50)
    log("伊蕾娜语音服务启动")
    proc = start_api(conf)
    stop = threading.Event()
    try:
        load_models(conf)
        data, cost = synth(conf, "你好，我是伊蕾娜。")
        log(f"自检：合成一句用了 {cost:.1f} 秒（第一句会慢一点）")
        srv = ThreadingHTTPServer(("127.0.0.1", conf["service_port"]), make_handler(conf))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"本机服务：http://127.0.0.1:{conf['service_port']}")
        if conf.get("tunnel", True):
            threading.Thread(target=tunnel_loop, args=(conf, stop), daemon=True).start()
        log("服务已就绪。关掉它（关闭语音服务.bat，或者关掉这个窗口）= 她不发语音，只发文字")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if proc:
            proc.terminate()
        log("语音服务已停止")


if __name__ == "__main__":
    main()
