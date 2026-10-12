# YTLdowner

**视频下载工作站** —— 不是又一个 yt-dlp 套壳。

YTLdowner 是基于 yt-dlp + FFmpeg 引擎的桌面下载工作站，专为"难搞的网站"而生：常规解析被风控拦截时，它会自动用无头浏览器嗅探带鉴权的真实 m3u8 直链；网站要求登录态时，它会按序轮询 Chrome → Safari → Edge → 导入 Cookie，直到拿到数据。

更狠的是：它内置了**微信视频号**与**抖音**的专属解析引擎——视频号能在电脑微信里播放时自动捕获直链与密钥、本地无损解密为标准 MP4；抖音直接去水印拿最高码率流。

## 为什么选 YTLdowner？

- 🧠 **反爬组合拳** —— Cookie 智能轮询 + 无头浏览器 m3u8 嗅探 + 代理隧道，咪咕、B 站这类 HLS 站点也能拿下
- 🎬 **微信视频号** —— 一键开启助手，在电脑微信里播放即自动捕获并下载；抓取内核按平台自动安装，本地 Isaac64 无损解密，零手工配置
- 🎵 **抖音无水印** —— 专属解析引擎自动挑选最高码率流去水印；图集一键下载全部高清原图并提取背景音乐
- 📋 **队列工作站** —— 并发 1–10 个任务，暂停/恢复、优先级调度、断点续传，全部下完可自动关机
- 🎞️ **列表一键展开** —— YouTube 播放列表、B 站分 P，粘贴即展开成批量任务并自动去重
- 🎚️ **画质与音频** —— 7 档分辨率（最佳/4K/2K/1080p/720p/480p/360p）可选，或一键只下载音频并转 MP3
- 🔧 **组件自维护** —— yt-dlp / FFmpeg / FFprobe / Node.js 版本查询 + 一键升级，代理环境也支持
- 🖥️ **跨平台开箱即用** —— macOS / Windows；PyInstaller 打包，macOS arm64 原生应用，FFmpeg 等二进制全部内置，剪贴板监听自动填链接

> 适合每天跟视频下载打交道的人：素材党、归档党、被风控折磨过的，都懂。

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

> `YTLdowner.spec` 只打包主程序与基础工具；视频号抓取内核**不打进安装包**，
> 而是在首次使用时按当前平台自动下载（保持安装包体积精简）。

> macOS 首次打开非 App Store 应用：若提示“无法验证开发者”，右键点击 `YTLdowner.app`
> 选择「打开」即可；或在「系统设置 → 隐私与安全性」中点击「仍要打开」。

## 微信视频号下载

这是本项目的一等公民能力：**在电脑微信里播放视频号，软件自动捕获并下载为可直接播放的 MP4。**

### 使用步骤

1. 点击主界面顶部的 **🎬 视频号助手** 按钮。
   - 首次使用会自动从 GitHub 下载抓取内核（约 20MB，需联网），日志区会显示进度；
   - 按钮变为绿色的 **🎬 视频号监听中** 即表示就绪。
2. 打开**电脑端微信**，进入视频号，**点击播放**任意视频（仅停留在封面不触发捕获）。
3. 日志区出现「🔐 含解密密钥 捕获视频号内容：xxx」后，任务会自动进入下载队列。
4. 下载完成后在保存目录得到标准 MP4；关闭助手时自动还原你的系统代理。

### 工作原理

```
┌──────────────┐   播放视频号    ┌─────────────────────┐   直链+decode_key   ┌──────────────┐
│  电脑端微信   │ ─────────────▶ │ 视频号抓取内核        │ ──────────────────▶ │  YTLdowner   │
│ (Mac/Win)    │                │ (本地透明代理，按需自举)│   写入 SQLite       │  下载+解密    │
└──────────────┘                └─────────────────────┘                     └──────────────┘
                                                                                    │
                                                              Isaac64 密钥流 XOR 前128KB
                                                                                    ▼
                                                                            标准 MP4（H.264/AAC）
```

1. **内核自举**：首次开启助手时，按 `平台 + CPU 架构` 从
   [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download) 的
   Release 自动下载对应二进制（macOS arm64/x86_64、Windows x86_64/arm64、Linux x86_64/arm64），
   解压到 `~/.ytldowner/bin/` 并清除 quarantine 隔离属性。
2. **流量解析**：内核在本机架设透明代理，接管微信视频号的请求，解析出真实视频直链、
   封面与解密密钥，落库到 `~/.ytldowner/wx_capture/wxd/data.db`。
3. **捕获入队**：YTLdowner 以只读方式轮询该 SQLite 库，发现新视频即以信号推送入队
   （启动时既有的历史记录会被记为基线，不会重复下载）。
4. **本地解密**：视频号视频流**前 128KB 为加密数据**，本项目实现了纯 Python 的
   ISAAC64 密钥流生成器完成 XOR 解密，产物与官方内核解密结果**逐字节一致**。

### 关于代理（无需你操心）

抓取内核必须接管系统代理才能解析视频号流量，这是它的工作方式。本项目对此做了**全自动托管**：

- 启动助手前自动**快照**系统各网络服务的 HTTP/HTTPS/SOCKS 代理配置；
- 关闭助手或退出软件时**原样还原**，不留下任何改动；
- 内置**孤儿进程回收**：即使上次异常退出导致内核仍占用端口，下次启动会自动清理；
- 上游代理留空（直连），不会把你内网的代理配置牵进链路。

> 也就是说：你**不需要**手动开关任何代理设置。关闭助手即恢复原状。

### 手动下载与网页链接

- 直接粘贴视频号分享短链（`https://weixin.qq.com/sph/XXXX`）或页面链接时，
  软件会尝试解析页面元数据。**注意**：视频号网页预览版只加载封面与文案，
  视频流被限制为「前往微信观看」，因此**网页链接无法直接下载**——
  这是微信的限制，不是软件问题。正确做法是走上面的「助手 + 微信内播放」。

## 项目结构

```
YTLdowner/
├── main.py                # 主入口，UI 逻辑（首选项对话框、主窗口、视频号助手开关）
├── config.py              # 配置管理、工具路径、版本查询、代理设置
├── threads.py             # 工作线程（下载、信息解析、组件升级；含抖音/视频号专属管道）
├── wx_channel_helper.py   # 视频号：内核自举、服务生命周期、代理快照还原、Isaac64 解密、DB 读取
├── douyin_extractor.py    # 抖音无水印解析与通用流式下载器
├── m3u8_sniffer.py        # 前置解析：无头浏览器(CDP)嗅探带鉴权的真实 m3u8
├── widgets.py             # 任务列表控件（TaskWidget）
├── ui_mainwindow.py       # Qt Designer 生成的主窗口 UI
├── help_dialog.py         # 帮助文档对话框
├── wx_capture_addon.py    # 【实验性·备用】mitmproxy 捕获脚本，主链路不依赖
├── YTLdowner.spec         # PyInstaller 打包配置
├── yt-dlp.dat             # yt-dlp 打包二进制（macOS 伪装文件）
├── ffmpeg                 # FFmpeg 二进制
├── ffprobe                # FFprobe 二进制
├── node                   # Node.js 二进制
└── Icon.*                 # 应用图标
```

### 运行时数据目录

```
~/.ytldowner/
├── bin/                   # 弹射的可执行文件（yt-dlp/ffmpeg/node/wx_video_download）
├── wx_capture/
│   ├── wxd/               # 视频号内核工作目录（config.yaml / data.db / kernel.log）
│   ├── proxy_snapshot.json# 代理快照（退出时还原用，还原后自动删除）
│   └── proxy_backup.txt   # 早期备份留下的说明文件
├── config.json            # 应用配置
├── download_queue.json    # 下载队列持久化
└── .thumbs/               # 任务缩略图缓存
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

> 这里的代理设置用于「下载」环节（含视频号内核的首次下载）。
> 视频号助手运行期的系统代理接管由软件自动管理，与该项无关。

## 组件升级

打开首选项（⚙️）→ 组件升级区，可查看和升级各组件版本：

- **yt-dlp** — 视频下载引擎，支持自升级或直接下载最新二进制
- **FFmpeg / FFprobe** — 音视频处理，从 FFmpeg-Builds 下载
- **Node.js** — 运行时，从 nodejs.org 下载

版本查询使用并行 HTTP 请求，支持代理环境。

## 常见问题

### 视频号助手开启了但捕获不到

1. 确认按钮已是绿色的「🎬 视频号监听中」；
2. 在电脑微信里**真正点击播放**该视频（仅预览封面不算）；
3. 确认日志无「视频号内核启动失败」字样；若有，多为上次内核未退出导致端口占用，
   软件会自动清理，重开一次助手即可；
4. 首次使用需联网下载内核，请检查网络/代理。

### 关闭助手后网络异常

正常情况下软件会自动还原代理。若遭遇强制退出（如系统断电、强杀进程）导致
代理残留，可手动执行：

```bash
# 查看当前代理
networksetup -getwebproxy "Wi-Fi"
# 关闭代理
networksetup -setwebproxystate "Wi-Fi" off
```

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

## 版本历史

- **v1.4.0** — 微信视频号下载能力内建：
  - 新增抓取内核跨平台自举（自动下载/安装/健康检查/孤儿进程回收）
  - 新增系统代理自动快照与还原，助手开关全程零配置
  - 改为直接读取内核 SQLite 库获取直链与密钥，移除对猜测端口的依赖
  - **修复 Isaac64 解密的两处致命缺陷**（密钥流字节序 + 采样索引错位），
    解密产物与官方内核结果逐字节一致
  - UI：助手启动改为后台线程（首次下载内核不再卡界面），启动失败会明确报错
- **v1.3.0** — 系列/播放列表选择、画质与仅音频设置、m3u8 智能嗅探

## 致谢

- [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download) — 视频号抓取内核
- [Evil0ctal/WeChat-Channels-Video-File-Decryption](https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption) — Isaac64 解密算法参考
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) / [FFmpeg](https://ffmpeg.org/)

## 许可

本项目仅供学习研究使用。请遵守目标网站的服务条款。
