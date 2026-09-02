import sys
import os
import subprocess
import shutil

# ==========================================
# 全局目录架构
# ==========================================

USER_HOME = os.path.expanduser("~")
APP_DATA_DIR = os.path.join(USER_HOME, ".ytldowner")
DEFAULT_TEMP_DIR = os.path.join(APP_DATA_DIR, "temp")
THUMB_DIR = os.path.join(APP_DATA_DIR, ".thumbs")
QUEUE_FILE = os.path.join(APP_DATA_DIR, "download_queue.json")
CONFIG_FILE = os.path.join(APP_DATA_DIR, "config.json")

BIN_DIR = os.path.join(APP_DATA_DIR, "bin")


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return None


for _d in (DEFAULT_TEMP_DIR, THUMB_DIR, BIN_DIR):
    _ensure_dir(_d)

# PyInstaller onefile 二进制（yt-dlp_macos）解包所需的运行时临时目录。
# 必须保证可写，否则会报 "Could not create temporary directory!"。
# 应用目录不可写时回退到系统临时目录。
def _pick_runtime_temp_dir():
    # 优先应用目录；失败则依次尝试常见系统临时目录。
    candidates = [
        os.path.join(APP_DATA_DIR, "runtime"),
        os.path.expanduser("~/Library/Caches/ytldowner/runtime"),
        "/tmp/ytldowner-runtime",
        "/private/tmp/ytldowner-runtime",
        "/var/tmp/ytldowner-runtime",
    ]
    for c in candidates:
        if _ensure_dir(c):
            return c
    return APP_DATA_DIR

RUNTIME_TEMP_DIR = _pick_runtime_temp_dir()

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ==========================================
# 全局代理配置
# ==========================================
_PROXY_ENABLED = False
_PROXY_ADDRESS = ""

def set_proxy_config(enabled, address):
    global _PROXY_ENABLED, _PROXY_ADDRESS
    _PROXY_ENABLED = bool(enabled)
    _PROXY_ADDRESS = address or ""

def _get_proxy_opener():
    import urllib.request
    if _PROXY_ENABLED and _PROXY_ADDRESS:
        handler = urllib.request.ProxyHandler({
            "http": _PROXY_ADDRESS,
            "https": _PROXY_ADDRESS,
        })
        return urllib.request.build_opener(handler)
    return None


# ==========================================
# 跨平台与底层引擎执行辅助
# ==========================================
# 需要在 macOS 上"弹射"到 BIN_DIR 执行的二进制（避免 _MEIPASS 权限/隔离问题，
# 同时让升级后的版本优先生效）
_ESCAPE_TOOLS = ("yt-dlp", "ffmpeg", "ffprobe", "node")


def get_exe_path(relative_path):
    """跨平台资源路径：返回可执行的二进制绝对路径。

    - macOS 下 yt-dlp 在打包内伪装为 yt-dlp.dat。
    - 打包环境中，二进制会被拷贝到 ~/.ytldowner/bin 后执行（弹射机制）。
    - BIN_DIR 中已存在（升级后）的二进制优先。
    """
    source_filename = relative_path + ".dat" if (
        sys.platform == "darwin" and relative_path == "yt-dlp"
    ) else relative_path

    if hasattr(sys, "_MEIPASS"):
        original_path = os.path.join(sys._MEIPASS, source_filename)

        if sys.platform == "darwin" and relative_path in _ESCAPE_TOOLS:
            escape_path = os.path.join(BIN_DIR, relative_path)
            if os.path.exists(escape_path):
                if relative_path == "yt-dlp" and not _binary_healthy(escape_path):
                    try:
                        os.remove(escape_path)
                    except Exception:
                        pass
                else:
                    return escape_path
            try:
                if os.path.exists(original_path):
                    shutil.copyfile(original_path, escape_path)
                    os.chmod(escape_path, 0o755)
                    strip_quarantine(escape_path)
                    prepare_binary(escape_path)
                return escape_path
            except Exception:
                pass
        return os.path.join(sys._MEIPASS, relative_path)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    if sys.platform == "darwin" and relative_path in _ESCAPE_TOOLS:
        escape_path = os.path.join(BIN_DIR, relative_path)
        if os.path.exists(escape_path):
            if relative_path == "yt-dlp" and not _binary_healthy(escape_path):
                try:
                    os.remove(escape_path)
                except Exception:
                    pass
            else:
                return escape_path
        src_name = "yt-dlp.dat" if relative_path == "yt-dlp" else relative_path
        src_path = os.path.join(script_dir, src_name)
        if os.path.exists(src_path):
            try:
                shutil.copyfile(src_path, escape_path)
                os.chmod(escape_path, 0o755)
                strip_quarantine(escape_path)
                prepare_binary(escape_path)
                return escape_path
            except Exception:
                pass
        return escape_path

    return os.path.join(script_dir, relative_path)


def get_node_path():
    """返回可用的 node 可执行文件路径，找不到返回 None。"""
    candidates = []
    try:
        candidates.append(os.path.join(BIN_DIR, get_bin_name("node")))
    except Exception:
        pass
    try:
        candidates.append(get_exe_path("node"))
    except Exception:
        pass
    # 系统 PATH 中的 node
    sys_node = shutil.which("node")
    if sys_node:
        candidates.append(sys_node)
    for c in candidates:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def get_ytdlp_js_args():
    """返回供 yt-dlp 使用的 JS 运行时参数（用于 YouTube n-sig 挑战等）。

    新版 yt-dlp 需要一个 JS 运行时（node/deno/bun）才能解析 YouTube。
    项目自带 node，这里显式指定其路径，避免依赖 PATH。
    """
    node = get_node_path()
    if node:
        return ["--js-runtimes", f"node:{node}"]
    return []


def get_bin_name(base_name):
    return f"{base_name}.exe" if os.name == "nt" else base_name


def get_subprocess_kwargs():
    """返回子进程通用参数。

    - Windows: 隐藏控制台窗口。
    - stdin=DEVNULL: 避免 PyInstaller onefile 二进制（如 yt-dlp_macos）
      在继承管道 stdin 时挂起；这是版本查询卡顿的常见根因。
    """
    kwargs = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def get_clean_env():
    """净化子进程环境：移除代理与套娃污染，但保留系统其他变量。

    - 移除代理变量，避免 yt-dlp 走系统代理导致冲突。
    - 移除 PyInstaller/Python/DYLD 污染变量（App 自身是打包的，
      这些变量会泄漏给子进程）。
    - 把 BIN_DIR 放到 PATH 最前面，让 yt-dlp 能找到 node/ffmpeg。
    - 强制 TMPDIR 指向应用自有可写目录，避免 PyInstaller onefile
      二进制（yt-dlp_macos）报 "Could not create temporary directory!"。
    """
    env = os.environ.copy()

    # 移除代理
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "no_proxy", "NO_PROXY"):
        env.pop(k, None)

    # 移除 PyInstaller / Python / DYLD 污染
    for key in list(env.keys()):
        if (key.startswith("_MEI") or key.startswith("_PYI")
                or key.startswith("PYI") or key.startswith("DYLD_")
                or key.startswith("LD_") or key.startswith("PYTHON")):
            env.pop(key, None)

    # BIN_DIR 置顶到 PATH
    path_parts = [BIN_DIR]
    for d in env.get("PATH", "").split(os.pathsep):
        if d and d not in path_parts:
            path_parts.append(d)
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
              "/usr/sbin", "/sbin"):
        if d not in path_parts:
            path_parts.append(d)
    env["PATH"] = os.pathsep.join(path_parts)

    # 强制可写临时目录给 PyInstaller onefile 二进制使用
    env["TMPDIR"] = RUNTIME_TEMP_DIR
    env["TEMP"] = RUNTIME_TEMP_DIR
    env["TMP"] = RUNTIME_TEMP_DIR

    return env


# ==========================================
# 版本查询
# ==========================================
def _is_exe_usable(exe_path, timeout=8):
    """快速检查二进制是否可运行。使用应用自有 TMPDIR，防止 onefile 解包失败。"""
    if not exe_path or not os.path.exists(exe_path):
        return False
    try:
        env = get_clean_env()
        proc = subprocess.run(
            [exe_path, "--version"],
            capture_output=True, timeout=timeout,
            env=env, **get_subprocess_kwargs()
        )
        if proc.returncode != 0:
            return False
        out = (proc.stdout or b"") + (proc.stderr or b"")
        text = out.decode("utf-8", errors="ignore")
        # PyInstaller bootloader 的致命错误：临时目录无法创建
        if "Could not create temporary directory" in text:
            return False
        return bool(out.strip())
    except Exception:
        return False


def _binary_healthy(exe_path):
    """判断弹射到 BIN_DIR 的 yt-dlp 二进制是否可正常运行。

    用于检测升级失败/文件损坏导致的 "Could not create temporary
    directory!" 等问题；返回 False 时调用方应删除并从打包版本重新释放。
    """
    return _is_exe_usable(exe_path, timeout=15)


def codesign_adhoc(path):
    """macOS 下对二进制进行 ad-hoc 签名，绕过 Gatekeeper 隔离。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["codesign", "--force", "--sign", "-", path],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def strip_quarantine(path):
    """移除 macOS quarantine 隔离属性。

    通过浏览器/网络下载的二进制会带上 com.apple.quarantine，
    Gatekeeper 会直接 SIGKILL（exit 137）杀掉未公证的二进制。
    shutil.copy2 会把扩展属性一并复制，因此弹射后必须显式清除。
    """
    if sys.platform != "darwin":
        return
    try:
        import xattr
        for attr in ("com.apple.quarantine", "com.apple.provenance"):
            try:
                xattr.removexattr(path, attr)
            except Exception:
                pass
    except ImportError:
        pass
    # 兜底：使用 xattr 命令行工具
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", path],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(["xattr", "-d", "com.apple.provenance", path],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def prepare_binary(path):
    """弹射后统一处理二进制：清除隔离属性 + ad-hoc 签名。"""
    strip_quarantine(path)
    codesign_adhoc(path)


def ensure_binaries_ready():
    """启动时确保 ffmpeg/ffprobe/node/yt-dlp 全部弹射到 BIN_DIR。

    这样 yt-dlp 合流时能在同一目录找到 ffmpeg 与 ffprobe，
    JS 运行时 node 也就位。BIN_DIR 中已存在（升级后）的文件不覆盖。
    """
    # 标记文件：首次处理完成后创建，后续启动跳过 xattr/codesign 等耗时操作
    _marker = os.path.join(BIN_DIR, ".binaries_ready")
    _all_exist = all(
        os.path.exists(os.path.join(BIN_DIR, get_bin_name(t)))
        for t in ("ffmpeg", "ffprobe", "node", "yt-dlp")
    )
    _skip_processing = _all_exist and os.path.exists(_marker)

    for tool in ("ffmpeg", "ffprobe", "node", "yt-dlp"):
        try:
            if not _skip_processing:
                # 首次启动：清除隔离属性防止 Gatekeeper 拦截
                existing = os.path.join(BIN_DIR, get_bin_name(tool))
                if os.path.exists(existing):
                    strip_quarantine(existing)
            get_exe_path(get_bin_name(tool))
        except Exception:
            pass

    # 所有二进制就位后创建标记文件
    if not _skip_processing:
        try:
            with open(_marker, "w") as f:
                f.write("1")
        except Exception:
            pass


def get_tool_version(tool_name):
    """获取本地工具版本号。只执行一次 --version，避免双重卡顿。

    PyInstaller onefile 二进制（yt-dlp_macos）首次冷启动可能较慢，
    因此超时给到 12 秒；挂起或报错返回"获取失败"，由 UI 显示缓存值。
    """
    try:
        if tool_name == "yt-dlp":
            exe = get_exe_path(get_bin_name("yt-dlp"))
            if not exe or not os.path.exists(exe):
                return "未安装"
        elif tool_name == "node":
            exe = get_node_path()
            if not exe:
                return "未安装"
        else:
            exe = None
            for c in (os.path.join(BIN_DIR, tool_name),
                      os.path.join(BASE_DIR, tool_name)):
                if os.path.exists(c):
                    exe = c
                    break
            if not exe:
                return "未安装"

        try:
            result = subprocess.run(
                [exe, "--version"],
                capture_output=True, timeout=12,
                **get_subprocess_kwargs()
            )
        except subprocess.TimeoutExpired:
            return "查询超时"
        except Exception:
            return "获取失败"

        out = result.stdout.decode("utf-8", errors="ignore").strip()
        err = result.stderr.decode("utf-8", errors="ignore").strip()
        all_lines = (out + "\n" + err).split("\n")

        for line in all_lines:
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if line.startswith("[PYI-") or "semaphore" in low or "semctl" in low:
                continue
            if line.startswith("  configuration:") or line.startswith("  built with"):
                continue
            if line.startswith("lib") and "version" in low:
                continue
            if line.startswith("ffmpeg version") or line.startswith("ffprobe version"):
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
            return line.split()[0] if line.split() else line

        for line in all_lines:
            line = line.strip()
            if line:
                return line.split()[0] if line.split() else line
        return "未知"
    except Exception:
        return "获取失败"


def _get_version_cache_path():
    return os.path.join(APP_DATA_DIR, "version_cache.json")


def load_version_cache():
    import json
    cp = _get_version_cache_path()
    if os.path.exists(cp):
        try:
            with open(cp, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_version_cache(cache):
    import json
    cp = _get_version_cache_path()
    try:
        with open(cp, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def get_latest_version(tool_name):
    """从网络获取最新版本号（支持代理）。"""
    import urllib.request
    import json

    opener = _get_proxy_opener()

    def _open(req, timeout=12):
        return opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)

    try:
        if tool_name == "yt-dlp":
            req = urllib.request.Request(
                "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                headers={"User-Agent": "YTLdowner", "Accept": "application/json"},
            )
            with _open(req) as resp:
                data = json.loads(resp.read().decode())
            return data.get("tag_name", "").lstrip("v") or "获取失败"

        elif tool_name in ("ffmpeg", "ffprobe"):
            if sys.platform == "darwin":
                # macOS 从 evermeet.cx 获取 snapshot 版本
                return _fetch_evermeet_latest(tool_name)
            # Windows / Linux 用 yt-dlp/FFmpeg-Builds
            req = urllib.request.Request(
                "https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest",
                headers={"User-Agent": "YTLdowner", "Accept": "application/json"},
            )
            with _open(req) as resp:
                data = json.loads(resp.read().decode())
            return data.get("tag_name", "").replace("autobuild-", "") or "获取失败"

        elif tool_name == "node":
            req = urllib.request.Request(
                "https://nodejs.org/dist/index.json",
                headers={"User-Agent": "YTLdowner"},
            )
            with _open(req) as resp:
                releases = json.loads(resp.read().decode())
            return releases[0]["version"].lstrip("v") if releases else "获取失败"

        return "未知"
    except Exception:
        return "获取失败"


def _fetch_evermeet_latest(tool_name):
    """从 evermeet.cx 解析最新 snapshot 版本号（如 125881-g946272b79a3）。"""
    import re
    import urllib.request
    opener = _get_proxy_opener()
    req = urllib.request.Request(
        "https://evermeet.cx/ffmpeg/",
        headers={"User-Agent": "Mozilla/5.0 YTLdowner"},
    )
    with (opener.open(req, timeout=12) if opener else urllib.request.urlopen(req, timeout=12)) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    # 形如 ffmpeg-125881-g946272b79a3.zip 或 ffprobe-125881-g946272b79a3.zip
    m = re.search(rf'{tool_name}-(\d+-g[0-9a-f]+)\.zip', html)
    if m:
        return m.group(1)
    return "获取失败"
