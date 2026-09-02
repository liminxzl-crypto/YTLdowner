import os
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel, 
                               QPushButton, QProgressBar, QMenu, QApplication, QStyle)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap, QCursor

class TaskWidget(QWidget):
    action_clicked = Signal(str, str)

    def __init__(self, title, thumb_path, url, status="waiting", progress=0, speed="", eta=""):
        super().__init__()
        self.url = url
        self.status = status
        self.title = title  
        self.setFixedHeight(85)
        
        self.marquee_val = 0
        self.marquee_dir = 3  
        self.marquee_timer = QTimer(self)
        self.marquee_timer.timeout.connect(self._animate_marquee)
        
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(120, 68)
        self.thumb_label.setStyleSheet("background-color: #2c3e50; border-radius: 4px;")
        self.thumb_label.setScaledContents(True)
        if thumb_path and os.path.exists(thumb_path):
            self.thumb_label.setPixmap(QPixmap(thumb_path))
        else:
            self.thumb_label.setText("MEDIA")
            self.thumb_label.setStyleSheet("background-color: #34495e; color: #7f8c8d; font-weight: bold; border-radius: 4px;")
            self.thumb_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.thumb_label)
        
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #ecf0f1;")
        info_layout.addWidget(self.title_label)
        
        status_layout = QHBoxLayout()
        
        speed_text = ""
        if status == "finished":
            speed_text = "✅ 任务已完成"  # 🌟 核心修改：去掉了“双击播放”这种废话
        elif status == "downloading":
            if progress == 100:
                speed_text = "🔄 正在调用 FFmpeg 封装合成..."
            else:
                speed_text = speed if (speed and "计算中" not in speed) else "🚀 引擎已激活，正在连接资源..."
        elif status == "waiting":
            speed_text = "⏳ 排队等待中..."
        elif status == "paused":
            speed_text = "⏸️ 任务已手动暂停"
        elif status == "error":
            speed_text = "❌ 下载异常，请重试"
        else:
            speed_text = speed

        self.speed_label = QLabel(speed_text)
        self.speed_label.setStyleSheet("color: #3498db; font-size: 11px;")
        
        eta_text = ""
        if status == "downloading":
            if progress == 100:
                eta_text = "极速渲染中..."
            elif eta and "计算中" not in eta:
                eta_text = f"⏳ 剩余: {eta}"
                
        self.eta_label = QLabel(eta_text)
        self.eta_label.setStyleSheet("color: #e67e22; font-size: 11px;")
        
        status_layout.addWidget(self.speed_label)
        status_layout.addStretch()
        status_layout.addWidget(self.eta_label)
        info_layout.addLayout(status_layout)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 100)
        
        if status == "finished":
            self.progress_bar.setValue(100)
            self.progress_bar.setStyleSheet("""
                QProgressBar { border: none; background-color: #2c3e50; border-radius: 3px; }
                QProgressBar::chunk { background-color: #2ecc71; border-radius: 3px; margin: 0px; width: 1px; }
            """)
        elif status == "downloading" and progress == 100:
            self.marquee_timer.start(15) 
            self.progress_bar.setStyleSheet("""
                QProgressBar { border: none; background-color: #2c3e50; border-radius: 3px; }
                QProgressBar::chunk { background-color: #27ae60; border-radius: 3px; margin: 0px; width: 1px; }
            """)
        else:
            self.progress_bar.setValue(progress)
            self.progress_bar.setStyleSheet("""
                QProgressBar { border: none; background-color: #2c3e50; border-radius: 3px; }
                QProgressBar::chunk { background-color: #3498db; border-radius: 3px; margin: 0px; width: 1px; }
            """)
            
        info_layout.addWidget(self.progress_bar)
        main_layout.addLayout(info_layout)
        
        btn_layout = QVBoxLayout()
        style = QApplication.style()
        
        self.btn_toggle = QPushButton()
        self.btn_toggle.setFixedSize(32, 32)
        self.btn_toggle.setCursor(Qt.PointingHandCursor)
        
        if status == "finished":
            self.btn_toggle.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
            self.btn_toggle.setEnabled(False)
            self.btn_toggle.setStyleSheet("QPushButton { background-color: transparent; border: none; }")
        else:
            is_downloading = (status == "downloading")
            icon = QStyle.SP_MediaPause if is_downloading else QStyle.SP_MediaPlay
            self.btn_toggle.setIcon(style.standardIcon(icon))
            self.btn_toggle.setStyleSheet("""
                QPushButton { background-color: #ecf0f1; border-radius: 16px; border: 1px solid #bdc3c7; }
                QPushButton:hover { background-color: #d5d8dc; }
            """)
        
        self.btn_del = QPushButton()
        self.btn_del.setFixedSize(32, 32)
        self.btn_del.setCursor(Qt.PointingHandCursor)
        self.btn_del.setIcon(style.standardIcon(QStyle.SP_TrashIcon))
        self.btn_del.setStyleSheet("""
            QPushButton { background-color: #fadbd8; border-radius: 16px; border: 1px solid #f5b7b1; }
            QPushButton:hover { background-color: #f1948a; }
        """)
        
        btn_layout.addWidget(self.btn_toggle)
        btn_layout.addWidget(self.btn_del)
        main_layout.addLayout(btn_layout)
        
        if status != "finished":
            action = "pause" if status == "downloading" else "start"
            self.btn_toggle.clicked.connect(lambda: self.action_clicked.emit(action, self.url))
        self.btn_del.clicked.connect(lambda: self.action_clicked.emit("delete", self.url))

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self.status == "finished":
            self.action_clicked.emit("play_video", self.url)
        super().mouseDoubleClickEvent(event)

    def _animate_marquee(self):
        self.marquee_val += self.marquee_dir
        if self.marquee_val >= 100:
            self.marquee_val = 100
            self.marquee_dir = -3  
        elif self.marquee_val <= 0:
            self.marquee_val = 0
            self.marquee_dir = 3   
        self.progress_bar.setValue(self.marquee_val)

    def show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2c3e50; color: white; border: 1px solid #34495e; padding: 5px; border-radius: 5px;}
            QMenu::item { padding: 6px 25px 6px 25px; border-radius: 3px; }
            QMenu::item:selected { background-color: #3498db; }
            QMenu::separator { height: 1px; background-color: #34495e; margin: 4px 0px 4px 0px; }
        """)
        
        play_act, folder_act = None, None
        if self.status == "finished":
            play_act = menu.addAction("▶️ 默认播放器打开")
            folder_act = menu.addAction("📂 打开下载文件位置")
            menu.addSeparator()
            
        start_act, pause_act = None, None
        if self.status != "finished":
            start_act = menu.addAction("播放 / 继续")
            pause_act = menu.addAction("暂停任务")
            menu.addSeparator()
            
        top_act = menu.addAction("置顶任务")
        up_act = menu.addAction("向上移动")
        down_act = menu.addAction("向下移动")
        menu.addSeparator()
        copy_act = menu.addAction("复制视频网址")
        del_act = menu.addAction("删除任务")
        
        action = menu.exec(self.mapToGlobal(pos))
        
        if self.status == "finished":
            if action == play_act: self.action_clicked.emit("play_video", self.url)
            elif action == folder_act: self.action_clicked.emit("open_folder", self.url)
            
        if self.status != "finished":
            if action == start_act: self.action_clicked.emit("start", self.url)
            elif action == pause_act: self.action_clicked.emit("pause", self.url)
            
        if action == top_act: self.action_clicked.emit("top", self.url)
        elif action == up_act: self.action_clicked.emit("up", self.url)
        elif action == down_act: self.action_clicked.emit("down", self.url)
        elif action == copy_act: self.action_clicked.emit("copy_url", self.url)
        elif action == del_act: self.action_clicked.emit("delete", self.url)

    def update_progress(self, p, s, e):
        if self.status != "finished":
            if p == 100:
                if not self.marquee_timer.isActive():
                    self.marquee_timer.start(15) 
                
                self.speed_label.setText("🔄 正在调用 FFmpeg 封装合成...")
                self.eta_label.setText("极速渲染中...")
                self.progress_bar.setStyleSheet("""
                    QProgressBar { border: none; background-color: #2c3e50; border-radius: 3px; }
                    QProgressBar::chunk { background-color: #27ae60; border-radius: 3px; margin: 0px; width: 1px; }
                """)
            else:
                self.marquee_timer.stop() 
                self.progress_bar.setValue(p)
                
                if s and "计算中" not in s:
                    self.speed_label.setText(f"⚡ 速度: {s}")
                else:
                    self.speed_label.setText("🚀 引擎处理中...")
                    
                if e and "计算中" not in e:
                    self.eta_label.setText(f"⏳ 剩余: {e}")
                else:
                    self.eta_label.setText("")
                    
                self.progress_bar.setStyleSheet("""
                    QProgressBar { border: none; background-color: #2c3e50; border-radius: 3px; }
                    QProgressBar::chunk { background-color: #3498db; border-radius: 3px; margin: 0px; width: 1px; }
                """)