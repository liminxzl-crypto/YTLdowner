# -*- coding: utf-8 -*-
"""
微信视频号（WeChat Channels）抓取助手与本地视频流解密引擎。

核心参考与整合自业界标杆项目：
- ltaoo/wx_channels_download
- nobiyou/wx_channel
- Evil0ctal/WeChat-Channels-Video-File-Decryption

支持：
1. Isaac64 PRNG 密钥流生成与 XOR 微信加密视频纯 Python 极速解密算法
2. 视频号抓取助手服务（Proxy Bridge）进程启停控制与健康检查
3. 实时拦截监听器：监听 PC 微信视频号播放捕获，自动解析视频标题、封面、直链与 decode_key
4. 视频号 URL 模式识别与元数据标准化
"""

import os
import sys
import re
import time
import json
import socket
import struct
import shutil
import sqlite3
import tempfile
import zipfile
import tarfile
import platform
import subprocess
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, List, Callable
from PySide6.QtCore import QThread, Signal

from config import BIN_DIR, APP_DATA_DIR, get_subprocess_kwargs, get_clean_env


# ======================================================================
# 1. 微信视频号 Isaac64 PRNG 与 XOR 解密算法引擎 (纯 Python 高效实现)
# ======================================================================

class Isaac64:
    """
    ISAAC-64: A fast, cryptographically secure PRNG designed by Bob Jenkins.
    微信视频号加密流解密标准 PRNG 算法实现。
    """
    MASK64 = 0xFFFFFFFFFFFFFFFF

    def __init__(self, seed_key: int):
        self.randcnt = 0
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.randrsl = [0] * 256
        self.mm = [0] * 256

        # 初始化随机种子
        self.randrsl[0] = seed_key & self.MASK64
        self._init_state()

    def _init_state(self):
        # 金色常数 0x9e3779b97f4a7c13 (Golden Ratio in 64-bit)
        GOLDEN = 0x9e3779b97f4a7c13
        a = b = c = d = e = f = g = h = GOLDEN

        def mix():
            nonlocal a, b, c, d, e, f, g, h
            a = (a - e) & self.MASK64; f ^= (h >> 9);  h = (h + a) & self.MASK64
            b = (b - f) & self.MASK64; g ^= (a << 9);  a = (a + b) & self.MASK64
            c = (c - g) & self.MASK64; h ^= (b >> 23); b = (b + c) & self.MASK64
            d = (d - h) & self.MASK64; a ^= (c << 15); c = (c + d) & self.MASK64
            e = (e - a) & self.MASK64; b ^= (d >> 14); d = (d + e) & self.MASK64
            f = (f - b) & self.MASK64; c ^= (e << 20); e = (e + f) & self.MASK64
            g = (g - c) & self.MASK64; d ^= (f >> 17); f = (f + g) & self.MASK64
            h = (h - d) & self.MASK64; e ^= (g << 14); g = (g + h) & self.MASK64

        for _ in range(4):
            mix()

        for i in range(0, 256, 8):
            a = (a + self.randrsl[i]) & self.MASK64
            b = (b + self.randrsl[i + 1]) & self.MASK64
            c = (c + self.randrsl[i + 2]) & self.MASK64
            d = (d + self.randrsl[i + 3]) & self.MASK64
            e = (e + self.randrsl[i + 4]) & self.MASK64
            f = (f + self.randrsl[i + 5]) & self.MASK64
            g = (g + self.randrsl[i + 6]) & self.MASK64
            h = (h + self.randrsl[i + 7]) & self.MASK64
            mix()
            self.mm[i] = a & self.MASK64;     self.mm[i + 1] = b & self.MASK64
            self.mm[i + 2] = c & self.MASK64; self.mm[i + 3] = d & self.MASK64
            self.mm[i + 4] = e & self.MASK64; self.mm[i + 5] = f & self.MASK64
            self.mm[i + 6] = g & self.MASK64; self.mm[i + 7] = h & self.MASK64

        for i in range(0, 256, 8):
            a = (a + self.mm[i]) & self.MASK64
            b = (b + self.mm[i + 1]) & self.MASK64
            c = (c + self.mm[i + 2]) & self.MASK64
            d = (d + self.mm[i + 3]) & self.MASK64
            e = (e + self.mm[i + 4]) & self.MASK64
            f = (f + self.mm[i + 5]) & self.MASK64
            g = (g + self.mm[i + 6]) & self.MASK64
            h = (h + self.mm[i + 7]) & self.MASK64
            mix()
            self.mm[i] = a & self.MASK64;     self.mm[i + 1] = b & self.MASK64
            self.mm[i + 2] = c & self.MASK64; self.mm[i + 3] = d & self.MASK64
            self.mm[i + 4] = e & self.MASK64; self.mm[i + 5] = f & self.MASK64
            self.mm[i + 6] = g & self.MASK64; self.mm[i + 7] = h & self.MASK64

        self.isaac64()
        self.randcnt = 256

    def isaac64(self):
        self.cc = (self.cc + 1) & self.MASK64
        self.bb = (self.bb + self.cc) & self.MASK64

        for i in range(256):
            x = self.mm[i]
            mod = i % 4
            if mod == 0:
                self.aa = ~(self.aa ^ (self.aa << 21)) & self.MASK64
            elif mod == 1:
                self.aa = (self.aa ^ (self.aa >> 5)) & self.MASK64
            elif mod == 2:
                self.aa = (self.aa ^ (self.aa << 12)) & self.MASK64
            elif mod == 3:
                self.aa = (self.aa ^ (self.aa >> 33)) & self.MASK64

            self.aa = (self.mm[(i + 128) % 256] + self.aa) & self.MASK64
            self.mm[i] = y = (self.mm[(x >> 3) % 256] + self.aa + self.bb) & self.MASK64
            self.randrsl[i] = self.bb = (self.mm[(y >> 11) % 256] + x) & self.MASK64

    def next_uint64(self) -> int:
        # 与标准 ISAAC64 的 rand64() 严格对齐：
        #     if (!randcnt--) { isaac64(); randcnt = RANDSIZ - 1; }
        #     return randrsl[randcnt];
        # 即「先自减、再取值」，首次取到的是 randrsl[255] 而非 randrsl[0]。
        # 旧实现写成 randrsl[256 - randcnt - 1]，整体错位一个元素，
        # 会让密钥流完全不对 —— 表现为解密成功但文件头损坏、无法播放。
        if self.randcnt == 0:
            self.isaac64()
            self.randcnt = 256
        self.randcnt -= 1
        return self.randrsl[self.randcnt]


def decrypt_wechat_video(
    encrypted_path: str,
    decrypted_path: str,
    decode_key: int | str,
    max_decrypt_bytes: int = 131072,  # 默认解密头部 128KB，0 表示全量解密
    progress_callback: Optional[Callable[[float], None]] = None
) -> bool:
    """
    解密微信视频号加密的 MP4 视频文件。
    
    参数：
        encrypted_path: 加密源视频路径
        decrypted_path: 解密后目标视频保存路径
        decode_key: 视频号解析得到的 decode_key (整数或字符串形式的数字)
        max_decrypt_bytes: 微信视频号默认只加密前 128KB (131072 字节)；若为 0 则解密整文件
    """
    if not os.path.exists(encrypted_path):
        return False

    try:
        if isinstance(decode_key, str):
            decode_key = int(decode_key.strip())
    except Exception:
        return False

    file_size = os.path.getsize(encrypted_path)
    if file_size == 0:
        return False

    isaac = Isaac64(decode_key)

    decrypt_len = min(file_size, max_decrypt_bytes) if max_decrypt_bytes > 0 else file_size

    try:
        with open(encrypted_path, "rb") as fin, open(decrypted_path, "wb") as fout:
            processed = 0
            while processed < decrypt_len:
                chunk_size = min(65536, decrypt_len - processed)
                chunk = fin.read(chunk_size)
                if not chunk:
                    break

                # 每次取 8 字节 (uint64) 进行 XOR 异或运算
                out_chunk = bytearray(chunk)
                for i in range(0, len(chunk), 8):
                    key_val = isaac.next_uint64()
                    # 必须用大端序写入密钥流：微信客户端的密钥流按 big-endian
                    # 逐 64 位展开。早先用小端会把每个 qword 的 8 个字节整体反转，
                    # 结果是「解密成功、但文件头损坏、播放器认不出 MP4」。
                    key_bytes = struct.pack(">Q", key_val)
                    for j in range(min(8, len(chunk) - i)):
                        out_chunk[i + j] ^= key_bytes[j]

                fout.write(out_chunk)
                processed += len(chunk)
                if progress_callback:
                    progress_callback(processed / file_size * 100.0)

            # 剩余未加密部分直接原样复制
            if processed < file_size:
                while True:
                    chunk = fin.read(1024 * 1024)
                    if not chunk:
                        break
                    fout.write(chunk)
                    processed += len(chunk)
                    if progress_callback:
                        progress_callback(processed / file_size * 100.0)

        return True
    except Exception:
        if os.path.exists(decrypted_path):
            os.remove(decrypted_path)
        return False


def is_valid_mp4(path: str) -> bool:
    """校验文件是否为合法 MP4（前 12 字节内应出现 ftyp box）。

    专用于捕捉最隐蔽的失败模式：解密函数「执行成功」但密钥/字节序有误，
    结果写出一个无法播放的坏文件。此时必须回退到原始流，而不是当成成品交付。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return b"ftyp" in head
    except Exception:
        return False


# ======================================================================
# 2. 视频号抓取内核（ltaoo/wx_channels_download）自举与生命周期管理
# ======================================================================
#
# 工作原理（与 mitmproxy 方案彻底不同，也是唯一"零手工配置"的可行路径）：
#   wx_video_download 自己在本机架一个透明代理，接管 PC 微信的视频号流量，
#   从响应里解析出真实视频直链与 decode_key，落库到 data.db 的 browse_history。
#   YTLdowner 只做两件事：
#     ① 负责把这个内核二进制按当前平台自动准备好、拉起、退出时清理干净；
#     ② 轮询 data.db 拿「直链 + decode_key」，交给本地下载与 Isaac64 解密管道。
#   用户不需要碰任何代理设置，也不需要在浏览器里点任何按钮。
# ----------------------------------------------------------------------

WX_TOOL_REPO = "ltaoo/wx_channels_download"
WX_TOOL_BASE = "wx_video_download"

# 内核工作目录：config.yaml / data.db 都放这里，不污染用户目录与项目目录
WX_WORKDIR = os.path.join(APP_DATA_DIR, "wx_capture", "wxd")
WX_TOOL_DB = os.path.join(WX_WORKDIR, "data.db")
WX_TOOL_CONFIG = os.path.join(WX_WORKDIR, "config.yaml")
WX_KERNEL_LOG = os.path.join(WX_WORKDIR, "kernel.log")

# 代理快照：内核会接管系统代理，退出时必须还原，做到"用户无感"
WX_PROXY_BACKUP = os.path.join(APP_DATA_DIR, "wx_capture", "proxy_snapshot.json")

# 内核监听端口（仅用于日志展示与健康检查；数据走 sqlite，不依赖端口）
WX_API_PORT = 2022
WX_PROXY_PORT = 2023


def _wx_binary_name() -> str:
    return WX_TOOL_BASE + (".exe" if os.name == "nt" else "")


def _wx_platform_asset() -> tuple:
    """返回当前平台在 GitHub Release 中的 asset 后缀与压缩格式。

    资产命名形如：
        wx_video_download_v260907_darwin_arm64.zip
        wx_video_download_v260907_windows_x86_64.zip
        wx_video_download_v260907_linux_x86_64.tar.gz
    """
    m = (platform.machine() or "").lower()
    arm = m in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return ("darwin_arm64" if arm else "darwin_x86_64"), "zip"
    if os.name == "nt":
        return ("windows_arm64" if arm else "windows_x86_64"), "zip"
    return ("linux_arm64" if arm else "linux_x86_64"), "tar.gz"


def _wx_binary_candidates() -> List[str]:
    """可能已经存在内核二进制的路径（应用目录 / 内核工作目录 / 项目目录 / PATH）。"""
    name = _wx_binary_name()
    cands = [
        os.path.join(BIN_DIR, name),
        os.path.join(WX_WORKDIR, name),
        os.path.join(APP_DATA_DIR, "wx_capture", name),
    ]
    try:
        from config import BASE_DIR
        cands += [os.path.join(BASE_DIR, name), os.path.join(BASE_DIR, WX_TOOL_BASE)]
    except Exception:
        pass
    try:
        which = shutil.which(WX_TOOL_BASE)
        if which:
            cands.append(which)
    except Exception:
        pass
    return cands


def _wx_http_open(url: str, timeout: int = 30):
    """带用户代理配置的 HTTP 打开（GitHub 在国内通常需要走代理）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "YTLdowner/1.0 (+wx-channels)",
        "Accept": "*/*",
    })
    try:
        from config import _get_proxy_opener
        opener = _get_proxy_opener()
    except Exception:
        opener = None
    if opener is not None:
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """检测本机端口是否已被监听。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def _pids_listening(port: int) -> List[int]:
    """返回监听指定端口的进程 PID（Unix）。"""
    if os.name == "nt":
        return []
    pids: List[int] = []
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=6
        ).stdout
        for tok in out.split():
            try:
                pids.append(int(tok))
            except ValueError:
                pass
    except Exception:
        pass
    return pids


def _kill_pid(pid: int, allow_elevated: bool = False) -> bool:
    """结束指定进程；必要时（macOS）用管理员权限兜底。

    内核有时是上一次以 root 启动的孤儿进程，普通用户杀不掉。
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8)
            return True
        except Exception:
            return False

    def _alive() -> bool:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    for sig in (15, 9):
        try:
            os.kill(pid, sig)
        except Exception:
            return True
        time.sleep(0.5)
        if not _alive():
            return True

    if allow_elevated and sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "kill -9 {pid}" with administrator privileges'],
                capture_output=True, timeout=40
            )
            time.sleep(0.4)
            return not _alive()
        except Exception:
            pass
    return not _alive()


def download_wx_kernel(dest_dir: Optional[str] = None, on_log=None, on_progress=None) -> Optional[str]:
    """从 GitHub Release 下载并解压当前平台的内核二进制，返回可执行文件路径。

    这是"所有环境、所有电脑都能用"的关键：无论 macOS(arm/x86)、Windows、Linux，
    首次点击「视频号助手」时自动拉取对应架构的内核，用户无需手工下载。
    """
    def log(m):
        if on_log:
            try:
                on_log(m)
            except Exception:
                pass

    dest_dir = dest_dir or BIN_DIR
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        log(f"❌ 无法创建目录 {dest_dir}: {e}")
        return None

    suffix, ext = _wx_platform_asset()
    log(f"🔍 检查视频号内核（目标平台：{suffix}）…")

    try:
        with _wx_http_open(
            f"https://api.github.com/repos/{WX_TOOL_REPO}/releases/latest", timeout=20
        ) as resp:
            release = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        log(f"❌ 无法访问 GitHub Release：{e}")
        return None

    tag = release.get("tag_name") or "?"
    asset = None
    for a in release.get("assets", []):
        name = a.get("name", "")
        if "safe" in name.lower():
            continue
        if name.startswith("wx_video_download_v") and name.endswith(f"_{suffix}.{ext}"):
            asset = a
            break
    if not asset:
        log(f"❌ Release {tag} 中没有适配 {suffix} 的资产")
        return None

    total = int(asset.get("size") or 0)
    log(f"⬇️ 正在下载视频号内核 {tag}（{total / 1048576:.1f} MB）…")

    tmpdir = tempfile.mkdtemp(prefix="ytl-wxkernel-")
    try:
        archive = os.path.join(tmpdir, asset["name"])
        with _wx_http_open(asset["browser_download_url"], timeout=300) as resp, open(archive, "wb") as f:
            got = 0
            last_pct = -10
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = int(got * 100 / total)
                    if pct - last_pct >= 10:
                        last_pct = pct
                        log(f"   … {pct}%  ({got / 1048576:.1f}/{total / 1048576:.1f} MB)")
                        if on_progress:
                            try:
                                on_progress(pct)
                            except Exception:
                                pass

        extract_dir = os.path.join(tmpdir, "x")
        os.makedirs(extract_dir, exist_ok=True)
        if ext == "zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(extract_dir)

        target_name = _wx_binary_name()
        found = None
        for root, _dirs, files in os.walk(extract_dir):
            for fn in files:
                if fn in (target_name, WX_TOOL_BASE):
                    found = os.path.join(root, fn)
                    break
            if found:
                break
        if not found:
            log("❌ 解压后未找到内核可执行文件")
            return None

        dest = os.path.join(dest_dir, target_name)
        shutil.copyfile(found, dest)
        os.chmod(dest, 0o755)
        try:
            from config import prepare_binary
            prepare_binary(dest)   # 清 quarantine + ad-hoc 签名，绕过 Gatekeeper
        except Exception:
            pass
        log(f"✅ 视频号内核已就绪：{dest}")
        return dest
    except Exception as e:
        log(f"❌ 下载/解压视频号内核失败：{e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_WX_CONFIG_TEMPLATE = """debug:
  error: false
  echolog: false

workdir: "__WORKDIR__"

download:
  filenameTemplate: "{{filename}}"
  dir: "%UserDownloads%"
  playDoneAudio: false
  maxRunning: 3
  resourceConcurrency: 100
  segmentConcurrency: 10
  connectionConcurrency: 0

api:
  protocol: "http"
  hostname: "127.0.0.1"
  port: __API_PORT__

mcp:
  enabled: false

scraper:
  retainedJobs: 20

db:
  type: "sqlite"
  filepath: "%CWD%/data.db"

proxy:
  enabled: true
  system: true
  hostname: "127.0.0.1"
  port: __PROXY_PORT__
  tun: false
  skipInstallRootCert: false
  upstreamProxy: ""

channels:
  disableLocationToHome: false
  download:
    defaultHighest: false
    frontend: false
    cover: false
    pauseWhenDownload: false

update:
  sources:
    - type: github
      priority: 1
      github_repo: __REPO__
      enabled: true
"""


def write_wx_config(workdir: Optional[str] = None) -> str:
    """生成内核配置文件。

    要点：上游代理留空（直连），不把用户的内网代理连进来；
    代理端口固定 2023，API 端口固定 2022，避免与用户其它工具冲突。
    """
    workdir = workdir or WX_WORKDIR
    os.makedirs(workdir, exist_ok=True)
    cfg_path = os.path.join(workdir, "config.yaml")
    if os.path.exists(cfg_path):
        return cfg_path
    content = (
        _WX_CONFIG_TEMPLATE
        .replace("__WORKDIR__", workdir)
        .replace("__API_PORT__", str(WX_API_PORT))
        .replace("__PROXY_PORT__", str(WX_PROXY_PORT))
        .replace("__REPO__", WX_TOOL_REPO)
    )
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(content)
    return cfg_path


# ----------------------------------------------------------------------
# 系统代理快照 / 还原（macOS）
#
# 内核要接管系统代理才能解析视频号流量，这是它的工作原理。用户的诉求是
# "不要让我管代理" —— 那就由程序全程托管：启动前记录，退出后原样还原，
# 用户在界面上看不到、也不需要考虑任何代理设置。
# ----------------------------------------------------------------------

def _mac_network_services() -> List[str]:
    try:
        out = subprocess.run(
            ["networksetup", "-listallnetworkservices"],
            capture_output=True, text=True, timeout=8
        ).stdout
    except Exception:
        return []
    services = []
    for line in out.splitlines()[1:]:      # 首行是说明文字
        line = line.strip()
        if not line or line.startswith("*"):   # * 表示该服务已停用
            continue
        services.append(line)
    return services


def _mac_get_proxy(svc: str) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for kind, getter in (
        ("web", "-getwebproxy"),
        ("secure", "-getsecurewebproxy"),
        ("socks", "-getsocksfirewallproxy"),
    ):
        cfg: Dict[str, str] = {}
        try:
            out = subprocess.run(
                ["networksetup", getter, svc],
                capture_output=True, text=True, timeout=8
            ).stdout
            for line in out.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    cfg[k.strip()] = v.strip()
        except Exception:
            pass
        result[kind] = cfg
    return result


def _mac_apply_proxy(svc: str, kind: str, cfg: Dict[str, str]) -> None:
    setters = {
        "web": ("-setwebproxy", "-setwebproxystate"),
        "secure": ("-setsecurewebproxy", "-setsecurewebproxystate"),
        "socks": ("-setsocksfirewallproxy", "-setsocksfirewallproxystate"),
    }
    set_proxy, set_state = setters[kind]
    enabled = (cfg.get("Enabled", "").strip().lower() == "yes")
    server = cfg.get("Server", "").strip()
    port = cfg.get("Port", "").strip()
    try:
        if enabled and server and port:
            subprocess.run(["networksetup", set_proxy, svc, server, port],
                           capture_output=True, timeout=8)
            subprocess.run(["networksetup", set_state, svc, "on"],
                           capture_output=True, timeout=8)
        else:
            subprocess.run(["networksetup", set_state, svc, "off"],
                           capture_output=True, timeout=8)
    except Exception:
        pass


def snapshot_system_proxy() -> bool:
    """记录当前系统代理配置，供退出时还原。非 macOS 直接返回 False。"""
    if sys.platform != "darwin":
        return False
    try:
        snap = {svc: _mac_get_proxy(svc) for svc in _mac_network_services()}
        os.makedirs(os.path.dirname(WX_PROXY_BACKUP), exist_ok=True)
        with open(WX_PROXY_BACKUP, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def restore_system_proxy() -> bool:
    """把系统代理还原成快照状态（内核退出后调用）。"""
    if sys.platform != "darwin":
        return False
    if not os.path.exists(WX_PROXY_BACKUP):
        return False
    try:
        with open(WX_PROXY_BACKUP, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        return False
    for svc, kinds in (snap or {}).items():
        for kind, cfg in (kinds or {}).items():
            if kind in ("web", "secure", "socks"):
                _mac_apply_proxy(svc, kind, cfg or {})
    try:
        os.remove(WX_PROXY_BACKUP)
    except Exception:
        pass
    return True


WECHAT_CHANNELS_PATTERNS = [
    re.compile(r"finder\.video\.qq\.com", re.IGNORECASE),
    re.compile(r"channels\.weixin\.qq\.com", re.IGNORECASE),
    re.compile(r"findermp\.weixin\.qq\.com", re.IGNORECASE),
    # 官方分享短链：https://weixin.qq.com/sph/XXXXXXXX
    # （旧规则写的是 sph\. ，要求 "sph" 后面跟点号，实际短链是 sph/ ，永远匹配不上）
    re.compile(r"weixin\.qq\.com/sph", re.IGNORECASE),
]

# 视频号真正的媒体直链特征（带 token 的视频流，可直接下载）
_CHANNELS_MEDIA_HINTS = ("finder.video.qq.com", "stodownload", "findermp.weixin.qq.com")


def is_wechat_channels_url(text: str) -> bool:
    """检测 URL 是否为微信视频号相关链接或媒体流"""
    if not text:
        return False
    t = text.lower()
    return any(p.search(t) for p in WECHAT_CHANNELS_PATTERNS)


def is_channels_media_url(text: str) -> bool:
    """是否为视频号可直接下载的媒体直链（含 token 的 mp4/m3u8）。"""
    if not text:
        return False
    t = text.lower()
    if any(k in t for k in _CHANNELS_MEDIA_HINTS):
        # 封面图也走 stodownload，务必排除（picformat 表示图片规格）
        if "picformat=" in t or "wxampicformat=" in t or "/finderhead/" in t:
            return False
        return True
    return t.split("?")[0].endswith((".mp4", ".m3u8", ".ts", ".flv"))


def is_wechat_channels_page_url(text: str) -> bool:
    """是否为视频号的“网页/预览页”链接。

    这类页面（weixin.qq.com/sph/xxx、channels.weixin.qq.com/finder-preview/...）
    在浏览器里只加载封面图与文字信息，真正的视频流被限制为“前往微信观看”，
    因此不要把它交给 yt-dlp 通用解析或 m3u8 嗅探，白费时间。
    """
    if not text or is_channels_media_url(text):
        return False
    t = text.lower()
    return bool(re.search(r"(weixin\.qq\.com/sph|channels\.weixin\.qq\.com|findermp\.weixin\.qq\.com)", t))


def resolve_channels_short_url(url: str, timeout: int = 10) -> str:
    """把视频号分享短链解析成真实预览页地址。

    https://weixin.qq.com/sph/ABF3Oqrt64
        -> https://channels.weixin.qq.com/finder-preview/pages/sph?id=ABF3Oqrt64
    """
    if "weixin.qq.com/sph" not in (url or "").lower():
        return url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                           "Mobile/15E148 Safari/604.1"),
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl() or url
    except Exception:
        return url


# ======================================================================
# 视频号内核服务管理器
# ======================================================================

class WeChatChannelsServiceManager:
    """微信视频号抓取内核的生命周期管理器（单例）。

    负责：二进制自举 → 启动内核 → 健康检查 → 停止并还原系统代理。
    """

    _instance = None

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.is_running = False
        self.binary_path = self._find_binary()
        self.log_fn: Optional[Callable[[str], None]] = None
        self._log_file = None

    @classmethod
    def get_instance(cls) -> "WeChatChannelsServiceManager":
        if cls._instance is None:
            cls._instance = WeChatChannelsServiceManager()
        return cls._instance

    # ---------------- 日志 ----------------
    def _log(self, msg: str) -> None:
        if self.log_fn:
            try:
                self.log_fn(msg)
            except Exception:
                pass

    # ---------------- 二进制 ----------------
    def _find_binary(self) -> Optional[str]:
        for c in _wx_binary_candidates():
            if c and os.path.exists(c) and os.access(c, os.X_OK):
                return c
        for c in _wx_binary_candidates():
            if c and os.path.exists(c):
                return c
        return None

    def ensure_binary(self, force: bool = False) -> tuple:
        """确保内核二进制可用；缺失时自动按平台下载。返回 (ok, path)。"""
        if not force:
            found = self._find_binary()
            if found:
                self.binary_path = found
                return True, found
        path = download_wx_kernel(on_log=self._log)
        if path:
            self.binary_path = path
            return True, path
        return False, ""

    # ---------------- 健康检查 ----------------
    def process_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def kernel_log_tail(self, lines: int = 25) -> str:
        try:
            with open(WX_KERNEL_LOG, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except Exception:
            return ""

    # ---------------- 启动 / 停止 ----------------
    def _clear_stale_kernel(self) -> None:
        """清理上一次运行残留的内核进程。

        场景：App 被强退、或用户曾在终端/其它工具里跑过内核，进程仍在监听
        2022/2023，导致本次启动「端口被占用」直接失败。这里主动回收。
        """
        mine = self.process.pid if self.process else None
        pids = set()
        for port in (WX_API_PORT, WX_PROXY_PORT):
            pids.update(_pids_listening(port))
        pids.discard(mine)
        if not pids:
            return
        self._log(f"🧹 检测到上次残留的视频号内核进程（{len(pids)} 个），正在清理…")
        for pid in sorted(pids):
            if _kill_pid(pid, allow_elevated=True):
                self._log(f"   已结束残留进程 PID {pid}")
            else:
                self._log(f"   ⚠️ 无法结束残留进程 PID {pid}，可能仍会端口冲突")

    def start_service(self) -> tuple:
        """启动视频号内核。返回 (ok, message)。"""
        if self.process_alive():
            self.is_running = True
            return True, "视频号助手已在运行中"

        ok, path = self.ensure_binary()
        if not ok:
            return False, "未找到视频号内核，且自动下载失败（请检查网络/代理后重试）"

        try:
            os.makedirs(WX_WORKDIR, exist_ok=True)
            cfg = write_wx_config(WX_WORKDIR)

            # 回收上轮残留进程，避免端口被占用导致内核起不来
            self._clear_stale_kernel()

            # 启动前快照系统代理，退出时还原
            snapshot_system_proxy()

            if self._log_file is None:
                self._log_file = open(WX_KERNEL_LOG, "ab")

            if sys.platform != "win32":
                try:
                    os.chmod(path, 0o755)
                except Exception:
                    pass

            kwargs = get_subprocess_kwargs()
            kwargs.pop("stdin", None)          # 内核需要 stdin 常开
            self.process = subprocess.Popen(
                [path, "-c", cfg],
                cwd=WX_WORKDIR,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                env=get_clean_env(),
                **kwargs
            )

            # 轮询等待内核就绪（API 端口监听成功），最多 12 秒
            ready = False
            for _ in range(24):
                if not self.process_alive():
                    break
                if _port_listening(WX_API_PORT):
                    ready = True
                    break
                time.sleep(0.5)

            if not self.process_alive():
                rc = self.process.returncode if self.process else "?"
                tail = self.kernel_log_tail()
                self._log("❌ 视频号内核启动失败，日志尾部：")
                for ln in tail.splitlines()[-10:]:
                    if ln.strip():
                        self._log(f"   {ln}")
                if "address already in use" in tail or "端口被占用" in tail:
                    self._log(f"💡 端口 {WX_API_PORT} 被占用，通常是上一次的内核未正常退出。")
                self.process = None
                return False, f"视频号内核启动失败（退出码 {rc}）"

            if not ready:
                self._log("ℹ️ 内核进程已启动，API 端口尚未就绪，继续在后台等待…")

            self.is_running = True
            return True, "视频号助手已启动，正在接管视频号流量（请用电脑微信播放视频号）"
        except Exception as e:
            return False, f"启动失败: {e}"

    def stop_service(self) -> None:
        """停止内核并还原系统代理。"""
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=4)
                except Exception:
                    self.process.kill()
            except Exception:
                pass
            self.process = None

        # 无论内核是否优雅退出，都强制还原代理，绝不把用户环境留在被接管状态
        try:
            restore_system_proxy()
        except Exception:
            pass

        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

        self.is_running = False


# ======================================================================
# 3. 内核数据读取器 + 后台捕获监听线程
# ======================================================================

class WxChannelsDBReader:
    """只读访问内核的 SQLite 库，把 browse_history 行规整成下载任务描述。

    为什么读库而不是走 HTTP/MCP：
      - 零端口依赖，不受防火墙/端口占用影响；
      - 内核自己维护 schema，字段稳定（title/url/cover_url/extra_data.decode_key）；
      - Python 标准库 sqlite3 即可，无需额外协议实现。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or WX_TOOL_DB

    def available(self) -> bool:
        return os.path.exists(self.db_path)

    def _connect(self):
        # WAL 模式下只读连接即可读到已提交数据（含 -wal 中未 checkpoint 的部分）；
        # 极端情况回退到普通连接（仍只做 SELECT）。
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0)
        except Exception:
            conn = sqlite3.connect(self.db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        if not self.available():
            return []
        rows = []
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, title, url, source_url, cover_url, type, external_id, "
                    "extra_data, visited_times, updated_at "
                    "FROM browse_history "
                    "WHERE deleted_at IS NULL "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (int(limit),)
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return [self._normalize(r) for r in rows]

    @staticmethod
    def _normalize(row) -> Dict[str, Any]:
        d = dict(row)
        extra: Dict[str, Any] = {}
        raw = d.get("extra_data")
        if raw:
            try:
                extra = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                extra = {}
        decode_key = extra.get("decode_key")
        return {
            "id": d.get("id"),
            "external_id": d.get("external_id"),
            "title": d.get("title") or "微信视频号视频",
            # url 用视频号页面地址（供队列展示与缓存键）；直链在 video_url
            "url": d.get("source_url") or d.get("url") or "",
            "source_url": d.get("source_url") or "",
            "video_url": d.get("url") or "",
            "thumb": d.get("cover_url") or "",
            "decode_key": str(decode_key) if decode_key else "",
            "media_type": "video",
            "visited_times": d.get("visited_times"),
            "updated_at": d.get("updated_at"),
        }


class ChannelsListenerThread(QThread):
    """轮询内核数据库，发现新捕获的视频号内容后发出信号。

    首次扫描会把既有历史记为基线（不重复入队），此后只响应"新播放"的内容。
    """
    video_captured = Signal(dict)   # {"url","video_url","title","thumb","decode_key",...}
    log_signal = Signal(str)

    def __init__(self, check_interval_ms: int = 1500, db_path: Optional[str] = None):
        super().__init__()
        self.check_interval = check_interval_ms / 1000.0
        self.running = True
        self.reader = WxChannelsDBReader(db_path)
        self._baseline_done = False
        self._seen: set = set()

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        waited = 0.0
        while self.running:
            try:
                if not self.reader.available():
                    waited += self.check_interval
                    if waited >= 12.0 and int(waited) % 6 < 2:
                        self.log_signal.emit("⏳ 等待视频号内核初始化数据库…")
                    time.sleep(self.check_interval)
                    continue

                items = self.reader.fetch_history(limit=30)

                if not self._baseline_done:
                    for it in items:
                        if it.get("id"):
                            self._seen.add(it["id"])
                    self._baseline_done = True
                    if items:
                        self.log_signal.emit(
                            f"ℹ️ 视频号助手已就绪，已忽略 {len(items)} 条历史记录，等待新播放的内容…")
                    else:
                        self.log_signal.emit("ℹ️ 视频号助手已就绪，请在电脑微信中播放视频号内容…")
                    time.sleep(self.check_interval)
                    continue

                # 从旧到新入队，保证按播放先后顺序处理
                for it in reversed(items):
                    key = it.get("id")
                    if not key or key in self._seen:
                        continue
                    self._seen.add(key)
                    if not it.get("video_url"):
                        continue
                    tag = "🔐 含解密密钥" if it.get("decode_key") else "🎬"
                    self.log_signal.emit(f"{tag} 捕获视频号内容：{it.get('title')}")
                    self.video_captured.emit(it)
            except Exception:
                pass
            time.sleep(self.check_interval)


class ChannelsStartWorker(QThread):
    """后台启动内核。

    首次使用需要从 GitHub 下载内核二进制（约 20MB），耗时可达数十秒，
    必须放到后台线程执行，否则会卡住整个 UI。
    """
    log_signal = Signal(str)
    done_signal = Signal(bool, str)

    def __init__(self, manager: "WeChatChannelsServiceManager"):
        super().__init__()
        self.manager = manager

    def run(self) -> None:
        self.manager.log_fn = self.log_signal.emit
        try:
            ok, msg = self.manager.start_service()
        except Exception as e:
            ok, msg = False, f"启动异常：{e}"
        finally:
            self.manager.log_fn = None
        self.done_signal.emit(bool(ok), str(msg))


# ======================================================================
# 4. 视频号网页链接 (channels.weixin.qq.com) 智能提取器
# ======================================================================

def extract_channels_web_video(
    url: str,
    on_log: Optional[Callable[[str], None]] = None,
    timeout: int = 15
) -> Optional[Dict[str, Any]]:
    """
    通过无头浏览器加载微信视频号网页预览页（channels.weixin.qq.com），
    自动拦截真实视频直链（finder.video.qq.com / stodownload）及标题封面元数据。
    """
    import m3u8_sniffer
    import tempfile
    import shutil

    def log(msg):
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    browser = m3u8_sniffer.find_browser()
    if not browser:
        log("⚠️ 未找到 Chrome/Edge 浏览器，无法嗅探视频号网页流。")
        return None

    port = m3u8_sniffer._free_port()
    udd = tempfile.mkdtemp(prefix="ytl-wx-")
    args = [
        browser,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--disable-gpu",
        "--mute-audio",
        "--autoplay-policy=no-user-gesture-required",
        "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "about:blank"
    ]

    proc = None
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wsurl = m3u8_sniffer._wait_devtools(port, log)
        if not wsurl:
            return None

        ws = m3u8_sniffer._WS(wsurl)
        ws.send({"id": 1, "method": "Network.enable", "params": {}})
        ws.send({"id": 2, "method": "Page.enable", "params": {}})
        ws.send({"id": 3, "method": "Runtime.enable", "params": {}})
        ws.send({"id": 4, "method": "Page.navigate", "params": {"url": url}})

        start = time.time()
        video_url = None
        title = "微信视频号视频"
        author = "视频号作者"
        cover_url = ""
        feed_req_id = None

        for frame in ws.frames():
            if time.time() - start > timeout:
                break
            if not frame:
                continue
            try:
                msg = json.loads(frame)
                method = msg.get("method")
                if method == "Network.requestWillBeSent":
                    req = msg["params"].get("request", {})
                    u = req.get("url", "")
                    if "get_feed_info" in u:
                        feed_req_id = msg["params"].get("requestId")
                    elif ("finder.video.qq.com" in u or "stodownload" in u) and not video_url:
                        # 预览页会用 stodownload 加载“封面图”（带 picformat 参数），
                        # 那不是视频流，误当成视频会下载出一张 jpg 冒充 mp4。
                        if is_channels_media_url(u):
                            video_url = u
                            log("🎬 成功捕获视频号真实视频流直链！")
                elif method == "Network.loadingFinished" and feed_req_id:
                    if msg["params"].get("requestId") == feed_req_id:
                        ws.send({"id": 888, "method": "Network.getResponseBody", "params": {"requestId": feed_req_id}})
                elif msg.get("id") == 888:
                    body = msg.get("result", {}).get("body", "")
                    if body:
                        try:
                            f_data = json.loads(body).get("data", {})
                            author = f_data.get("authorInfo", {}).get("nickname", author)
                            f_info = f_data.get("feedInfo", {})
                            desc = f_info.get("description", "")
                            if desc:
                                title = desc.strip()
                            cover_url = f_info.get("coverUrl", cover_url)
                        except Exception:
                            pass
            except Exception:
                pass

            if video_url and (time.time() - start > 3.5):
                break

        ws.close()
        if video_url:
            safe_title = re.sub(r'[\/:*?"<>|]', '_', title)[:100]
            return {
                "url": url,
                "video_url": video_url,
                "title": safe_title,
                "author": author,
                "thumb": cover_url,
                "media_type": "video"
            }
        # 没拿到视频流，但封面/作者/标题通常已经拿到：
        # 预览页（/sph?id=...）本身就不放流，只提示“前往微信观看”，
        # 这里把元数据带回去，让上层能显示正确标题并给出明确指引。
        if title and title != "微信视频号视频":
            safe_title = re.sub(r'[\/:*?"<>|]', '_', title)[:100]
            log("ℹ️ 预览页已拿到标题/封面，但视频流受微信限制，浏览器内无法取到。")
            return {
                "url": url,
                "video_url": "",
                "title": safe_title,
                "author": author,
                "thumb": cover_url,
                "media_type": "video",
                "need_wechat": True
            }
        return None
    except Exception as e:
        log(f"⚠️ 视频号网页解析异常: {e}")
        return None
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(udd, ignore_errors=True)
        except Exception:
            pass
