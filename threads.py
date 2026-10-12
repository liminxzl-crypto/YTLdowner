import os
import json
import subprocess
import re
import locale
from PySide6.QtCore import QThread, Signal
import sys
import shutil
import platform
import stat
from config import (get_exe_path, get_bin_name, get_subprocess_kwargs, get_clean_env,
                    get_ytdlp_js_args, THUMB_DIR, BIN_DIR)
import m3u8_sniffer
import douyin_extractor
import wx_channel_helper

# 浏览器嗅探结果缓存：页面 URL -> SniffResult（解析阶段产出，下载阶段复用，避免重复开浏览器）
M3U8_CACHE = {}
# 抖音解析结果缓存：URL -> Dict
DOUYIN_CACHE = {}
# 微信视频号解析结果缓存：URL -> Dict
CHANNELS_CACHE = {}
# 任务解析全量元数据与最优策略缓存：URL -> Dict
TASK_CACHE = {}


def _proxy_from_args(cookie_args):
    if "--proxy" in cookie_args:
        i = cookie_args.index("--proxy")
        if i + 1 < len(cookie_args):
            return cookie_args[i+1]
    return None

# ==========================================
# 🌟 极速自适应 Cookie 智能调度引擎（精准定向，杜绝 5 级耗时遍历）
# ==========================================
def generate_cookie_queue(preferred_args, for_download=False):
    """
    极速自适应策略：精准定向，杜绝 5 级盲目耗时轮询。
    - 若用户已配置 Cookie（本地 TXT 或指定浏览器），优先且仅使用该策略。
    - 若用户未配置 Cookie（免 Cookie 模式）：
      1. 优先尝试 免 Cookie（带代理）；
      2. 解析阶段若失败，仅快速回退 1 次本机首选主浏览器（Mac: Chrome/Safari, Win: Chrome/Edge）；
      3. 下载阶段严格使用解析成功的策略，绝不重复回退！
    """
    base_args = []
    if "--proxy" in preferred_args:
        idx = preferred_args.index("--proxy")
        if idx + 1 < len(preferred_args):
            base_args = ["--proxy", preferred_args[idx+1]]

    # 1. 用户有明确配置（如导入了 txt 或指定了特定浏览器）
    has_custom_cookie = "--cookies" in preferred_args or "--cookies-from-browser" in preferred_args
    if has_custom_cookie:
        queue = [preferred_args]
        if not for_download:
            queue.append(base_args)  # 仅带上免 cookie 快速兜底
        return queue

    # 2. 用户选择“免 Cookie”
    queue = [base_args]
    if not for_download:
        # 仅智能追加 1 个本机最可能登录的浏览器，不追加全部 3 个！
        main_browser = "chrome"
        if sys.platform == "darwin":
            if os.path.exists("/Applications/Google Chrome.app"):
                main_browser = "chrome"
            elif os.path.exists("/Applications/Microsoft Edge.app"):
                main_browser = "edge"
            else:
                main_browser = "safari"
        queue.append(base_args + ["--cookies-from-browser", main_browser])

    return queue

def get_strategy_name(args):
    """将参数翻译成直观的策略名称"""
    if "--cookies-from-browser" in args: 
        idx = args.index("--cookies-from-browser")
        return f"浏览器身份 ({args[idx+1].capitalize()})"
    if "--cookies" in args: return "本地 TXT 导入文件"
    return "无 / 免 Cookie"


# ==========================================
# 🌟 画质 / 音频模式（由“设置”注入，作用于解析与下载）
# ==========================================
# resolution: best / 2160 / 1440 / 1080 / 720 / 480 / 360
# audio_only: False=下载视频(默认)，True=仅下载音频并转 mp3
def build_media_args(resolution="best", audio_only=False):
    """根据画质/音频偏好，生成 yt-dlp 媒体相关参数。"""
    args = []
    if audio_only:
        args += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        return args
    if resolution and str(resolution) not in ("best", "0", "", "None"):
        h = str(resolution)
        # 优先不超过目标高度的最佳 mp4 音视频；兜底退化为任意不超过该高度、再退回最佳
        fmt = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
               f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b")
        args += ["-f", fmt]
    return args


# ==========================================
# 🌟 YouTube 风控对抗：播放器客户端轮换
# ==========================================
def is_youtube_url(url):
    u = (url or "").lower()
    return ("youtube.com/" in u or "youtu.be/" in u
            or "youtube-nocookie.com/" in u)


def youtube_client_variants(url):
    """返回 YouTube 的播放器客户端轮换表（每个元素是一组额外的 yt-dlp 参数）。

    2026 起 YouTube 对“数据中心 / 代理出口 IP”大面积启用风控，典型症状：
      - 解析正常、下载跑几 MB 后 `HTTP Error 403: Forbidden`；
      - 或直接 `Sign in to confirm you're not a bot`；
      - 或 `Some formats have been skipped ... SABR-only streaming experiment`。
    换一个播放器客户端往往能拿到真正可下载的直链，因此按顺序轮换重试。
    """
    if not is_youtube_url(url):
        return [[]]
    return [
        [],                                                          # 默认客户端
        ["--extractor-args", "youtube:player_client=web_embedded"],
    ]


def _iter_log_markers(line, state):
    """在 yt-dlp 输出里识别风控特征，写入 state。"""
    low = line.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        state["botcheck"] = True
    if "http error 403" in low or "403: forbidden" in low:
        state["forbidden"] = True
    if "sabr-only streaming experiment" in low:
        state["sabr"] = True


def looks_like_playlist(url):
    u = (url or "").lower()
    if "list=" in u:                      # YouTube / 多数站点通用 playlist 参数
        return True
    pats = [
        r"youtube\.com/(playlist|channel|c/|@)",
        r"bilibili\.com/(video/bv[^/?#]*\?.*p=|list)",
        r"/playlist\b", r"/album\b", r"/course\b",
        r"\blist=", r"series", r"collection",
    ]
    for pat in pats:
        if re.search(pat, u):
            return True
    return False


# --- 播放列表展开线程：把“整集/单集”解析成具体视频 URL 列表 ---
class PlaylistExpandThread(QThread):
    log_signal = Signal(str)
    finished_signal = Signal(str, list, str)  # 原始 url, [ {url,title}... ], 错误信息

    def __init__(self, url, cookie_args=None, mode="single"):
        super().__init__()
        self.url = url
        self.cookie_args = cookie_args or []
        # single: 只要当前这一个视频；series: 整个播放列表
        self.mode = mode

    def run(self):
        from config import get_exe_path, get_bin_name, get_subprocess_kwargs, get_clean_env
        yt_exe = get_exe_path(get_bin_name("yt-dlp"))
        if not os.path.exists(yt_exe):
            self.finished_signal.emit(self.url, [], "找不到 yt-dlp 引擎")
            return

        flat = ["--flat-playlist", "--dump-json"]
        # single 模式：只要当前视频，不展开列表
        scope = ["--no-playlist"] if self.mode == "single" else []

        # 带上媒体偏好无意义，这里只用代理/cookie；轮询首选 cookie 即可，失败再无 cookie
        attempts = [self.cookie_args]
        base = []
        if "--proxy" in self.cookie_args:
            i = self.cookie_args.index("--proxy")
            if i + 1 < len(self.cookie_args):
                base = ["--proxy", self.cookie_args[i+1]]
        if base:
            attempts.append(base)

        last_err = ""
        for cur in attempts:
            cmd = [yt_exe] + flat + scope + [
                "--socket-timeout", "15", "--retries", "1",
                "--no-warnings", "--no-colors",
            ] + get_ytdlp_js_args() + cur + [self.url]
            try:
                proc = subprocess.run(cmd, capture_output=True, env=get_clean_env(),
                                      **get_subprocess_kwargs())
            except Exception as e:
                last_err = str(e)
                continue

            out = proc.stdout or b""
            if isinstance(out, bytes):
                out = out.decode(locale.getpreferredencoding(), errors="ignore")
            items = []
            for line in out.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                vu = j.get("url") or j.get("webpage_url") or j.get("id")
                if not vu:
                    continue
                # flat-playlist 里 url 可能是相对 id，尽量补全
                if not str(vu).startswith("http") and j.get("webpage_url"):
                    vu = j["webpage_url"]
                items.append({"url": str(vu),
                              "title": j.get("title") or "系列视频"})
            if proc.returncode == 0 and items:
                self.finished_signal.emit(self.url, items, "")
                return
            err = proc.stderr or b""
            if isinstance(err, bytes):
                err = err.decode(locale.getpreferredencoding(), errors="ignore")
            last_err = (err or "未能解析出视频列表")[-300:]

        self.finished_signal.emit(self.url, [], last_err)

# --- 1. 信息获取线程 ---
class InfoThread(QThread):
    log_signal = Signal(str)
    finished_signal = Signal(dict)

    def __init__(self, url, cookie_args=None, media_args=None):
        super().__init__()
        self.url = url
        self.cookie_args = cookie_args or []
        self.media_args = media_args or []

    def run(self):
        proxy = _proxy_from_args(self.cookie_args)

        # 🌟 1. 抖音专属极速解析引擎
        if douyin_extractor.is_douyin_url(self.url):
            self.log_signal.emit(f"🎵 检测到抖音内容链接，启动专属无水印原画解析引擎: {self.url}")
            try:
                extractor = douyin_extractor.DouyinExtractor(proxy=proxy)
                info = extractor.parse(self.url)
                if info:
                    # 缓存解析结果
                    DOUYIN_CACHE[self.url] = info
                    DOUYIN_CACHE[info["url"]] = info
                    if info.get("id"):
                        DOUYIN_CACHE[info["id"]] = info

                    title = info.get("title", "抖音作品")
                    thumb_url = info.get("thumb", "")
                    thumb_path = ""
                    if thumb_url:
                        import hashlib
                        import urllib.request
                        md5_name = hashlib.md5(self.url.encode()).hexdigest() + ".jpg"
                        thumb_path = os.path.join(THUMB_DIR, md5_name)
                        if not os.path.exists(thumb_path):
                            try:
                                req = urllib.request.Request(thumb_url, headers={'User-Agent': 'Mozilla/5.0'})
                                with urllib.request.urlopen(req, timeout=5) as res, open(thumb_path, 'wb') as f:
                                    f.write(res.read())
                            except Exception:
                                pass

                    m_type_str = "【图集】" if info.get("media_type") == "image_album" else "【高清视频】"
                    self.log_signal.emit(f"✅ 抖音解析成功 {m_type_str}: {title}")
                    self.finished_signal.emit({"url": self.url, "title": f"{m_type_str} {title}", "thumb": thumb_path})
                    return
                else:
                    self.log_signal.emit("⚠️ 抖音专属解析未能获取详情，尝试回退常规引擎...")
            except Exception as e:
                self.log_signal.emit(f"⚠️ 抖音专属解析异常: {e}，尝试回退常规引擎...")

        # 🌟 2. 微信视频号专属解析 (支持 channels.weixin.qq.com 网页预览链接与嗅探直链)
        if wx_channel_helper.is_wechat_channels_url(self.url):
            self.log_signal.emit(f"🎬 检测到微信视频号内容链接，启动视频号专用提取器: {self.url}")
            cached_ch = CHANNELS_CACHE.get(self.url)

            # 短链 https://weixin.qq.com/sph/XXXX -> 真实预览页地址
            page_url = self.url
            if wx_channel_helper.is_wechat_channels_page_url(self.url):
                resolved = wx_channel_helper.resolve_channels_short_url(self.url)
                if resolved and resolved != self.url:
                    page_url = resolved
                    self.log_signal.emit(f"🔗 视频号短链已解析 -> {page_url}")

            if not cached_ch:
                cached_ch = CHANNELS_CACHE.get(page_url)

            if not cached_ch and wx_channel_helper.is_wechat_channels_page_url(page_url):
                try:
                    cached_ch = wx_channel_helper.extract_channels_web_video(
                        page_url,
                        on_log=lambda m: self.log_signal.emit(m),
                        timeout=12
                    )
                except Exception as e:
                    self.log_signal.emit(f"⚠️ 视频号网页解析异常: {e}")

            if cached_ch:
                CHANNELS_CACHE[self.url] = cached_ch
                if page_url:
                    CHANNELS_CACHE[page_url] = cached_ch
                if cached_ch.get("video_url"):
                    CHANNELS_CACHE[cached_ch["video_url"]] = cached_ch

            if cached_ch and cached_ch.get("video_url"):
                title = cached_ch.get("title", "微信视频号视频")
                thumb_url = cached_ch.get("thumb", "")
                thumb_path = ""
                if thumb_url:
                    import hashlib
                    import urllib.request
                    md5_name = hashlib.md5(self.url.encode()).hexdigest() + ".jpg"
                    thumb_path = os.path.join(THUMB_DIR, md5_name)
                    if not os.path.exists(thumb_path):
                        try:
                            req = urllib.request.Request(thumb_url, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=5) as res, open(thumb_path, 'wb') as f:
                                f.write(res.read())
                        except Exception:
                            pass
                TASK_CACHE[self.url] = {
                    "title": title,
                    "thumb": thumb_path,
                    "cookie_args": [],
                    "info": cached_ch
                }
                self.log_signal.emit(f"✅ 视频号解析成功: {title}")
                self.finished_signal.emit({"url": self.url, "title": f"【视频号】{title}", "thumb": thumb_path})
                return

            # 走到这里说明：拿到了预览页元数据（或预览页根本没放流）。
            # 视频号的网页预览版只给封面+文案，真正的视频流必须在微信内播放才能被捕获，
            # 因此不要再回退到 yt-dlp 通用解析 + m3u8 嗅探（必然失败且要等 1 分钟）。
            if wx_channel_helper.is_wechat_channels_page_url(page_url):
                title = (cached_ch or {}).get("title") or "微信视频号视频"
                thumb_url = (cached_ch or {}).get("thumb", "")
                thumb_path = ""
                if thumb_url:
                    import hashlib
                    import urllib.request
                    md5_name = hashlib.md5(self.url.encode()).hexdigest() + ".jpg"
                    thumb_path = os.path.join(THUMB_DIR, md5_name)
                    if not os.path.exists(thumb_path):
                        try:
                            req = urllib.request.Request(thumb_url, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=5) as res, open(thumb_path, 'wb') as f:
                                f.write(res.read())
                        except Exception:
                            pass
                self.log_signal.emit("ℹ️ 该链接是视频号的网页预览版，浏览器内只加载封面，")
                self.log_signal.emit("   视频流被限制为“前往微信观看”，无法直接用直链下载。")
                self.log_signal.emit("💡 正确姿势：在 PC 微信里打开这条视频号内容播放一次，")
                self.log_signal.emit("   【抓取助手】会自动捕获真实流并自动入队。")
                self.finished_signal.emit({
                    "url": self.url,
                    "title": f"【视频号·需微信内播放】{title}",
                    "thumb": thumb_path
                })
                return

        yt_exe = get_exe_path(get_bin_name("yt-dlp"))
        if not os.path.exists(yt_exe):
            self.log_signal.emit(f"❌ 找不到引擎: {yt_exe}")
            self.finished_signal.emit({"url": self.url, "title": "获取失败", "thumb": ""})
            return

        cookie_queue = generate_cookie_queue(self.cookie_args, for_download=False)
        
        for current_cookie in cookie_queue:
            strategy = get_strategy_name(current_cookie)
            self.log_signal.emit(f"🔍 正在解析: {self.url} [尝试策略: {strategy}]")

            cmd = [
                yt_exe,
                "--dump-json",
                "--skip-download",
                "--no-playlist",
                "--no-check-certificates",
                # 解析阶段：超快超时，失败秒切
                "--socket-timeout", "4",
                "--retries", "0",
                "--fragment-retries", "1",
                "--no-colors",
                "--extractor-args", "youtube:player_client=android,web",
            ] + get_ytdlp_js_args() + current_cookie + [self.url]
            
            try:
                result = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    env=get_clean_env(), 
                    **get_subprocess_kwargs()
                )
                
                def safe_decode(b_data):
                    if not b_data: return ""
                    try:
                        return b_data.decode('utf-8')
                    except UnicodeDecodeError:
                        return b_data.decode(locale.getpreferredencoding(), errors='ignore')

                out_str = safe_decode(result.stdout)
                err_str = safe_decode(result.stderr)

                if result.returncode == 0:
                    info = json.loads(out_str)
                    title = info.get("title", "未知标题")
                    thumb_url = info.get("thumbnail", "")
                    
                    thumb_path = ""
                    if thumb_url:
                        import hashlib
                        import urllib.request
                        ext = thumb_url.split('?')[0].split('.')[-1]
                        if len(ext) > 4: ext = "jpg"
                        md5_name = hashlib.md5(self.url.encode()).hexdigest() + f".{ext}"
                        thumb_path = os.path.join(THUMB_DIR, md5_name)
                        if not os.path.exists(thumb_path):
                            try:
                                req = urllib.request.Request(thumb_url, headers={'User-Agent': 'Mozilla/5.0'})
                                with urllib.request.urlopen(req, timeout=5) as res, open(thumb_path, 'wb') as f:
                                    f.write(res.read())
                            except Exception as e:
                                pass # 封面失败不影响大局
                                
                    # 🌟 写入全量任务元数据与已验证成功策略缓存（供下载时 0 延迟秒开）
                    TASK_CACHE[self.url] = {
                        "title": title,
                        "thumb": thumb_path,
                        "cookie_args": current_cookie,
                        "info": info
                    }
                    self.log_signal.emit(f"✅ 解析成功: {title}")
                    self.finished_signal.emit({"url": self.url, "title": title, "thumb": thumb_path})
                    return # 🎉 首选方案成功，直接退出！绝不拖泥带水！
                else:
                    self.log_signal.emit(f"⚠️ [{strategy}] 响应超时或受限，快速切换备用策略...")
                    
            except Exception as e:
                self.log_signal.emit(f"⚠️ 运行异常: {str(e)}，自动切换...")

        # 常规 yt-dlp 解析全部失败 -> 浏览器嗅探真实 m3u8（带鉴权参数）
        title = "解析失败 (需导入TXT)"
        if is_youtube_url(self.url):
            # YouTube 走 HLS 嗅探没有任何意义，直接给出针对性结论，不浪费一分钟开浏览器
            self.log_signal.emit("❌ YouTube 解析被风控拦截（常见提示：Sign in to confirm you're not a bot）。")
            self.log_signal.emit("💡 说明当前出口 IP 被判定为机房/机器人 IP，请：")
            self.log_signal.emit("   1) 换一个原生/住宅 IP 的代理节点后重试；")
            self.log_signal.emit("   2) 在【设置】里选择【导入本地 txt...】或【浏览器身份】带上登录 Cookie。")
            self.finished_signal.emit({"url": self.url, "title": title, "thumb": ""})
            return
        if m3u8_sniffer.is_sniffable(self.url) and m3u8_sniffer.find_browser():
            self.log_signal.emit("🛰️ 常规解析被风控拦截，启动浏览器智能嗅探真实视频流...")
            proxy = _proxy_from_args(self.cookie_args)
            try:
                result = m3u8_sniffer.sniff_m3u8(
                    self.url, on_log=lambda m: self.log_signal.emit(m),
                    timeout=45, proxy=proxy)
            except Exception as e:
                result = None
                self.log_signal.emit(f"⚠️ 嗅探异常: {e}")
            if result and result.m3u8_url:
                M3U8_CACHE[self.url] = result
                t = (result.title or "").strip()
                if t:
                    title = t
                self.log_signal.emit(f"✅ 嗅探成功，已锁定真实视频流: {title}")
                self.finished_signal.emit({"url": self.url, "title": title, "thumb": ""})
                return

        # 🌟 所有弹药打光后的终极提示
        self.log_signal.emit("❌ 致命风控：所有 Cookie 授权方案均已失效！\n💡 请前往【设置】选择【导入本地 txt...】并提供最新导出的 Netscape 格式 cookie 文件！")
        self.finished_signal.emit({"url": self.url, "title": title, "thumb": ""})

# --- 2. 单个下载任务线程 ---
class SingleDownloadWorker(QThread):
    output_signal = Signal(str)
    progress_signal = Signal(str, int, str, str)
    finished_signal = Signal(str, bool, str)

    def __init__(self, url, final_dir, temp_dir, cookie_args=None, media_args=None):
        super().__init__()
        self.url = url
        self.final_dir = final_dir
        self.temp_dir = temp_dir
        self.cookie_args = cookie_args or []
        self.media_args = media_args or []
        self.is_paused = False
        self.process = None
        self.final_downloaded_path = "" 
        self.fail_hint = {}

    def run(self):
        from config import ensure_binaries_ready
        import time
        ensure_binaries_ready()
        proxy = _proxy_from_args(self.cookie_args)

        # 🌟 1. 抖音专属下载管道（支持无水印原画视频与图集多图+BGM）
        if douyin_extractor.is_douyin_url(self.url) or self.url in DOUYIN_CACHE:
            parsed = DOUYIN_CACHE.get(self.url)
            if not parsed:
                self.output_signal.emit("🔍 正在提取抖音无水印视频流地址...")
                try:
                    extractor = douyin_extractor.DouyinExtractor(proxy=proxy)
                    parsed = extractor.parse(self.url)
                except Exception as e:
                    self.output_signal.emit(f"⚠️ 抖音解析失败: {e}")

            if parsed:
                self.output_signal.emit(f"🚀 启动抖音专属无水印下载引擎: {parsed.get('title', '')}")
                audio_only = "-x" in self.media_args
                ok, path, err = douyin_extractor.download_douyin_media(
                    parsed_info=parsed,
                    save_dir=self.final_dir,
                    temp_dir=self.temp_dir,
                    on_progress=lambda pct, spd, d_b, t_b: self.progress_signal.emit(self.url, int(pct), spd, ""),
                    on_log=lambda m: self.output_signal.emit(m),
                    cancel_checker=lambda: self.is_paused,
                    audio_only=audio_only
                )
                if ok:
                    self._finish_ok(path)
                    return
                elif err == "paused":
                    self._finish_paused()
                    return
                else:
                    self.output_signal.emit(f"⚠️ 抖音专属下载失败 ({err})，尝试回退常规引擎...")

        # 🌟 2. 微信视频号专属下载管道
        if wx_channel_helper.is_wechat_channels_url(self.url) or self.url in CHANNELS_CACHE:
            cached_ch = CHANNELS_CACHE.get(self.url)
            if not cached_ch and wx_channel_helper.is_wechat_channels_page_url(self.url):
                # 短链先解析成真实预览页地址，再查缓存
                page_url = wx_channel_helper.resolve_channels_short_url(self.url)
                cached_ch = CHANNELS_CACHE.get(page_url)
                if not cached_ch and "channels.weixin.qq.com" in page_url:
                    cached_ch = wx_channel_helper.extract_channels_web_video(page_url)
                if cached_ch:
                    CHANNELS_CACHE[self.url] = cached_ch

            if cached_ch and cached_ch.get("video_url"):
                target_video_url = cached_ch.get("video_url") or self.url
                title = cached_ch.get("title", f"视频号_{int(time.time())}")
                safe_title = re.sub(r'[\/:*?"<>|]', '_', title)[:100]
                final_file = os.path.join(self.final_dir, f"{safe_title}.mp4")

                if cached_ch.get("decode_key"):
                    decode_key = cached_ch["decode_key"]
                    self.output_signal.emit(f"🎬 启动微信视频号下载与本地 Isaac64 解密引擎 (Key: {decode_key})...")
                    temp_file = os.path.join(self.temp_dir, f"wx_{int(time.time())}.tmp")
                    ok, err = douyin_extractor._stream_download_file(
                        target_video_url, temp_file,
                        on_progress=lambda pct, spd, d_b, t_b: self.progress_signal.emit(self.url, int(pct), spd, ""),
                        on_log=lambda m: self.output_signal.emit(m),
                        cancel_checker=lambda: self.is_paused
                    )
                    if ok and os.path.exists(temp_file):
                        self.output_signal.emit("🔐 正在进行微信视频号本地无损解密...")
                        dec_ok = wx_channel_helper.decrypt_wechat_video(
                            temp_file, final_file, decode_key,
                            progress_callback=lambda pct: self.progress_signal.emit(self.url, int(pct), "解密中...", "")
                        )
                        # 解密函数返回成功 ≠ 产物可用：密钥失效/算法不符时会写出坏文件，
                        # 必须校验 ftyp 头，否则会把无法播放的文件当成品交付。
                        if dec_ok and wx_channel_helper.is_valid_mp4(final_file):
                            try:
                                os.remove(temp_file)
                            except Exception:
                                pass
                            self._finish_ok(final_file)
                            return
                        self.output_signal.emit("⚠️ 视频号解密校验未通过（密钥可能已失效），已保留原始视频流。")
                        try:
                            if os.path.exists(final_file):
                                os.remove(final_file)
                        except Exception:
                            pass
                        if os.path.exists(temp_file):
                            shutil.move(temp_file, final_file)
                            self._finish_ok(final_file)
                            return
                    elif err == "paused":
                        self._finish_paused()
                        return
                else:
                    # 网页捕获的直链（带 token 的高清流）
                    self.output_signal.emit(f"🎬 启动微信视频号高清流式下载: {title}...")
                    temp_file = os.path.join(self.temp_dir, f"wx_{int(time.time())}.mp4")
                    ok, err = douyin_extractor._stream_download_file(
                        target_video_url, temp_file,
                        on_progress=lambda pct, spd, d_b, t_b: self.progress_signal.emit(self.url, int(pct), spd, ""),
                        on_log=lambda m: self.output_signal.emit(m),
                        cancel_checker=lambda: self.is_paused
                    )
                    if ok and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1024:
                        if os.path.exists(final_file):
                            os.remove(final_file)
                        os.rename(temp_file, final_file)
                        self._finish_ok(final_file)
                        return
                    elif err == "paused":
                        self._finish_paused()
                        return
                    else:
                        self.output_signal.emit(f"⚠️ 视频号直链下载失败 ({err})，尝试回退常规引擎...")
            elif wx_channel_helper.is_wechat_channels_page_url(self.url):
                # 预览页拿不到视频流：明确告知原因，不再浪费一分钟跑 yt-dlp + 嗅探
                title = (cached_ch or {}).get("title") or "微信视频号视频"
                self.output_signal.emit("=" * 40)
                self.output_signal.emit(f"❌ 视频号网页预览版无法直接下载：{title}")
                self.output_signal.emit("ℹ️ 该页面只加载封面图与文案，视频流被限制为“前往微信观看”。")
                self.output_signal.emit("💡 正确做法：在 PC 微信里打开并播放这条视频号内容，")
                self.output_signal.emit("   【抓取助手】会自动捕获真实直链并自动入队下载。")
                self.output_signal.emit("=" * 40)
                self.finished_signal.emit(self.url, False, "视频号需微信内抓取")
                return

        yt_exe = get_exe_path(get_bin_name("yt-dlp"))
        ffmpeg_exe = BIN_DIR

        if not os.path.exists(yt_exe):
            self.output_signal.emit(f"❌ 找不到引擎: {yt_exe}")
            self.finished_signal.emit(self.url, False, "引擎缺失")
            return

        proxy = _proxy_from_args(self.cookie_args)

        # 若解析阶段已嗅探到真实 m3u8 直链，直接无缝交接下载（跳过必败的常规尝试）
        cached = M3U8_CACHE.get(self.url)
        if cached and getattr(cached, "m3u8_url", None):
            self.output_signal.emit("🔗 使用已捕获的真实 m3u8 直链，直接交接下载引擎...")
            extra = cached.ytdlp_headers()
            ok, path, err = self._ytdlp_download(yt_exe, ffmpeg_exe, cached.m3u8_url,
                                                 [], extra, direct=True)
            if ok:
                self._finish_ok(path)
                return
            if err == "paused":
                self._finish_paused()
                return
            # 直链可能过期，丢弃缓存走常规+重新嗅探
            M3U8_CACHE.pop(self.url, None)
            self.output_signal.emit("⚠️ 缓存直链失效，回退到常规解析...")

        # 第一阶段：常规 yt-dlp（优先使用解析阶段已验证成功的策略，0 延迟直连）
        cached_task = TASK_CACHE.get(self.url)
        effective_cookie_args = cached_task.get("cookie_args") if (cached_task and "cookie_args" in cached_task) else self.cookie_args

        ok, path, err = self._ytdlp_download(yt_exe, ffmpeg_exe, self.url,
                                             effective_cookie_args, [])
        if ok:
            self._finish_ok(path)
            return
        if err == "paused":
            self._finish_paused()
            return

        # 第二阶段：常规方案全部失败 -> 浏览器嗅探真实 m3u8 直链后无缝重试
        # YouTube 不是 HLS 站，浏览器嗅探必然空手而归，直接给出针对性建议，省掉一分钟等待。
        if is_youtube_url(self.url):
            self._finish_fail("youtube")
            return
        if not m3u8_sniffer.is_sniffable(self.url):
            self._finish_fail()
            return
        if not m3u8_sniffer.find_browser():
            self.output_signal.emit("⚠️ 未找到可用浏览器，无法启用智能嗅探。")
            self._finish_fail()
            return

        self.output_signal.emit("🛰️ 常规解析被风控拦截，启动浏览器智能嗅探真实视频流...")
        result = m3u8_sniffer.sniff_m3u8(
            self.url, on_log=lambda m: self.output_signal.emit(m),
            timeout=50, proxy=proxy)

        if not result or not getattr(result, "m3u8_url", None):
            self.output_signal.emit("ℹ️ 浏览器嗅探未能发现可用的 m3u8 直链。")
            self._finish_fail()
            return

        M3U8_CACHE[self.url] = result
        self.output_signal.emit("🔗 已捕获带鉴权参数的真实 m3u8，无缝交接下载引擎...")
        extra = result.ytdlp_headers()
        ok, path, err = self._ytdlp_download(yt_exe, ffmpeg_exe, result.m3u8_url,
                                             [], extra, direct=True)
        if ok:
            self._finish_ok(path)
        elif err == "paused":
            self._finish_paused()
        else:
            self._finish_fail()

    def _ytdlp_download(self, yt_exe, ffmpeg_exe, target_url, cookie_args,
                        extra_args, direct=False):
        """运行一轮 yt-dlp 下载。返回 (success, final_path, err)。"""
        cookie_queue = generate_cookie_queue(cookie_args, for_download=True) if not direct else [[]]
        # YouTube 风控对抗：换播放器客户端往往能拿到可下载的直链
        client_variants = [[]] if direct else youtube_client_variants(target_url)
        success = False
        final_error = ""
        sys_encoding = locale.getpreferredencoding()
        seen_parts = set()
        hint = {}

        for vi, client_args in enumerate(client_variants):
            if success or self.is_paused:
                break
            if vi > 0:
                name = ""
                for i, a in enumerate(client_args):
                    if a == "--extractor-args":
                        name = client_args[i + 1]
                        break
                self.output_signal.emit(f"🎚️ 更换播放器客户端重试（绕过风控）: {name}")

            for current_cookie in cookie_queue:
                if self.is_paused:
                    break

                if not direct:
                    strategy = get_strategy_name(current_cookie)
                    self.output_signal.emit(
                        f"🔄 引擎正在启动，当前采用授权策略: 【{strategy}】...")
                else:
                    self.output_signal.emit("🔄 使用嗅探到的直链下载中...")

                self.final_downloaded_path = ""
                attempt_state = {"botcheck": False, "forbidden": False, "sabr": False}

                audio_only = "-x" in self.media_args
                cmd = [
                    yt_exe,
                    "--continue",
                ]
                if audio_only:
                    # 仅音频：让 yt-dlp 选最佳音频流，后处理转 mp3
                    cmd += ["-f", "bestaudio/best",
                            "-x", "--audio-format", "mp3", "--audio-quality", "0"]
                elif "-f" in self.media_args:
                    # 用户指定了分辨率上限
                    fi = self.media_args.index("-f")
                    cmd += ["-f", self.media_args[fi+1],
                            "--merge-output-format", "mp4"]
                else:
                    cmd += ["-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b",
                            "--merge-output-format", "mp4"]
                cmd += [
                    "-P", f"home:{self.final_dir}",
                    "-P", f"temp:{self.temp_dir}",
                    "-o", "%(title)s.%(ext)s",
                    "--ffmpeg-location", ffmpeg_exe,
                    "--newline",
                    "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "--no-colors",
                    "--retries", "infinite",
                    "--fragment-retries", "infinite",
                    "--file-access-retries", "10",
                    "--extractor-retries", "5",
                    "--retry-sleep", "3",
                    "--socket-timeout", "30",
                ] + get_ytdlp_js_args() + list(client_args)

                if "xinpianchang.com" in target_url:
                    cmd.extend(["--add-header", "Referer:https://www.xinpianchang.com/"])
                    cmd.extend(["--add-header", "Origin:https://www.xinpianchang.com"])

                cmd += list(extra_args)
                cmd += current_cookie + [target_url]

                try:
                    self.process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        env=get_clean_env(),
                        **get_subprocess_kwargs()
                    )

                    for raw_line in iter(self.process.stdout.readline, b''):
                        if self.is_paused:
                            break
                        try:
                            line = raw_line.decode('utf-8').strip()
                        except UnicodeDecodeError:
                            line = raw_line.decode(sys_encoding, errors='ignore').strip()
                        if not line:
                            continue
                        _iter_log_markers(line, attempt_state)
                        if "Retrying" in line or "giving up" in line:
                            self.output_signal.emit(f"🔄 重试: {line}")
                        else:
                            self.output_signal.emit(line)

                        dest_match = re.search(r'\[download\] Destination: (.+)', line)
                        if dest_match:
                            pc = dest_match.group(1).strip()
                            if pc.endswith(('.part', '.ytdl', '.temp')):
                                seen_parts.add(pc)
                            else:
                                self.final_downloaded_path = pc
                                seen_parts.add(pc + ".part")

                        m_merger = re.search(r'\[Merger\] Merging formats into "([^"]+)"', line)
                        m_move = re.search(r'\[MoveFiles\] Moving file "[^"]+" to "([^"]+)"', line)
                        m_already = re.search(r'\[download\] (.*?) has already been downloaded', line)
                        m_ffmpeg = re.search(r'\[ffmpeg\] Destination: (.+)', line)
                        m_remux = re.search(r'\[VideoRemuxer\] Remuxing video from .* to "([^"]+)"', line)
                        if m_merger: self.final_downloaded_path = m_merger.group(1)
                        elif m_move: self.final_downloaded_path = m_move.group(1)
                        elif m_already: self.final_downloaded_path = m_already.group(1).strip().strip('"')
                        elif m_ffmpeg: self.final_downloaded_path = m_ffmpeg.group(1).strip().strip('"')
                        elif m_remux: self.final_downloaded_path = m_remux.group(1)

                        prog_match = re.search(r'\[download\]\s+([\d\.]+)%', line)
                        if prog_match:
                            pv = int(float(prog_match.group(1)))
                            speed, eta = "计算中...", "计算中..."
                            sm = re.search(r'at\s+([~\d\.]+[a-zA-Z]+/s)', line)
                            if sm: speed = sm.group(1)
                            em = re.search(r'ETA\s+([\d:]+)', line)
                            if em: eta = em.group(1)
                            elif "100%" in line: eta = "00:00"
                            self.progress_signal.emit(self.url, pv, speed, eta)
                        elif "[Merger]" in line or "Merging formats" in line:
                            self.progress_signal.emit(self.url, 99, "合成中...", "即将完成")

                    self.process.wait()
                    if self.is_paused:
                        final_error = "paused"
                        break
                    elif self.process.returncode == 0:
                        success = True
                        break
                    else:
                        if not direct:
                            self.output_signal.emit(f"⚠️ 【{strategy}】失败 (代码 {self.process.returncode})，自动切换下一方案...")
                        final_error = f"error_{self.process.returncode}"
                        if attempt_state["botcheck"]:
                            hint["botcheck"] = True
                        elif attempt_state["forbidden"]:
                            hint["forbidden"] = True
                        if attempt_state["sabr"]:
                            hint["sabr"] = True
                        if attempt_state["botcheck"] or attempt_state["forbidden"]:
                            # 直链已被风控作废：带着旧 .part 续传只会立刻再吃一次 403，
                            # 因此清掉失败分片，换下一个播放器客户端重新取直链。
                            self._drop_partial_files(seen_parts)
                            if len(client_variants) > 1:
                                break
                except Exception as e:
                    if not self.is_paused:
                        self.output_signal.emit(f"⚠️ 发生错误: {str(e)}，自动重试...")
                finally:
                    self.process = None

        self.fail_hint = hint
        return success, self.final_downloaded_path, final_error

    def _drop_partial_files(self, paths):
        """删除失败尝试残留的分片文件（仅限本次任务记录过的路径）。"""
        removed = []
        for p in paths:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
                    removed.append(os.path.basename(p))
            except Exception:
                pass
        if removed:
            self.output_signal.emit(
                f"🧹 已清理失效分片 {len(removed)} 个（{', '.join(removed[:3])}），将重新拉取直链。")

    def _finish_ok(self, path):
        self.output_signal.emit(f"🎉 任务完美结束: {self.url}")
        self.progress_signal.emit(self.url, 100, "完成", "00:00")
        self.finished_signal.emit(self.url, True, path if path else "ok")

    def _finish_paused(self):
        self.output_signal.emit(f"⏸️ 任务已暂停: {self.url}")
        self.finished_signal.emit(self.url, False, "paused")

    def _finish_fail(self, kind=None):
        self.output_signal.emit("=" * 40)
        hint = getattr(self, "fail_hint", {}) or {}
        if kind == "youtube" or is_youtube_url(self.url):
            self.output_signal.emit("❌ YouTube 下载失败：已轮换过默认 / web_embedded 两种播放器客户端，仍拿不到可用直链。")
            if hint.get("botcheck"):
                self.output_signal.emit("🚩 关键线索：YouTube 返回 “Sign in to confirm you're not a bot”，")
                self.output_signal.emit("   说明当前出口 IP 被判定为机器人/机房 IP。")
            elif hint.get("forbidden"):
                self.output_signal.emit("🚩 关键线索：直链刚下几 MB 就被 403 掐断，")
                self.output_signal.emit("   这是 YouTube 对机房/代理 IP 的限速+封禁策略。")
            elif hint.get("sabr"):
                self.output_signal.emit("🚩 关键线索：YouTube 开启了 SABR-only 实验，高清直链不再下发。")
            self.output_signal.emit("💡 按顺序试这几招（从便宜到彻底）：")
            self.output_signal.emit("   1) 换一个代理节点（尽量选“原生/住宅 IP”节点，不要用机房节点），再重试；")
            self.output_signal.emit("   2) 设置里改用【导入本地 TXT】或【浏览器身份】带上登录 Cookie；")
            self.output_signal.emit("   3) 把画质上限从 2160p 降到 1080p，4K/AV1 流更容易被限速；")
            self.output_signal.emit("   4) 在“工具箱”里升级 yt-dlp 到最新版（当前版本已超过 90 天）。")
        else:
            self.output_signal.emit("❌ 下载失败：常规解析与浏览器嗅探均未能取得可用视频流。")
            self.output_signal.emit("💡 可尝试：开启代理、导入目标站 Cookie(TXT)，或确认该视频需要登录/付费。")
        self.output_signal.emit("=" * 40)
        self.finished_signal.emit(self.url, False, "风控拦截")

    def pause(self):
        self.is_paused = True
        if self.process:
            try:
                self.process.terminate()
                self.process.kill()
            except Exception:
                pass

    def resume(self):
        self.is_paused = False
# ==========================================
# 🌟 升级线程
# ==========================================
class UpgradeWorker(QThread):
    log_signal = Signal(str)
    finished_signal = Signal(str, bool)  # tool_name, success

    def __init__(self, tool_name):
        super().__init__()
        self.tool_name = tool_name

    def run(self):
        from config import get_exe_path, get_bin_name, get_subprocess_kwargs, get_clean_env, BIN_DIR
        import shutil

        tool = self.tool_name
        self.log_signal.emit(f"🔧 开始升级 {tool} ...")

        try:
            if tool == "yt-dlp":
                self._upgrade_ytdlp()
            elif tool in ("ffmpeg", "ffprobe"):
                self._upgrade_ffmpeg(tool)
            elif tool == "node":
                self._upgrade_node()
            else:
                self.log_signal.emit(f"❌ 未知工具: {tool}")
                self.finished_signal.emit(tool, False)
        except Exception as e:
            self.log_signal.emit(f"❌ {tool} 升级失败: {str(e)}")
            self.finished_signal.emit(tool, False)

    # ----------------------------------------------------------------
    # 下载辅助：从 GitHub API 获取最新 release 的下载链接
    # ----------------------------------------------------------------
    def _github_download(self, repo, asset_name, dest_path, timeout=300):
        """从 GitHub 最新 release 下载指定 asset（支持代理），返回 True/False。

        采用分块流式写入：yt-dlp 的 macOS 通用二进制约 35MB，
        一次性 resp.read() 在网络较慢时既没有进度反馈也容易超时失败。
        """
        import urllib.request
        import json as _json
        from config import _get_proxy_opener

        opener = _get_proxy_opener()

        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        self.log_signal.emit(f"🔍 查询最新版本: {repo}")

        req = urllib.request.Request(api_url, headers={
            "User-Agent": "YTLdowner/1.0",
            "Accept": "application/json"
        })
        with (opener.open(req, timeout=15) if opener else urllib.request.urlopen(req, timeout=15)) as resp:
            release = _json.loads(resp.read().decode())

        download_url = None
        for asset in release.get("assets", []):
            if asset["name"] == asset_name:
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            self.log_signal.emit(f"❌ 未找到 asset: {asset_name}")
            return False

        self.log_signal.emit(f"⬇️ 正在下载 {asset_name} ...")
        dl_req = urllib.request.Request(download_url, headers={
            "User-Agent": "YTLdowner/1.0",
            "Accept": "application/octet-stream"
        })
        with (opener.open(dl_req, timeout=timeout) if opener else urllib.request.urlopen(dl_req, timeout=timeout)) as resp:
            total = 0
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            got = 0
            last_pct = -10
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = int(got * 100 / total)
                        if pct - last_pct >= 10:
                            last_pct = pct
                            self.log_signal.emit(
                                f"   … {pct}% ({got/1048576:.1f}/{total/1048576:.1f} MB)")
            self.log_signal.emit(f"✅ 下载完成 ({got/1048576:.1f} MB)")
        return True

    # ----------------------------------------------------------------
    # yt-dlp 升级
    # ----------------------------------------------------------------
    def _upgrade_ytdlp(self):
        import shutil
        import stat
        import tempfile
        from config import BASE_DIR, get_exe_path, get_bin_name, get_subprocess_kwargs, get_clean_env, BIN_DIR, strip_quarantine, prepare_binary

        # 自升级在 macOS 独立二进制上常因权限/更新通道而失败，
        # 直接下载官方最新二进制更稳定可靠。
        exe = get_exe_path(get_bin_name("yt-dlp"))
        self.log_signal.emit("⬇️ 准备下载官方最新 yt-dlp 二进制 ...")
        try:
            tmp_dir = tempfile.mkdtemp()

            # 根据平台选择正确的 asset 名
            if os.name == "nt":
                asset_name = "yt-dlp.exe"
                tmp_exe = os.path.join(tmp_dir, "yt-dlp.exe")
                dest_name = "yt-dlp.exe"
            elif sys.platform == "darwin":
                # macOS 通用二进制（同时支持 arm64 和 x86_64）
                asset_name = "yt-dlp_macos"
                tmp_exe = os.path.join(tmp_dir, "yt-dlp")
                dest_name = "yt-dlp"
            else:
                asset_name = "yt-dlp"
                tmp_exe = os.path.join(tmp_dir, "yt-dlp")
                dest_name = "yt-dlp"

            ok = self._github_download("yt-dlp/yt-dlp", asset_name, tmp_exe)
            if not ok:
                self.finished_signal.emit("yt-dlp", False)
                return

            # 设置执行权限（非 Windows）
            if os.name != "nt":
                os.chmod(tmp_exe, os.stat(tmp_exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            # 复制到 BIN_DIR
            dest = os.path.join(BIN_DIR, dest_name)
            shutil.copyfile(tmp_exe, dest)
            if os.name != "nt":
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            if sys.platform == "darwin":
                strip_quarantine(dest)
                prepare_binary(dest)
                self._sync_ytdlp_dat(dest)
            self.log_signal.emit(f"✅ yt-dlp 升级成功 (直接下载)")
            self.finished_signal.emit("yt-dlp", True)
        except Exception as e:
            self.log_signal.emit(f"❌ yt-dlp 升级失败: {e}")
            self.finished_signal.emit("yt-dlp", False)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _sync_ytdlp_dat(self, exe_path):
        """将 yt-dlp 二进制同步回 .dat 文件"""
        from config import BASE_DIR
        dat_path = os.path.join(BASE_DIR, "yt-dlp.dat")
        try:
            shutil.copyfile(exe_path, dat_path)
            os.chmod(dat_path, 0o755)
            if sys.platform == "darwin":
                strip_quarantine(dat_path)
            self.log_signal.emit(f"📦 已同步到: {dat_path}")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 同步 .dat 文件失败: {e}")

    # ----------------------------------------------------------------
    # ffmpeg / ffprobe 升级
    # ----------------------------------------------------------------
    def _upgrade_ffmpeg(self, tool):
        import shutil
        import stat
        import tempfile
        import zipfile
        import platform as _platform
        from config import BIN_DIR, codesign_adhoc, _get_proxy_opener

        if sys.platform == "darwin":
            self._upgrade_ffmpeg_macos(tool, tempfile, zipfile, shutil, stat, BIN_DIR, codesign_adhoc, _get_proxy_opener)
            return
        if sys.platform == "linux":
            plat, ext = "linux", "tar.xz"
        elif sys.platform == "win32":
            plat, ext = "windows", "zip"
        else:
            self.log_signal.emit(f"❌ 不支持的操作系统: {sys.platform}")
            self.finished_signal.emit(tool, False)
            return

        arch_map = {"arm64": "linuxarm64", "aarch64": "linuxarm64",
                    "x86_64": "linux64", "amd64": "linux64"}
        machine = _platform.machine().lower()
        if plat == "linux":
            arch = arch_map.get(machine, "linux64")
            asset_name = f"ffmpeg-master-latest-{arch}-gpl.tar.xz"
        else:
            win_arch = "winarm64" if machine in ("arm64","aarch64") else "win64"
            asset_name = f"ffmpeg-master-latest-{win_arch}-gpl.zip"

        tmp_dir = tempfile.mkdtemp()
        archive_path = os.path.join(tmp_dir, asset_name)
        try:
            ok = self._github_download("yt-dlp/FFmpeg-Builds", asset_name, archive_path, timeout=180)
            if not ok:
                self.finished_signal.emit(tool, False)
                return
            self.log_signal.emit("📦 解压中 ...")
            if ext == "zip":
                with zipfile.ZipFile(archive_path, "r") as zf:
                    zf.extractall(tmp_dir)
            else:
                import tarfile
                with tarfile.open(archive_path, "r:xz") as tf:
                    tf.extractall(tmp_dir)
            self._install_ffmpeg_binary(tool, tmp_dir, BIN_DIR, shutil, stat)
        except Exception as e:
            self.log_signal.emit(f"❌ {tool} 升级失败: {e}")
            self.finished_signal.emit(tool, False)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _upgrade_ffmpeg_macos(self, tool, tempfile, zipfile, shutil, stat, BIN_DIR, codesign_adhoc, get_opener):
        """macOS 从 evermeet.cx 下载最新 snapshot（Intel 二进制，可在 Apple Silicon 通过 Rosetta 运行）。"""
        import re as _re
        import urllib.request
        tmp_dir = tempfile.mkdtemp()
        try:
            self.log_signal.emit("🔍 查询 evermeet.cx 最新版本 ...")
            opener = get_opener()
            req = urllib.request.Request(
                "https://evermeet.cx/ffmpeg/",
                headers={"User-Agent": "Mozilla/5.0 YTLdowner"},
            )
            with (opener.open(req, timeout=15) if opener else urllib.request.urlopen(req, timeout=15)) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            m = _re.search(rf'{tool}-(\d+-g[0-9a-f]+)\.zip', html)
            if not m:
                self.log_signal.emit(f"❌ 无法在 evermeet.cx 找到 {tool} 下载链接")
                self.finished_signal.emit(tool, False)
                return
            ver = m.group(1)
            fname = f"{tool}-{ver}.zip"
            url = f"https://evermeet.cx/ffmpeg/{fname}"
            archive_path = os.path.join(tmp_dir, fname)
            self.log_signal.emit(f"⬇️ 正在下载 {fname} ...")
            dl_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 YTLdowner"})
            with (opener.open(dl_req, timeout=180) if opener else urllib.request.urlopen(dl_req, timeout=180)) as resp:
                with open(archive_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
            self.log_signal.emit("📦 解压中 ...")
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(tmp_dir)
            self._install_ffmpeg_binary(tool, tmp_dir, BIN_DIR, shutil, stat, codesign_adhoc)
        except Exception as e:
            self.log_signal.emit(f"❌ {tool} 升级失败: {e}")
            self.finished_signal.emit(tool, False)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _install_ffmpeg_binary(self, tool, search_dir, BIN_DIR, shutil, stat, codesign_fn=None):
        for root, dirs, files in os.walk(search_dir):
            for fn in files:
                if fn == tool or fn == tool + ".exe":
                    src = os.path.join(root, fn)
                    dest = os.path.join(BIN_DIR, tool)
                    shutil.copyfile(src, dest)
                    if os.name != "nt":
                        os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    if codesign_fn and sys.platform == "darwin":
                        codesign_fn(dest)
                    self.log_signal.emit(f"✅ {tool} 已更新到: {dest}")
                    self.finished_signal.emit(tool, True)
                    return
        self.log_signal.emit(f"❌ 在压缩包中未找到 {tool}")
        self.finished_signal.emit(tool, False)

    # ----------------------------------------------------------------
    # Node.js 升级
    # ----------------------------------------------------------------
    def _upgrade_node(self):
        import shutil
        import stat
        import tempfile
        import zipfile
        import tarfile
        import platform as _platform
        import urllib.request
        import json as _json
        from config import BIN_DIR

        machine = _platform.machine().lower()
        arch_map = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}
        arch = arch_map.get(machine, "x64")

        # 获取最新版本号
        self.log_signal.emit("🔍 正在查询 Node.js 最新版本 ...")
        try:
            from config import _get_proxy_opener
            opener = _get_proxy_opener()
            req = urllib.request.Request(
                "https://nodejs.org/dist/index.json",
                headers={"User-Agent": "YTLdowner/1.0"}
            )
            with (opener.open(req, timeout=15) if opener else urllib.request.urlopen(req, timeout=15)) as resp:
                releases = _json.loads(resp.read().decode())
            latest = releases[0]["version"]  # e.g. v22.14.0
        except Exception:
            latest = "latest"

        if sys.platform == "darwin":
            ext = "tar.gz"
            plat = "darwin"
        elif sys.platform == "win32":
            ext = "zip"
            plat = "win"
        else:
            ext = "tar.gz"
            plat = "linux"

        download_url = f"https://nodejs.org/dist/{latest}/node-{latest}-{plat}-{arch}.{ext}"
        self.log_signal.emit(f"⬇️ 正在下载 Node.js {latest} ...")

        tmp_dir = tempfile.mkdtemp()
        archive_path = os.path.join(tmp_dir, f"node.{ext}")

        try:
            from config import _get_proxy_opener
            opener = _get_proxy_opener()
            req = urllib.request.Request(download_url, headers={"User-Agent": "YTLdowner/1.0"})
            with (opener.open(req, timeout=180) if opener else urllib.request.urlopen(req, timeout=180)) as resp:
                with open(archive_path, "wb") as f:
                    f.write(resp.read())

            self.log_signal.emit("📦 解压中 ...")
            if ext == "zip":
                with zipfile.ZipFile(archive_path, "r") as zf:
                    zf.extractall(tmp_dir)
            else:
                with tarfile.open(archive_path, "r:gz") as tar:
                    tar.extractall(tmp_dir)

            for root, dirs, files in os.walk(tmp_dir):
                for fn in files:
                    if fn == "node" or fn == "node.exe":
                        src = os.path.join(root, fn)
                        dest = os.path.join(BIN_DIR, "node")
                        shutil.copyfile(src, dest)
                        if os.name != "nt":
                            os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        if sys.platform == "darwin":
                            from config import strip_quarantine, prepare_binary
                            strip_quarantine(dest)
                            prepare_binary(dest)
                        self.log_signal.emit(f"✅ Node.js 已更新到: {dest}")
                        self.finished_signal.emit("node", True)
                        return

            self.log_signal.emit("❌ 在压缩包中未找到 node 二进制")
            self.finished_signal.emit("node", False)
        except Exception as e:
            self.log_signal.emit(f"❌ Node.js 升级失败: {e}")
            self.finished_signal.emit("node", False)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
