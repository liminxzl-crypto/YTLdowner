# 文件名：help_dialog.py

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTextBrowser, QPushButton
from PySide6.QtCore import Qt

class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📖 YTLdowner 使用说明与帮助")
        self.resize(650, 550)
        
        layout = QVBoxLayout(self)
        
        # 使用浏览器控件，支持富文本排版
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet("background-color: #ffffff; border-radius: 6px; padding: 10px; font-size: 13px;")
        
        help_html = """
        <h2 style='color: #2c3e50; font-family: sans-serif;'>YTLdowner 专业视频下载工作站 - 使用指南</h2>
        <hr>
        
        <h3 style='color: #2980b9;'>🚀 1. 基础快速入门</h3>
        <ul>
            <li><b>添加任务：</b>复制各大视频网站（YouTube、B站、抖音、视频号、X、Vimeo 等）的视频链接，粘贴到顶部输入框，点击【添加到队列】或按回车。</li>
            <li><b>剪贴板极速识别：</b>支持复制抖音 App 分享文本（含汉字表情），软件将全自动清洗并提取出真实链接。</li>
            <li><b>开始下载：</b>点击右上角的【全局启动】按钮，引擎将自动并发处理队列中的任务。</li>
        </ul>

        <h3 style='color: #e74c3c;'>🎵 2. 抖音（Douyin）无水印原画视频 & 图集下载</h3>
        <ul>
            <li><b>多格式支持：</b>直接支持手机 App 分享短链（v.douyin.com/...）、长链接（douyin.com/video/...）、以及包含“复制此条消息”的整段文字。</li>
            <li><b>原画质与去水印：</b>内置专属解析引擎，自动挑选最高比特率码率流，一键去除平台水印。</li>
            <li><b>图集（Note）一键下载：</b>自动检测图集内容，下载全部高清无水印图片至专属文件夹，并自动提取附带背景音乐 (BGM)。</li>
        </ul>

        <h3 style='color: #27ae60;'>🎬 3. 微信视频号（WeChat Channels）智能抓取与解密</h3>
        <ul>
            <li><b>视频号抓取助手：</b>点击顶部【🎬 视频号助手】按钮，即可开启监听服务（按钮变为绿色表示已就绪）。</li>
            <li><b>零配置内核自举：</b>首次开启时，软件会按你的系统与 CPU 架构（macOS / Windows / Linux，arm64 / x86_64）自动下载并安装视频号抓取内核，无需手工下载任何文件（约 20MB，需联网）。</li>
            <li><b>极简捕获：</b>在电脑端微信（Mac / Windows）中打开并播放任意视频号视频，助手将自动截获视频直链、封面与解密密钥，并自动推送到下载队列！</li>
            <li><b>本地 Isaac64 瞬时解密：</b>视频号视频流的前 128KB 为加密数据，下载后将在本地用 Isaac64 密钥流无损解密，还原为标准 MP4 文件。</li>
            <li><b>代理全自动托管：</b>抓取内核需要接管系统代理才能解析视频号流量。软件会在启动前自动记录你的原有代理设置，并在关闭助手时原样还原，全程无需你手动干预（关闭助手或退出软件即自动恢复）。</li>
            <li><b>提示：</b>助手仅在「开启状态」下拦截流量；日常不使用视频号下载时，保持助手关闭即可。</li>
        </ul>
        
        <h3 style='color: #2980b9;'>💡 4. 进度条与状态指示灯说明</h3>
        <ul>
            <li><span style='color: #3498db;'><b>🟦 蓝色进度条：</b></span>引擎正在进行高速网络数据拉取。</li>
            <li><span style='color: #27ae60;'><b>🔂 绿色激荡能量条：</b></span>数据拉取达 100%，正在后台调用 FFmpeg 进行音视频高保真混流封装或本地解密，请耐心等待。</li>
            <li><span style='color: #2ecc71;'><b>🟩 纯绿满格条：</b></span>任务彻底完成，视频/图集已安全保存到本地，双击任务即可直接打开播放或浏览文件夹。</li>
        </ul>

        <h3 style='color: #2980b9;'>⚙️ 5. 核心高阶功能（首选项设置）</h3>
        <ul>
            <li><b>突破限制 (Cookie 借用)：</b>遇到需要登录才能看的会员视频、年龄限制视频，可在设置中选择【借用 Chrome/Edge 身份】，软件将直接使用你浏览器的登录凭证去下载。</li>
            <li><b>代理穿透：</b>国内下载被墙视频时，请在设置中勾选【启用本地代理】并填入代理端口（如 http://127.0.0.1:7890）。</li>
            <li><b>并发控制：</b>可根据你的宽带情况调整同时下载任务数（1~10 个任务并行）。</li>
        </ul>

        <h3 style='color: #2980b9;'>⚠️ 6. 常见问题 (FAQ)</h3>
        <ul>
            <li><b>问：为什么抖音分享文本复制后会自动变短？</b><br>答：软件具备智能清洗功能，自动去除了冗余的分享文案与推广尾巴，只保留纯净的作品地址。</li>
            <li><b>问：视频号助手开启后为什么微信没反应？</b><br>答：请确认助手按钮已是绿色的「🎬 视频号监听中」状态，然后在电脑端微信中<b>点击播放</b>该视频号视频（仅停留在封面不算）。捕获成功会在日志区看到「捕获视频号内容」提示。</li>
            <li><b>问：首次开启视频号助手卡了一会儿？</b><br>答：首次使用需要联网从 GitHub 下载抓取内核（约 20MB），请耐心等待日志出现「视频号内核已就绪」。若网络受限，请先在首选项中配置可用代理。</li>
            <li><b>问：开启助手会影响我上网吗？</b><br>答：助手运行期间会临时接管系统代理以解析视频号流量，这是内核的工作方式。关闭助手或退出软件时，软件会自动把你的代理设置<b>原样还原</b>，无需手动处理。</li>
            <li><b>问：不小心点错了右上角的 X 关掉了软件怎么办？</b><br>答：软件默认会最小化到系统托盘后台运行，不会中断任务。右键托盘图标可呼出主界面或彻底退出。</li>
        </ul>
        """
        browser.setHtml(help_html)
        layout.addWidget(browser)
        
        # 底部按钮
        btn_close = QPushButton("我知道了")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet("padding: 8px 20px; font-weight: bold; background-color: #3498db; color: white; border-radius: 4px;")
        btn_close.clicked.connect(self.accept)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)