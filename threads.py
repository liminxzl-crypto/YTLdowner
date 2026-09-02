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

# 浏览器嗅探结果缓存：页面 URL -> SniffResult（解析阶段产出，下载阶段复用，避免重复开浏览器）
M3U8_CACHE = {}


def _proxy_from_args(cookie_args):
    if "--proxy" in cookie_args:
        i = cookie_args.index("--proxy")
        if i + 1 < len(cookie_args):
            return cookie_args[i+1]
    return None

# ==========================================
# 🌟 极速自适应 Cookie 智能轮询引擎
# ==========================================
def generate_cookie_queue(preferred_args):
    """核心算法：精准识别代理，无条件优先用户设置，无缝生成备用队列"""
    # 1. 🌟 修复关键：无条件将用户的首选方案放在第一位！绝对尊重你的设置！
    queue = [preferred_args]
    
    # 2. 🌟 提取代理配置（极其关键：保证切换备用方案时依然能翻墙，防止卡死）
    base_args = []
    if "--proxy" in preferred_args:
        idx = preferred_args.index("--proxy")
        if idx + 1 < len(preferred_args):
            base_args = ["--proxy", preferred_args[idx+1]]
            
    # 3. 准备备用武器库 (带着代理一起切换)
    fallbacks = [
        base_args + ["--cookies-from-browser", "chrome"],
        base_args + ["--cookies-from-browser", "edge"],
        base_args + ["--cookies-from-browser", "safari"],
        base_args  # 终极兜底：无 Cookie
    ]
    
    # 4. 去重并填装弹药
    for f in fallbacks:
        if f not in queue:
            queue.append(f)
            
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


def looks_like_playlist(url):
    """粗判 URL 是否是“系列/播放列表/频道合集”（需要让用户选择整集或单集）。"""
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
        yt_exe = get_exe_path(get_bin_name("yt-dlp"))
        if not os.path.exists(yt_exe):
            self.log_signal.emit(f"❌ 找不到引擎: {yt_exe}")
            self.finished_signal.emit({"url": self.url, "title": "获取失败", "thumb": ""})
            return

        cookie_queue = generate_cookie_queue(self.cookie_args)
        
        for current_cookie in cookie_queue:
            strategy = get_strategy_name(current_cookie)
            self.log_signal.emit(f"🔍 正在解析: {self.url} [尝试策略: {strategy}]")

            cmd = [
                yt_exe,
                "--dump-json",
                "--no-playlist",
                # 解析阶段：较短超时，快速失败切换策略
                "--socket-timeout", "10",
                "--retries", "1",
                "--fragment-retries", "3",
                "--no-colors",
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
                                
                    self.log_signal.emit(f"✅ 解析成功: {title}")
                    self.finished_signal.emit({"url": self.url, "title": title, "thumb": thumb_path})
                    return # 🎉 首选方案成功，直接退出！绝不拖泥带水！
                else:
                    self.log_signal.emit(f"⚠️ [{strategy}] 授权被拒或网络超时，0.1秒内自动切换...")
                    
            except Exception as e:
                self.log_signal.emit(f"⚠️ 运行异常: {str(e)}，自动切换...")

        # 常规 yt-dlp 解析全部失败 -> 浏览器嗅探真实 m3u8（带鉴权参数）
        title = "解析失败 (需导入TXT)"
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

    def run(self):
        from config import ensure_binaries_ready
        ensure_binaries_ready()
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

        # 第一阶段：常规 yt-dlp（含 Cookie 智能轮询）
        ok, path, err = self._ytdlp_download(yt_exe, ffmpeg_exe, self.url,
                                             self.cookie_args, [])
        if ok:
            self._finish_ok(path)
            return
        if err == "paused":
            self._finish_paused()
            return

        # 第二阶段：常规方案全部失败 -> 浏览器嗅探真实 m3u8 直链后无缝重试
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
        cookie_queue = generate_cookie_queue(cookie_args) if not direct else [[]]
        success = False
        final_error = ""
        sys_encoding = locale.getpreferredencoding()

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
            ] + get_ytdlp_js_args()

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
                    if "Retrying" in line or "giving up" in line:
                        self.output_signal.emit(f"🔄 重试: {line}")
                    else:
                        self.output_signal.emit(line)

                    dest_match = re.search(r'\[download\] Destination: (.+)', line)
                    if dest_match:
                        pc = dest_match.group(1).strip()
                        if not pc.endswith(('.part', '.ytdl', '.temp')):
                            self.final_downloaded_path = pc

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
            except Exception as e:
                if not self.is_paused:
                    self.output_signal.emit(f"⚠️ 发生错误: {str(e)}，自动重试...")
            finally:
                self.process = None

        return success, self.final_downloaded_path, final_error

    def _finish_ok(self, path):
        self.output_signal.emit(f"🎉 任务完美结束: {self.url}")
        self.progress_signal.emit(self.url, 100, "完成", "00:00")
        self.finished_signal.emit(self.url, True, path if path else "ok")

    def _finish_paused(self):
        self.output_signal.emit(f"⏸️ 任务已暂停: {self.url}")
        self.finished_signal.emit(self.url, False, "paused")

    def _finish_fail(self):
        self.output_signal.emit("="*40)
        self.output_signal.emit("❌ 下载失败：常规解析与浏览器嗅探均未能取得可用视频流。")
        self.output_signal.emit("💡 可尝试：开启代理、导入目标站 Cookie(TXT)，或确认该视频需要登录/付费。")
        self.output_signal.emit("="*40)
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
    def _github_download(self, repo, asset_name, dest_path, timeout=120):
        """从 GitHub 最新 release 下载指定 asset（支持代理），返回 True/False"""
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
            with open(dest_path, "wb") as f:
                f.write(resp.read())
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
