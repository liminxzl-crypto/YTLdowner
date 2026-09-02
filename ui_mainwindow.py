from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QListWidget, QTextEdit, QLabel, 
                               QMenuBar, QSplitter)
from PySide6.QtCore import Qt

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        MainWindow.resize(900, 650)
        # 恢复中立标题，等待 main.py 接管动态注入
        MainWindow.setWindowTitle("YTLdowner")

        self.centralwidget = QWidget(MainWindow)
        MainWindow.setCentralWidget(self.centralwidget)

        # 🌟 1. 构建顶部系统菜单栏
        self.menubar = QMenuBar(MainWindow)
        MainWindow.setMenuBar(self.menubar)

        # [文件] 菜单
        self.menu_file = self.menubar.addMenu("文件")
        self.action_load = self.menu_file.addAction("📂 导入历史队列...")
        self.action_export = self.menu_file.addAction("💾 导出当前队列...")
        self.menu_file.addSeparator()
        self.action_exit = self.menu_file.addAction("❌ 退出程序")

        # [设置] 菜单
        self.menu_settings = self.menubar.addMenu("设置")
        self.action_preferences = self.menu_settings.addAction("⚙️ 首选项...")

        # [帮助] 菜单
        self.menu_help = self.menubar.addMenu("帮助")
        # 🌟 新增：帮助文档菜单项
        self.action_help_doc = self.menu_help.addAction("📖 使用说明与帮助")
        self.menu_help.addSeparator()
        self.action_about = self.menu_help.addAction("ℹ️ 关于...")

        # 🌟 2. 核心主布局
        self.main_layout = QVBoxLayout(self.centralwidget)
        self.main_layout.setContentsMargins(15, 15, 15, 15)
        self.main_layout.setSpacing(10)

        # 顶部操作区
        self.top_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        
        self.url_input.setPlaceholderText("📋 请在此粘贴视频链接...")
        self.url_input.setMinimumHeight(40)
        self.url_input.setStyleSheet("padding: 5px 10px; font-size: 14px; border: 2px solid #bdc3c7; border-radius: 6px;")
        
        self.btn_quick_download = QPushButton("⬇️ 添加到队列")
        self.btn_quick_download.setFixedSize(120, 40)
        self.btn_quick_download.setStyleSheet("background-color: #3498db; color: white; font-weight: bold; border-radius: 6px;")
        
        self.btn_start = QPushButton("🚀 全局启动")
        self.btn_start.setFixedSize(120, 40)
        self.btn_start.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; border-radius: 6px;")

        self.top_layout.addWidget(self.url_input)
        self.top_layout.addWidget(self.btn_quick_download)
        self.top_layout.addWidget(self.btn_start)
        self.main_layout.addLayout(self.top_layout)

        # 中间核心区：使用分割器
        self.splitter = QSplitter(Qt.Vertical)
        
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("QListWidget { border: 1px solid #bdc3c7; border-radius: 6px; background-color: #f8f9fa; }")
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("QTextEdit { background-color: #1e1e1e; color: #a9b7c6; font-family: monospace; border-radius: 6px; padding: 5px; }")
        
        self.splitter.addWidget(self.list_widget)
        self.splitter.addWidget(self.log_output)
        self.splitter.setSizes([450, 150]) 
        
        self.main_layout.addWidget(self.splitter)

        # 底部状态栏
        self.status_label = QLabel("💤 待命 | 请粘贴链接或在系统菜单中进行设置")
        self.status_label.setStyleSheet("color: #7f8c8d; font-size: 12px; padding-top: 5px;")
        self.main_layout.addWidget(self.status_label)