# YTLdowner

**专业级视频下载工作站** — 基于 yt-dlp & FFmpeg 引擎驱动，支持全网视频断点续传、多策略 Cookie 轮询、智能队列调度。

## 功能特性

- **多引擎驱动** — 集成 yt-dlp、FFmpeg、FFprobe、Node.js，一站式管理
- **智能 Cookie 轮询** — 自动按序尝试：用户首选 → Chrome → Safari → Edge → 无 Cookie，突破网站风控
- **队列调度** — 支持并发下载、暂停/恢复、优先级排序（下载中 > 排队 > 暂停 > 失败 > 已完成）
- **代理隧道** — 内置 HTTP/HTTPS 代理支持，突破网络限制
- **组件升级** — 内置版本查询与一键升级，支持代理环境
- **剪贴板监听** — 自动检测并填入链接
- **断点续传** — 基于 yt-dlp 的 `--continue` 机制，意外中断后可恢复
- **浏览器智能嗅探** — 常规解析被风控拦截时，自动用无头浏览器(Chrome/Edge)打开页面、
  拦截带 timestamp/鉴权参数的真实 m3u8 直链（含咪咕等 HLS 站点），再无缝交接给 yt-dlp 下载
- **系列/播放列表选择** — 粘贴 YouTube 播放列表/频道合集（及 B 站分 P、通用合集）时，
  弹窗选择「下载整个系列」或「仅下载当前视频」；整集会后台展开为逐个任务并自动去重
- **画质与音频模式** — 设置中可选下载分辨率（最佳/4K/2K/1080p/720p/480p/360p），
  或勾选「仅下载音频」自动转为 MP3
- **跨平台** — 支持 macOS / Windows

## 快速开始

### 环境要求

- Python 3.9+
- PySide6
- yt-dlp（可选，项目自带打包版本）
- FFmpeg（可选，项目自带）

### 安装依赖

```bash
pip install PySide6
```

### 运行

```bash
python3 main.py
```

### 打包为独立应用

使用 PyInstaller（参考 `YTLdowner.spec`）：

```bash
pip install pyinstaller
pyinstaller YTLdowner.spec
```

打包产物在 `dist/`：`YTLdowner.app`（以及便于分发的 zip）。
在 Apple Silicon（M 系列，如 M4 Max）上构建出的即为原生 arm64 应用——主程序、
yt-dlp、node 均为 arm64，仓库内已随附 arm64 静态版 ffmpeg/ffprobe（不依赖 Rosetta）。

> macOS 首次打开非 App Store 应用：若提示“无法验证开发者”，右键点击 `YTLdowner.app`
> 选择「打开」即可；或在「系统设置 → 隐私与安全性」中点击「仍要打开」。

## 项目结构

```
YTLdowner/
├── main.py              # 主入口，UI 逻辑（首选项对话框、主窗口）
├── config.py            # 配置管理、工具路径、版本查询、代理设置
├── m3u8_sniffer.py       # 前置解析：无头浏览器(CDP)嗅探带鉴权的真实 m3u8
├── threads.py           # 工作线程（下载、信息解析、组件升级；含嗅探兜底与直链交接）
├── widgets.py           # 任务列表控件（TaskWidget）
├── ui_mainwindow.py     # Qt Designer 生成的主窗口 UI
├── help_dialog.py       # 帮助文档对话框
├── YTLdowner.spec       # PyInstaller 打包配置
├── yt-dlp.dat           # yt-dlp 打包二进制（macOS 伪装文件）
├── ffmpeg               # FFmpeg 二进制
├── ffprobe              # FFprobe 二进制
├── node                 # Node.js 二进制
└── Icon.*               # 应用图标
```

## 配置

配置文件存储在 `~/.ytldowner/config.json`，主要选项：

| 选项 | 说明 |
|------|------|
| `final_save_path` | 默认下载目录 |
| `proxy_enabled` | 是否启用代理 |
| `proxy_address` | 代理地址，如 `http://127.0.0.1:7890` |
| `cookie_mode` | Cookie 策略：0=无, 1=Safari, 2=Chrome, 3=Edge, 4=导入文件 |
| `cookie_path` | 导入的 Cookie 文件路径 |
| `concurrent` | 同时下载任务数（1-10） |
| `shutdown_enabled` | 全部完成后自动关机 |
| `resolution` | 下载画质上限：best/2160/1440/1080/720/480/360 |
| `audio_only` | true=仅下载音频并转 MP3 |

## 组件升级

打开首选项（⚙️）→ 组件升级区，可查看和升级各组件版本：

- **yt-dlp** — 视频下载引擎，支持自升级或直接下载最新二进制
- **FFmpeg / FFprobe** — 音视频处理，从 FFmpeg-Builds 下载
- **Node.js** — 运行时，从 nodejs.org 下载

版本查询使用并行 HTTP 请求，支持代理环境。

## 常见问题

### 下载返回 403 Forbidden

目标网站可能启用了反爬机制。尝试以下方法：

1. 在首选项中开启代理
2. 切换 Cookie 策略为 Chrome 或 Safari
3. 使用浏览器插件（如 Get cookies.txt LOCALLY）导出目标网站的 Cookie 文件，选择"导入本地 txt..."
4. 升级 yt-dlp 到最新版本

### 组件版本查询失败

1. 检查网络连接，开启代理（如需要）
2. 点击"🔄 刷新版本信息"重试
3. 检查 `~/.ytldowner/bin/` 目录下是否有对应二进制文件

### macOS 上提示 "semaphore" 错误

这是 PyInstaller 打包的二进制在沙箱环境中的限制，不影响实际使用。如果 yt-dlp 无法运行，可通过组件升级下载原生 macOS 版本。

## 许可

本项目仅供学习研究使用。请遵守目标网站的服务条款。
