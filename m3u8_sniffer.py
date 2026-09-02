# -*- coding: utf-8 -*-
"""
通用 m3u8 前置嗅探模块（零第三方依赖，仅用标准库）。

原理：启动一个 headless Chrome（通过远程调试端口 / CDP），用原生 WebSocket
监听 Network 事件。页面在浏览器里真实播放视频时，会向带鉴权参数（timestamp、
SecurityKey、encrypt、token 等）的 .m3u8 地址发起请求，我们把它拦截下来。

拿到真实 m3u8 后，交给上层无缝传给 yt-dlp 下载。

对外主入口：
    sniff_m3u8(url, on_log=None, timeout=45, proxy=None, prefer_master=True)
        -> SniffResult | None
"""

import os
import sys
import json
import time
import socket
import base64
import struct
import shutil
import tempfile
import subprocess
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ----------------------------------------------------------------------
# 浏览器可执行文件探测
# ----------------------------------------------------------------------
def _candidate_browsers():
    cands = []
    if sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif sys.platform.startswith("win"):
        pf = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
              os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            if not base:
                continue
            cands += [
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Chromium", "Application", "chrome.exe"),
            ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "brave-browser"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    return [c for c in cands if c and os.path.exists(c)]


def find_browser():
    cands = _candidate_browsers()
    return cands[0] if cands else None


def is_sniffable(url):
    """明显已经是直链 m3u8 / mp4 的就不必再嗅探。"""
    low = url.lower().split("?")[0]
    return not low.endswith((".m3u8", ".mp4", ".ts", ".flv"))


# ----------------------------------------------------------------------
# 极简 WebSocket 客户端（仅实现 CDP 所需的文本帧收发）
# ----------------------------------------------------------------------
class _WS:
    def __init__(self, url, timeout=15):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.sock.settimeout(1.0)
        self._buf = b""

    def send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def frames(self):
        """生成器：逐个 yield 解码后的文本帧；超时 yield None。"""
        payload = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                yield None
                continue
            except OSError:
                return
            if not chunk:
                return
            self._buf += chunk
            while True:
                if len(self._buf) < 2:
                    break
                b1, b2 = self._buf[0], self._buf[1]
                fin = b1 & 0x80
                op = b1 & 0x0F
                masked = b2 & 0x80
                ln = b2 & 0x7F
                idx = 2
                if ln == 126:
                    if len(self._buf) < 4:
                        break
                    ln = struct.unpack(">H", self._buf[2:4])[0]
                    idx = 4
                elif ln == 127:
                    if len(self._buf) < 10:
                        break
                    ln = struct.unpack(">Q", self._buf[2:10])[0]
                    idx = 10
                if masked:
                    if len(self._buf) < idx + 4:
                        break
                    mkey = self._buf[idx:idx + 4]
                    idx += 4
                else:
                    mkey = None
                if len(self._buf) < idx + ln:
                    break
                data = self._buf[idx:idx + ln]
                if mkey is not None:
                    data = bytes(b ^ mkey[i % 4] for i, b in enumerate(data))
                self._buf = self._buf[idx + ln:]

                if op == 0x9:  # ping -> pong
                    pong = bytearray([0x8A])
                    pm = os.urandom(4)
                    pong.append(0x80 | len(data))
                    pong += pm
                    self.sock.sendall(bytes(pong) +
                                      bytes(b ^ pm[i % 4] for i, b in enumerate(data)))
                    continue
                if op == 0xA:  # pong
                    continue
                if op == 0x8:  # close
                    return
                if op in (0x1, 0x0, 0x2):
                    payload += data
                    if fin:
                        try:
                            yield payload.decode("utf-8", "ignore")
                        except Exception:
                            pass
                        payload = b""

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 结果对象
# ----------------------------------------------------------------------
class SniffResult:
    def __init__(self, m3u8_url, page_url, headers=None, title=""):
        self.m3u8_url = m3u8_url
        self.page_url = page_url
        self.headers = headers or {}
        self.title = title or ""

    def ytdlp_headers(self):
        """把浏览器请求 m3u8 用的防盗链头翻译成 yt-dlp 参数。
        只注入 Referer/Origin；User-Agent 由下载引擎统一设置。"""
        out = []
        h = self.headers or {}
        if "://" in self.page_url:
            from urllib.parse import urlsplit
            sp = urlsplit(self.page_url)
            default_origin = f"{sp.scheme}://{sp.netloc}"
        else:
            default_origin = ""
        ref = h.get("Referer") or (default_origin + "/")
        if ref:
            out += ["--add-header", f"Referer:{ref}"]
        origin = h.get("Origin") or default_origin
        if origin:
            out += ["--add-header", f"Origin:{origin}"]
        return out

    def __repr__(self):
        return f"<SniffResult m3u8={self.m3u8_url[:80]}...>"


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
_PLAY_JS = """
(() => {
  const vids = document.querySelectorAll('video');
  vids.forEach(v => {
    try {
      v.muted = true;
      const p = v.play();
      if (p && p.catch) p.catch(()=>{});
    } catch(e) {}
  });
  // 有些播放器把视频放在点击按钮后才加载，尝试点一下播放按钮
  const btns = document.querySelectorAll('[class*="play" i], [class*="player"] button, .vjs-big-play-button');
  btns.forEach(b => { try { b.click(); } catch(e) {} });
})();
"""


def sniff_m3u8(url, on_log=None, timeout=45, proxy=None, settle_seconds=2.5):
    """在 headless 浏览器里打开 url，拦截真实 .m3u8 地址。

    on_log: 可选回调 callable(str)，用于回传进度日志。
    返回 SniffResult 或 None。
    """
    def log(msg):
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    browser = find_browser()
    if not browser:
        log("⚠️ 未找到可用的 Chrome/Edge 浏览器，无法嗅探 m3u8。")
        return None

    port = _free_port()
    udd = tempfile.mkdtemp(prefix="ytl-cdp-")
    args = [
        browser,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--mute-audio",
        "--disable-background-networking",
        "--disable-extensions",
        "--autoplay-policy=no-user-gesture-required",
        f"--user-agent={UA}",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append("about:blank")

    proc = None
    try:
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"⚠️ 浏览器启动失败: {e}")
            return None

        wsurl = _wait_devtools(port, log)
        if not wsurl:
            return None

        ws = _WS(wsurl)
        mid = 0

        def cmd(method, params=None):
            nonlocal mid
            mid += 1
            ws.send({"id": mid, "method": method, "params": params or {}})
            return mid

        cmd("Network.enable")
        cmd("Page.enable")
        cmd("Runtime.enable")
        cmd("Page.navigate", {"url": url})

        found_url = None
        found_headers = None
        page_title = ""
        title_req_id = None
        seen = set()
        deadline = time.time() + timeout
        last_play = 0.0
        settled_at = None

        for frame in ws.frames():
            now = time.time()
            if now > deadline:
                break
            if frame is None:
                # 空闲时周期性注入“播放”动作，触发懒加载视频
                if now - last_play > 2.0:
                    last_play = now
                    cmd("Runtime.evaluate", {"expression": _PLAY_JS})
                # 找到 m3u8 后取一次页面标题
                if found_url and title_req_id is None:
                    title_req_id = cmd("Runtime.evaluate", {
                        "expression": "document.title",
                        "returnByValue": True})
                if found_url and (settled_at is None):
                    settled_at = now
                if found_url and settled_at and now - settled_at >= settle_seconds:
                    break
                continue

            try:
                msg = json.loads(frame)
            except Exception:
                continue

            # 标题回执
            if title_req_id and msg.get("id") == title_req_id:
                page_title = (msg.get("result", {})
                              .get("result", {}).get("value") or "")
                title_req_id = None

            method = msg.get("method")
            if method == "Network.requestWillBeSent":
                req = msg["params"].get("request", {})
                u = req.get("url", "")
                if _looks_like_m3u8(u) and u not in seen:
                    seen.add(u)
                    # 优先选主播放列表（含多码率特征），简单地取第一个非子分片
                    if found_url is None or _rank(u) > _rank(found_url):
                        found_url = u
                        found_headers = dict(req.get("headers", {}))
                        log(f"🛰️ 嗅探到 m3u8（含鉴权参数）")
                    settled_at = None
            elif method == "Network.responseReceived":
                resp = msg["params"].get("response", {})
                u = resp.get("url", "")
                mime = (resp.get("mimeType") or "").lower()
                if (".m3u8" in u or "mpegurl" in mime or "vnd.apple.mpegurl" in mime) \
                        and u not in seen:
                    seen.add(u)
                    if found_url is None or _rank(u) > _rank(found_url):
                        found_url = u
                        log(f"🛰️ 嗅探到 m3u8（响应类型）")
                    settled_at = None

        ws.close()

        if found_url:
            found_url = _resolve_media_url(found_url, log, proxy=proxy)
            log("✅ m3u8 前置解析完成，准备交接给下载引擎。")
            return SniffResult(found_url, url, found_headers, title=page_title)
        log("ℹ️ 浏览器嗅探未发现 m3u8 流（可能是非 HLS 视频或需登录）。")
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(udd, ignore_errors=True)
        except Exception:
            pass


def _rank(u):
    """越大越像“主播放列表”。带子分片序号特征的排后。"""
    low = u.lower()
    score = 0
    if "m3u8" in low:
        score += 10
    # 子分片通常是 .ts / 含 -/segment；主表通常含 master/index/ 多码率关键字
    if any(k in low for k in ("master", "index", "playlist", "multi")):
        score += 5
    if low.count("/") > 6:
        score += 1
    return score



def _http_get_text(url, timeout=15, proxy=None):
    """用标准库 GET，返回 (status, text, final_url)。"""
    import urllib.request as _u
    opener = None
    if proxy:
        opener = _u.build_opener(_u.ProxyHandler({"http": proxy, "https": proxy}))
    req = _u.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.miguvideo.com/",
        "Accept": "*/*",
    })
    fn = opener.open if opener else _u.urlopen
    with fn(req, timeout=timeout) as r:
        data = r.read()
        final = r.geturl()
    return data.decode("utf-8", "ignore"), final


def _resolve_media_url(m3u8_url, log, proxy=None, hops=3):
    """有些站点（如咪咕 GSLB）抓到的地址响应体不是 m3u8，而是一行真实媒体 URL。
    跟随这种“正文跳转”，直到拿到真正以 #EXTM3U 开头的播放列表。"""
    cur = m3u8_url
    for _ in range(hops):
        try:
            text, final = _http_get_text(cur, proxy=proxy)
        except Exception as e:
            log(f"⚠️ 校验 m3u8 失败: {e}")
            return cur
        body = text.strip()
        if body.startswith("#EXTM3U"):
            return cur  # 已经是真正的播放列表
        # 正文是一行 http(s) 链接 -> 跟进
        first = body.splitlines()[0].strip() if body else ""
        if first.startswith("http://") or first.startswith("https://"):
            log("🔀 跟随媒体调度跳转...")
            cur = first
            continue
        # 既不是 m3u8 也不是跳转链接，原样返回
        return cur
    return cur


def _looks_like_m3u8(u):
    base = u.lower().split("?")[0]
    return base.endswith(".m3u8") or ".m3u8?" in u.lower() or ".m3u8&" in u.lower()


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_devtools(port, log, tries=40):
    url = f"http://127.0.0.1:{port}/json"
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                tabs = json.loads(r.read().decode("utf-8", "ignore"))
            for t in tabs:
                if t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.25)
    log("⚠️ 浏览器调试端口未就绪。")
    return None


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else \
        "https://www.miguvideo.com/p/detail/947470467"
    res = sniff_m3u8(target, on_log=lambda m: print(m))
    if res:
        print("M3U8:", res.m3u8_url)
        print("HDRS:", res.ytdlp_headers())
    else:
        print("NONE")
