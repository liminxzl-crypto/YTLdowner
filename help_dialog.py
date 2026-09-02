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
            <li><b>添加任务：</b>复制各大视频网站的视频链接，粘贴到顶部输入框，点击【添加到队列】或按回车。</li>
            <li><b>开始下载：</b>点击右上角的【全局启动】按钮，引擎将自动并发处理队列中的任务。</li>
        </ul>
        
        <h3 style='color: #2980b9;'>💡 2. 进度条与状态指示灯说明</h3>
        <ul>
            <li><span style='color: #3498db;'><b>🟦 蓝色进度条：</b></span>引擎正在进行高速网络数据拉取。</li>
            <li><span style='color: #27ae60;'><b>🔂 绿色激荡能量条：</b></span>数据拉取达100%，正在后台调用 FFmpeg 进行音视频高保真混流封装，请耐心等待（耗时取决于视频大小和电脑 CPU 性能）。</li>
            <li><span style='color: #2ecc71;'><b>🟩 纯绿满格条：</b></span>任务彻底完成，视频已安全保存到本地。</li>
        </ul>

        <h3 style='color: #2980b9;'>⚙️ 3. 核心高阶功能（首选项设置）</h3>
        <ul>
            <li><b>突破限制 (Cookie 借用)：</b>遇到需要登录才能看的会员视频、年龄限制视频，可在设置中选择【借用 Chrome/Edge 身份】，软件将直接使用你浏览器的登录凭证去下载。</li>
            <li><b>代理穿透：</b>国内下载被墙视频时，请在设置中勾选【启用本地代理】并填入代理端口（如 http://127.0.0.1:7890）。</li>
            <li><b>并发控制：</b>可根据你的宽带情况调整同时下载任务数。</li>
        </ul>

        <h3 style='color: #2980b9;'>⚠️ 4. 常见问题 (FAQ)</h3>
        <ul>
            <li><b>问：为什么下载速度偶尔会显示 "引擎处理中..."？</b><br>答：底层引擎正在解析视频轨道结构，或尝试建立最优下载节点，几秒钟后即可显示真实速度。</li>
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