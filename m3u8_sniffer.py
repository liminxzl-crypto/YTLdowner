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
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# ----------------------------------------------------------------------
# 需要「JS 环境校验」的站点
# 这类站点（腾讯云 WAF 的 ws_sec_page.js、或站点自研前端风控 fec_wrapper.js）
# 会先返回 403/406 + 一段挑战 JS，必须由浏览器执行后才放行。
# 纯 HTTP 请求（yt-dlp / urllib / curl）无论带什么 Cookie 都拿不到页面，
# 唯一可行路径是：用真实浏览器 + 复用用户浏览器里已通过校验的 Cookie。
# ----------------------------------------------------------------------
JS_CHALLENGE_HOSTS = (
    "xinpianchang.com",     # 新片场：腾讯云 WAF(ws_sec_page) + 自研 fec_wrapper 双层
)


def is_js_challenge_site(url):
    """判断 URL 是否属于「必须浏览器执行 JS 才能访问」的站点。"""
    if not url:
        return False
    low = url.lower()
    return any(h in low for h in JS_CHALLENGE_HOSTS)


_UA_CACHE = {}


def browser_user_agent(browser=None):
    """返回与已安装浏览器**真实版本**一致的 UA。

    WAF 的通行凭据通常与首次通过校验时的 UA 绑定：若用假 UA（如写死 Chrome/131）
    去带真实 Chrome 的 Cookie，会被判定环境不一致而重新挑战。所以这里读真实版本。
    """
    exe = browser or find_browser()
    if not exe:
        return UA
    if exe in _UA_CACHE:
        return _UA_CACHE[exe]

    version = ""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=8).stdout or ""
        # 形如 "Google Chrome 154.0.8037.99" / "Microsoft Edge 130.0.2849.68"
        for tok in out.replace("\n", " ").split():
            if tok[:1].isdigit() and "." in tok:
                version = tok
                break
    except Exception:
        version = ""

    if not version:
        _UA_CACHE[exe] = UA
        return UA

    major = version.split(".")[0]
    if "Edg" in os.path.basename(exe) or "Edge" in exe:
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0")
    else:
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    _UA_CACHE[exe] = ua
    return ua


# ----------------------------------------------------------------------
# 浏览器 Cookie 提取（复用 yt-dlp 自带的浏览器读取与解密能力）
# ----------------------------------------------------------------------
_COOKIE_JAR_CACHE = {}


def _read_netscape_jar(path, host_filter=None):
    """解析 Netscape 格式 cookie 文件，返回 CDP 用的 cookie 列表。"""
    cookies = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                f7 = line.rstrip("\n").split("\t")
                if len(f7) < 7:
                    continue
                domain, _, cpath, secure, expires, name, value = f7[:7]
                if host_filter and host_filter not in domain:
                    continue
                try:
                    expires = int(float(expires))
                except Exception:
                    expires = 0
                ck = {"name": name, "value": value, "path": cpath or "/",
                      "secure": str(secure).upper() == "TRUE"}
                # CDP 要求 domain 以点开头表示含子域；不用前导点会被当精确域处理
                ck["domain"] = domain
                if expires > 0:
                    ck["expires"] = expires
                cookies.append(ck)
    except Exception:
        return []
    return cookies


def extract_browser_cookies(browser="chrome", url=None, cookie_args=None, log=None):
    """取出目标域名下的浏览器 Cookie（含 WAF 通行凭据）。

    url: 目标页面 URL，用于过滤域名
    cookie_args: yt-dlp 风格参数列表（--cookies-from-browser X / --cookies PATH），
                 可直接用上层已有的策略参数
    返回 CDP cookie 列表（失败返回空列表）
    """
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    # 1) 解析出 cookie 来源
    browser_name, jar_path, proxy = None, None, None
    args = list(cookie_args or [])
    if browser:
        browser_name = browser
    if args:
        for i, a in enumerate(args):
            if a == "--cookies-from-browser" and i + 1 < len(args):
                browser_name = args[i + 1]
            elif a == "--cookies" and i + 1 < len(args):
                jar_path = args[i + 1]
            elif a == "--proxy" and i + 1 < len(args):
                proxy = args[i + 1]
    if not browser_name and not jar_path:
        browser_name = _default_browser_name()

    host_filter = None
    if url:
        try:
            from urllib.parse import urlparse
            host_filter = urlparse(url).hostname or None
            # 取主域，兼容 xxx.com 与 www.xxx.com
            if host_filter:
                parts = host_filter.split(".")
                if len(parts) > 2:
                    host_filter = ".".join(parts[-2:])
        except Exception:
            host_filter = None

    # 2) 直接读用户导入的 TXT
    if jar_path and os.path.exists(jar_path):
        return _read_netscape_jar(jar_path, host_filter)

    # 3) 用 yt-dlp 导出浏览器 cookie（它内部处理了 macOS Keychain 解密）
    cache_key = (browser_name, host_filter)
    if cache_key in _COOKIE_JAR_CACHE:
        return _COOKIE_JAR_CACHE[cache_key]

    try:
        from config import get_exe_path
        yt_exe = get_exe_path("yt-dlp")
    except Exception:
        yt_exe = None
    if not yt_exe:
        return []

    tmp_jar = os.path.join(tempfile.gettempdir(),
                           f"ytl_ck_{browser_name or 'x'}_{abs(hash(cache_key)) & 0xffffff}.txt")
    cmd = [yt_exe, "--cookies-from-browser", browser_name or "chrome",
           "--cookies", tmp_jar, "--simulate", "--no-warnings",
           "--socket-timeout", "3"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append("http://127.0.0.1:1/none")   # 只为导出 cookie，不真正联网
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception as e:
        _log(f"⚠️ 读取浏览器 Cookie 失败: {e}")
        return []

    cookies = _read_netscape_jar(tmp_jar, host_filter)
    if cookies:
        _log(f"🍪 已从 {browser_name} 取得 {len(cookies)} 条站点 Cookie（用于通过环境校验）")
    else:
        _log(f"ℹ️ 未能从 {browser_name} 取到该站点 Cookie，将以匿名身份访问。")
    _COOKIE_JAR_CACHE[cache_key] = cookies
    return cookies


def _default_browser_name():
    if sys.platform == "darwin":
        for app, name in (("/Applications/Google Chrome.app", "chrome"),
                          ("/Applications/Microsoft Edge.app", "edge"),
                          ("/Applications/Brave Browser.app", "brave")):
            if os.path.exists(app):
                return name
        return "safari"
    if sys.platform == "win32":
        return "chrome"
    return "chrome"


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
    def __init__(self, url, timeout=15, retries=4):
        # 有些 Chrome 版本返回 http:// 前缀，统一处理；localhost 可能解析到 ::1
        # 而 Chrome 只监听 127.0.0.1，导致 ConnectionReset，所以强制走 IPv4 字面量。
        for pre in ("ws://", "http://"):
            if url.startswith(pre):
                url = url[len(pre):]
                break
        hostport, path = url.split("/", 1)
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            host, port = hostport, "80"
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        hostport = f"{host}:{port}"

        last_err = None
        for attempt in range(max(1, retries)):
            try:
                self._handshake(host, int(port), hostport, path, timeout)
                return
            except Exception as e:      # 端口刚就绪时 Chrome 可能瞬断，重试即可
                last_err = e
                try:
                    self.sock.close()
                except Exception:
                    pass
                time.sleep(0.35 * (attempt + 1))
        raise last_err if last_err else OSError("WebSocket 握手失败")

    def _handshake(self, host, port, hostport, path, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("WebSocket 握手被对端关闭")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        if "101" not in first:
            raise OSError(f"WebSocket 握手被拒绝: {first[:80]}")
        self.sock.settimeout(1.0)
        # 关键：握手包之后可能已随同到达若干字节的 CDP 帧，必须保留，
        # 否则会丢掉最早的网络事件（正好包含我们最想要的媒体请求）。
        self._buf = rest

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
    def __init__(self, m3u8_url, page_url, headers=None, title="", user_agent=""):
        self.m3u8_url = m3u8_url
        self.page_url = page_url
        self.headers = headers or {}
        self.title = title or ""
        # 仅对启用 JS 环境校验的站点记录 UA，避免影响其他站点的既有行为
        self.user_agent = user_agent or ""

    def ytdlp_headers(self):
        """把浏览器请求媒体流用的防盗链头翻译成 yt-dlp 参数。
        默认只注入 Referer/Origin；若媒体请求自带 Cookie，或该站点需要
        与环境校验时一致的 UA，则一并带上，否则直链会被 CDN 拒绝。"""
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
        ck = h.get("Cookie") or h.get("cookie")
        if ck:
            out += ["--add-header", f"Cookie:{ck}"]
        if self.user_agent:
            out += ["--user-agent", self.user_agent]
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


def sniff_m3u8(url, on_log=None, timeout=45, proxy=None, settle_seconds=2.5,
               cookies=None, user_agent=None, cookie_args=None):
    """在 headless 浏览器里打开 url，拦截真实 .m3u8 / .mp4 地址。

    on_log: 可选回调 callable(str)，用于回传进度日志。
    cookies: 需要预注入浏览器的 cookie 列表（CDP 格式）。对启用 JS 环境校验的
             站点（如新片场），必须带上用户浏览器里已通过校验的凭据，否则会被
             反复挑战而拿不到任何视频流。
    user_agent: 指定 UA；默认自动对齐本机浏览器的真实版本。
    cookie_args: yt-dlp 风格 cookie 参数，用于自动提取浏览器 Cookie。
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
    low_url = url.lower()
    if user_agent:
        selected_ua = user_agent
    elif ("channels.weixin.qq.com" in low_url or "weixin.qq.com" in low_url
          or "sph" in low_url or "finder" in low_url):
        selected_ua = MOBILE_UA
    else:
        # 与真实浏览器版本对齐：UA 不一致会让 WAF 判定环境改变而重新挑战
        selected_ua = browser_user_agent(browser)

    # 需要 JS 环境校验的站点：自动从用户浏览器取通行 Cookie
    js_guarded = is_js_challenge_site(url)
    if cookies is None and js_guarded:
        log("🔐 该站点启用 JS 环境校验（反爬），正在取用浏览器中已通过的通行凭据...")
        cookies = extract_browser_cookies(url=url, cookie_args=cookie_args, log=log)
    inject_cookies = cookies or []
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
        f"--user-agent={selected_ua}",
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

        if inject_cookies:
            # 先落 Cookie 再导航，让首页请求就带上通行凭据，避免触发一轮挑战
            try:
                cmd("Network.setCookies", {"cookies": inject_cookies})
                time.sleep(0.8)
            except Exception as e:
                log(f"⚠️ Cookie 注入失败（继续尝试）: {e}")

        cmd("Page.navigate", {"url": url})

        found_url = None
        found_headers = None
        page_title = ""
        title_req_id = None
        seen = set()
        deadline = time.time() + timeout
        last_play = 0.0
        settled_at = None
        # 环境校验站点：整个过程**绝不注入任何 JS**（包括探测用的 evaluate）。
        # 实测：腾讯云 WAF 的挑战页一旦被 CDP 注入脚本/点击动作干扰，就会被判定
        # 环境异常而持续返回 403；而页面自身加载播放器后会自动发起视频请求，
        # 根本不需要我们做「点播放」这种激活动作。
        for frame in ws.frames():
            now = time.time()
            if now > deadline:
                break
            if frame is None:
                # 空闲时周期性注入“播放”动作，触发懒加载视频（校验站点除外）
                if not js_guarded and now - last_play > 2.0:
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

            # 标题回执（此时已捕获到流，挑战早已结束，取值是安全的）
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
            return SniffResult(found_url, url, found_headers, title=page_title,
                               user_agent=(selected_ua if js_guarded else ""))
        if js_guarded:
            log("ℹ️ 该站点启用了 JS 环境校验，浏览器未能取得通行凭据。")
            log("💡 请先用本机浏览器（Chrome/Edge）正常打开该视频页面并成功播放一次，"
                "让浏览器拿到通行 Cookie，再回到软件重试。")
            return None
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
    low = m3u8_url.lower()
    if ".mp4" in low or "stodownload" in low or "finder.video.qq.com" in low:
        return m3u8_url
    cur = m3u8_url
    for _ in range(hops):
        try:
            text, final = _http_get_text(cur, proxy=proxy)
        except Exception as e:
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


def _looks_like_media(u):
    low = u.lower()
    base = low.split("?")[0]
    if base.endswith((".m3u8", ".mp4", ".flv", ".ts", ".webm")):
        return True
    if ".m3u8?" in low or ".m3u8&" in low or ".mp4?" in low:
        return True
    if "finder.video.qq.com" in low or "stodownload" in low:
        return True
    return False

def _looks_like_m3u8(u):
    return _looks_like_media(u)


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
