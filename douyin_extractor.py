# -*- coding: utf-8 -*-
"""
抖音（Douyin）无水印原画视频、图集、音频提取与下载器。

核心参考与整合自业界标杆项目：
- JoeanAmier/TikTokDownloader
- Evil0ctal/Douyin_TikTok_Download_API
- lijinrui182/douyin-8k-parser

支持：
1. 智能正则提纯（自动清洗包含文字、表情的分享文本）
2. 短链跟随 302 重定向提取真实 aweme_id
3. 视频模式：提取最高比特率无水印原画（1080P/2K/4K）视频直链
4. 图集模式（Note）：提取高清无水印图片组及背景音乐 (BGM)
5. 分块流式下载、断点续传与多图并发下载
"""

import os
import re
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import ssl
from typing import Optional, Dict, Any, List, Callable

# 默认请求头
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
PC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 正则表达式集合
DOUYIN_SHARE_TEXT_PATTERN = re.compile(
    r"(https?://(?:v|www|iesdouyin)\.(?:douyin|iesdouyin)\.com/[a-zA-Z0-9_\-\./\?\=\&\#\%]+)",
    re.IGNORECASE
)
AWEME_ID_PATTERN = re.compile(r"(?:video|note)/([0-9]+)")
NUMERIC_ID_PATTERN = re.compile(r"\b([0-9]{18,20})\b")


def is_douyin_url(text: str) -> bool:
    """检查文本或 URL 是否包含抖音链接。"""
    if not text:
        return False
    t = text.lower()
    return "douyin.com" in t or "iesdouyin.com" in t or bool(DOUYIN_SHARE_TEXT_PATTERN.search(text))


def clean_douyin_url(text: str) -> Optional[str]:
    """从可能包含汉字、表情、多余符号的分享文本中提取纯净的抖音 URL。"""
    if not text:
        return None
    match = DOUYIN_SHARE_TEXT_PATTERN.search(text.strip())
    if match:
        return match.group(1).strip()
    # 纯 ID 兜底
    id_match = NUMERIC_ID_PATTERN.search(text.strip())
    if id_match:
        return f"https://www.douyin.com/video/{id_match.group(1)}"
    return None


def _get_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_url(url: str, headers: dict = None, proxy: str = None, timeout: int = 15) -> tuple[bytes, str]:
    """发起 HTTP GET 请求，返回 (content_bytes, final_url)。"""
    req_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.douyin.com/",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, headers=req_headers)
    handlers = [urllib.request.HTTPSHandler(context=_get_ssl_context())]

    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        data = resp.read()
        return data, final_url


def extract_aweme_id(url_or_text: str, proxy: str = None) -> tuple[Optional[str], Optional[str]]:
    """
    从短链/分享文本中解析出 aweme_id 以及 media_type ("video" 或 "note")。
    """
    clean_url = clean_douyin_url(url_or_text)
    if not clean_url:
        return None, None

    # 直接长链匹配
    match = AWEME_ID_PATTERN.search(clean_url)
    if match:
        media_type = "note" if "note" in clean_url else "video"
        return match.group(1), media_type

    # 短链接跟随重定向
    try:
        req_headers = {
            "User-Agent": PC_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        _, final_url = _fetch_url(clean_url, headers=req_headers, proxy=proxy, timeout=10)
        match = AWEME_ID_PATTERN.search(final_url)
        if match:
            media_type = "note" if "note" in final_url else "video"
            return match.group(1), media_type

        # 尝试从 final_url 提取纯数字
        num_match = NUMERIC_ID_PATTERN.search(final_url)
        if num_match:
            media_type = "note" if "note" in final_url else "video"
            return num_match.group(1), media_type
    except Exception:
        pass

    return None, None


class DouyinExtractor:
    """抖音核心解析器与元数据提取器"""

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    def parse(self, url_or_text: str) -> Optional[Dict[str, Any]]:
        """
        全量解析抖音链接。
        返回字典结构：
        {
            "id": "7xxxxxxxxxxxx",
            "url": "https://www.douyin.com/video/7xxxx",
            "title": "作品标题",
            "author": "作者昵称",
            "author_id": "作者唯一ID",
            "thumb": "封面直链URL",
            "duration": 15000, # 毫秒
            "media_type": "video" | "image_album",
            "video_url": "最高清无水印mp4直链",
            "video_urls_backup": ["备用直链1", "备用直链2"],
            "images": ["高清图1", "高清图2", ...],
            "audio_url": "BGM直链URL",
            "extra_info": {...}
        }
        """
        aweme_id, media_type_hint = extract_aweme_id(url_or_text, self.proxy)
        if not aweme_id:
            return None

        # 尝试调用官方 Web API 端点
        detail_data = self._request_detail_api(aweme_id)
        if not detail_data or "aweme_detail" not in detail_data:
            # 备用接口重试
            detail_data = self._request_detail_api_backup(aweme_id)

        if not detail_data or "aweme_detail" not in detail_data:
            return None

        detail = detail_data["aweme_detail"]
        return self._format_detail(aweme_id, detail, media_type_hint)

    def _request_detail_api(self, aweme_id: str) -> Optional[dict]:
        """请求抖音官方网页端 detail 接口"""
        api_url = (
            f"https://www.douyin.com/aweme/v1/web/aweme/detail/?"
            f"aweme_id={aweme_id}&aid=1128&version_name=23.5.0&device_platform=android&os_version=2333"
        )
        headers = {
            "User-Agent": PC_UA,
            "Referer": f"https://www.douyin.com/video/{aweme_id}",
            "Accept": "application/json, text/plain, */*",
            "Cookie": "s_v_web_id=verify_lazy_guest; passport_csrf_token=guest_token;",
        }
        try:
            raw_bytes, _ = _fetch_url(api_url, headers=headers, proxy=self.proxy, timeout=12)
            return json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        except Exception:
            return None

    def _request_detail_api_backup(self, aweme_id: str) -> Optional[dict]:
        """备用 iesdouyin 接口"""
        api_url = f"https://www.iesdouyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}"
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.iesdouyin.com/",
            "Accept": "application/json",
        }
        try:
            raw_bytes, _ = _fetch_url(api_url, headers=headers, proxy=self.proxy, timeout=12)
            return json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        except Exception:
            return None

    def _format_detail(self, aweme_id: str, detail: dict, media_type_hint: Optional[str]) -> Dict[str, Any]:
        """提取并清洗作品元数据"""
        title = detail.get("desc", "").strip() or f"抖音作品_{aweme_id}"
        # 清除文件名不合法字符
        safe_title = re.sub(r'[\/:*?"<>|]', '_', title)[:100]

        author_info = detail.get("author", {})
        author_name = author_info.get("nickname", "未知创作者")
        author_id = author_info.get("unique_id") or author_info.get("short_id") or ""

        # 提取封面
        thumb_url = ""
        video_info = detail.get("video", {})
        if video_info:
            covers = (
                video_info.get("origin_cover", {}).get("url_list")
                or video_info.get("cover", {}).get("url_list")
                or []
            )
            if covers:
                thumb_url = covers[-1]

        duration = video_info.get("duration", 0)

        # 判断是图集还是视频
        images_data = detail.get("images") or []
        is_album = bool(images_data) or (media_type_hint == "note")

        images_urls = []
        if is_album and images_data:
            for img in images_data:
                u_list = img.get("url_list") or img.get("download_url_list") or []
                if u_list:
                    # 最后一个通常是最高清版本
                    images_urls.append(u_list[-1])
            if not thumb_url and images_urls:
                thumb_url = images_urls[0]

        # 提取视频直链（最高码率优先 + 自动去水印）
        video_url = ""
        backup_urls = []
        if not is_album and video_info:
            bit_rate_list = video_info.get("bit_rate") or []
            if bit_rate_list:
                # 按 bit_rate 码率降序排序，取最高画质
                sorted_rates = sorted(bit_rate_list, key=lambda x: x.get("bit_rate", 0), reverse=True)
                for rate_item in sorted_rates:
                    play_addrs = rate_item.get("play_addr", {}).get("url_list", [])
                    for u in play_addrs:
                        # 替换水印标志
                        clean_u = u.replace("playwm", "play")
                        if not video_url:
                            video_url = clean_u
                        elif clean_u not in backup_urls:
                            backup_urls.append(clean_u)

            # 兜底直接取 play_addr
            if not video_url:
                direct_addrs = video_info.get("play_addr", {}).get("url_list", [])
                for u in direct_addrs:
                    clean_u = u.replace("playwm", "play")
                    if not video_url:
                        video_url = clean_u
                    elif clean_u not in backup_urls:
                        backup_urls.append(clean_u)

            # 如果仍为空，尝试通过 snssdk play 接口构造
            if not video_url and video_info.get("play_addr", {}).get("uri"):
                uri = video_info["play_addr"]["uri"]
                video_url = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=default&improve_bitrate=1&watermark=0"

        # 提取音频 (BGM)
        music_info = detail.get("music", {})
        audio_url = ""
        if music_info:
            m_list = music_info.get("play_url", {}).get("url_list", [])
            if m_list:
                audio_url = m_list[-1]

        canonical_url = f"https://www.douyin.com/{'note' if is_album else 'video'}/{aweme_id}"

        return {
            "id": aweme_id,
            "url": canonical_url,
            "title": safe_title,
            "author": author_name,
            "author_id": author_id,
            "thumb": thumb_url,
            "duration": duration,
            "media_type": "image_album" if is_album else "video",
            "video_url": video_url,
            "video_urls_backup": backup_urls,
            "images": images_urls,
            "audio_url": audio_url,
            "extra_info": {
                "create_time": detail.get("create_time", 0),
                "statistics": detail.get("statistics", {}),
            }
        }


def download_douyin_media(
    parsed_info: Dict[str, Any],
    save_dir: str,
    temp_dir: str,
    on_progress: Optional[Callable[[float, str, int, int], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
    audio_only: bool = False,
) -> tuple[bool, str, str]:
    """
    下载抖音解析结果（全能下载执行器）。
    
    参数：
        parsed_info: DouyinExtractor.parse 返回的字典
        save_dir: 最终保存目录
        temp_dir: 临时缓存目录
        on_progress: 进度回调 (percent, speed_str, downloaded_bytes, total_bytes)
        on_log: 日志输出回调
        cancel_checker: 取消/暂停检测函数，返回 True 时中断
        audio_only: 是否仅下载音频并保存为 MP3

    返回：
        (success: bool, final_path: str, err_msg: str)
    """
    def log(msg: str):
        if on_log:
            on_log(msg)

    os.makedirs(save_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    title = parsed_info.get("title", "抖音作品")
    aweme_id = parsed_info.get("id", str(int(time.time())))
    media_type = parsed_info.get("media_type", "video")

    # 1. 仅音频模式
    if audio_only:
        audio_url = parsed_info.get("audio_url")
        if not audio_url and parsed_info.get("video_url"):
            audio_url = parsed_info.get("video_url")

        if not audio_url:
            return False, "", "未找到可用的音频直链"

        target_file = os.path.join(save_dir, f"{title}_{aweme_id}.mp3")
        temp_file = os.path.join(temp_dir, f"{aweme_id}_audio.tmp")
        log(f"🎵 开始下载抖音原生音频: {title}...")
        ok, err = _stream_download_file(audio_url, temp_file, on_progress, log, cancel_checker)
        if not ok:
            return False, "", err
        if os.path.exists(target_file):
            os.remove(target_file)
        os.rename(temp_file, target_file)
        log(f"✅ 抖音音频已成功保存: {target_file}")
        return True, target_file, ""

    # 2. 图集模式
    if media_type == "image_album":
        images = parsed_info.get("images", [])
        if not images:
            return False, "", "图集没有包含有效的图片链接"

        album_dir = os.path.join(save_dir, f"{title}_{aweme_id}")
        os.makedirs(album_dir, exist_ok=True)
        total_count = len(images)
        log(f"🖼️ 开始下载抖音图集（共 {total_count} 张高清原图）: {album_dir}...")

        downloaded_count = 0
        for idx, img_url in enumerate(images, 1):
            if cancel_checker and cancel_checker():
                log("⏸️ 图集下载已暂停/中断。")
                return False, "", "paused"

            img_path = os.path.join(album_dir, f"{idx:02d}.jpg")
            temp_img = os.path.join(temp_dir, f"{aweme_id}_img_{idx}.tmp")
            ok, _ = _stream_download_file(img_url, temp_img, None, None, cancel_checker)
            if ok:
                if os.path.exists(img_path):
                    os.remove(img_path)
                os.rename(temp_img, img_path)
                downloaded_count += 1
                percent = (downloaded_count / total_count) * 100.0
                if on_progress:
                    on_progress(percent, f"图集 {downloaded_count}/{total_count}", downloaded_count, total_count)

        # 下载背景音乐 (BGM)
        audio_url = parsed_info.get("audio_url")
        if audio_url:
            bgm_path = os.path.join(album_dir, f"BGM_{title}.mp3")
            temp_bgm = os.path.join(temp_dir, f"{aweme_id}_bgm.tmp")
            _stream_download_file(audio_url, temp_bgm, None, None, cancel_checker)
            if os.path.exists(temp_bgm):
                if os.path.exists(bgm_path):
                    os.remove(bgm_path)
                os.rename(temp_bgm, bgm_path)

        log(f"✅ 抖音图集全部下载完成，共保存 {downloaded_count} 张图片！")
        return True, album_dir, ""

    # 3. 视频模式
    video_url = parsed_info.get("video_url")
    backup_urls = parsed_info.get("video_urls_backup", [])
    all_urls = [video_url] + backup_urls if video_url else backup_urls

    if not all_urls:
        return False, "", "未提取到有效的抖音视频直链"

    target_file = os.path.join(save_dir, f"{title}_{aweme_id}.mp4")
    temp_file = os.path.join(temp_dir, f"{aweme_id}_video.mp4")

    last_err = ""
    for candidate_url in all_urls:
        if not candidate_url:
            continue
        if cancel_checker and cancel_checker():
            return False, "", "paused"

        log(f"🚀 开始下载抖音无水印高清原画视频...")
        ok, err = _stream_download_file(candidate_url, temp_file, on_progress, log, cancel_checker)
        if ok and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1024:
            if os.path.exists(target_file):
                os.remove(target_file)
            os.rename(temp_file, target_file)
            log(f"✅ 抖音无水印原画视频下载完成: {target_file}")
            return True, target_file, ""
        last_err = err

    return False, "", f"视频流下载失败: {last_err}"


def _stream_download_file(
    url: str,
    temp_path: str,
    on_progress: Optional[Callable[[float, str, int, int], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> tuple[bool, str]:
    """支持断点续传与速度计算的流式下载核心。"""
    downloaded_bytes = 0
    if os.path.exists(temp_path):
        downloaded_bytes = os.path.getsize(temp_path)

    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Referer": "https://www.douyin.com/",
    }
    if downloaded_bytes > 0:
        headers["Range"] = f"bytes={downloaded_bytes}-"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=30) as resp:
            content_length = resp.headers.get("Content-Length")
            total_bytes = int(content_length) + downloaded_bytes if content_length else 0

            mode = "ab" if downloaded_bytes > 0 else "wb"
            start_time = time.time()
            last_calc_time = start_time
            last_bytes = downloaded_bytes

            with open(temp_path, mode) as f:
                while True:
                    if cancel_checker and cancel_checker():
                        return False, "paused"

                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    now = time.time()
                    if now - last_calc_time >= 0.5:
                        duration = now - last_calc_time
                        speed_bps = (downloaded_bytes - last_bytes) / duration
                        speed_str = _format_speed(speed_bps)
                        percent = (downloaded_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0

                        if on_progress:
                            on_progress(percent, speed_str, downloaded_bytes, total_bytes)

                        last_calc_time = now
                        last_bytes = downloaded_bytes

            if total_bytes > 0 and on_progress:
                on_progress(100.0, "完成", total_bytes, total_bytes)

            return True, ""
    except urllib.error.HTTPError as e:
        if e.code == 416:  # Range Not Satisfiable -> 说明已经完整下载
            return True, ""
        return False, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)


def _format_speed(bps: float) -> str:
    """将字节/秒格式化为人类可读的字符串"""
    if bps < 1024:
        return f"{bps:.1f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps / (1024 * 1024):.2f} MB/s"
