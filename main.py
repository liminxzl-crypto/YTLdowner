import sys
import os
import json
import platform 
import re  
import subprocess 
from PySide6.QtWidgets import (QApplication, QMainWindow, QFileDialog, QMessageBox, 
                               QListWidgetItem, QDialog, QVBoxLayout, QHBoxLayout, 
                               QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox, 
                               QCheckBox, QSystemTrayIcon, QMenu, QStyle)
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QIcon, QAction

from config import (BASE_DIR, QUEUE_FILE, CONFIG_FILE, get_tool_version, get_latest_version,
                    set_proxy_config, ensure_binaries_ready)
from ui_mainwindow import Ui_MainWindow
from widgets import TaskWidget
from threads import (InfoThread, SingleDownloadWorker, UpgradeWorker,
                        PlaylistExpandThread, build_media_args, looks_like_playlist)
from help_dialog import HelpDialog

# ==========================================
# 🌟 组件版本缓存（跨对话框实例持久化，避免重复查询）
# ==========================================
from config import load_version_cache, save_version_cache

_VERSION_CACHE = load_version_cache()  # {tool: {"local": "xxx", "latest": "xxx"}}

def _update_version_cache(local_vers, latest_vers):
    for t in local_vers:
        if t not in _VERSION_CACHE:
            _VERSION_CACHE[t] = {}
        _VERSION_CACHE[t]["local"] = local_vers[t]
        _VERSION_CACHE[t]["latest"] = latest_vers.get(t, "获取失败")
    save_version_cache(_VERSION_CACHE)

# ==========================================
# 🌟 独立的“首选项”设置弹窗类
# ==========================================
class PreferencesDialog(QDialog):
    def __init__(self, parent=None, current_config=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ 软件首选项")
        self.setFixedSize(540, 560)
        self.config = current_config.copy() if current_config else {}

        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit(self.config.get("final_save_path", os.path.join(os.path.expanduser("~"), "Videos")))
        self.path_input.setReadOnly(True)
        self.btn_browse = QPushButton("选择目录")
        self.btn_browse.clicked.connect(self.browse_path)
        path_layout.addWidget(QLabel("📂 默认下载目录:"))
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.btn_browse)
        layout.addLayout(path_layout)

        hint = QLabel("💡 提示: 缓存碎片将自动存放在此目录下的 temp 文件夹中，下完自动清理。")
        hint.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        layout.addWidget(hint)

        proxy_layout = QHBoxLayout()
        self.chk_proxy = QCheckBox("🌐 启用本地代理 (防墙拦截)")
        # 用 blockSignals 防止 setChecked 触发 toggled 导致未初始化完成就启动后台线程
        self.chk_proxy.blockSignals(True)
        self.chk_proxy.setChecked(self.config.get("proxy_enabled", False))
        self.chk_proxy.blockSignals(False)
        
        self.proxy_input = QLineEdit(self.config.get("proxy_address", "http://127.0.0.1:7890"))
        self.proxy_input.setPlaceholderText("例如: http://127.0.0.1:7890")
        self.proxy_input.setEnabled(self.chk_proxy.isChecked())
        
        self.chk_proxy.toggled.connect(self.proxy_input.setEnabled)
        self.chk_proxy.toggled.connect(lambda: self._on_proxy_changed())
        proxy_layout.addWidget(self.chk_proxy)
        proxy_layout.addWidget(self.proxy_input)
        layout.addLayout(proxy_layout)

        cookie_layout = QHBoxLayout()
        self.cookie_combo = QComboBox()
        self.cookie_combo.addItems(["无 / 免 Cookie (易被墙)", "🌐 借用 Safari 身份", "🌐 借用 Chrome 身份", "🌐 借用 Edge 身份", "📂 导入本地 txt..."])
        self.cookie_combo.setCurrentIndex(self.config.get("cookie_mode", 0))
        
        self.btn_browse_cookie = QPushButton("更换文件")
        self.btn_browse_cookie.setCursor(Qt.PointingHandCursor)
        self.btn_browse_cookie.setStyleSheet("padding: 2px 8px; font-size: 12px; background-color: #ecf0f1; border-radius: 4px;")
        self.btn_browse_cookie.clicked.connect(self.browse_cookie_file)

        self.cookie_label = QLabel()
        self.cookie_label.setStyleSheet("color: #27ae60; font-weight: bold; font-size: 12px; padding-left: 5px;")
        
        cookie_layout.addWidget(QLabel("🍪 账号身份凭证:"))
        cookie_layout.addWidget(self.cookie_combo)
        cookie_layout.addWidget(self.btn_browse_cookie)
        cookie_layout.addWidget(self.cookie_label)
        cookie_layout.addStretch()
        layout.addLayout(cookie_layout)

        self.update_cookie_label_display()
        self.cookie_combo.currentIndexChanged.connect(self.on_cookie_changed)

        concurrent_layout = QHBoxLayout()
        self.spin_concurrent = QSpinBox()
        self.spin_concurrent.setRange(1, 10)
        self.spin_concurrent.setValue(self.config.get("concurrent", 3))
        concurrent_layout.addWidget(QLabel("⚡ 同时下载任务数:"))
        concurrent_layout.addWidget(self.spin_concurrent)
        concurrent_layout.addStretch()
        layout.addLayout(concurrent_layout)

        # 🌟 下载画质 / 仅音频
        media_layout = QHBoxLayout()
        media_layout.addWidget(QLabel("🎞️ 下载画质:"))
        self.res_combo = QComboBox()
        self.res_combo.addItems([
            "最佳画质 (自动)", "4K (2160p)", "2K (1440p)",
            "1080p", "720p", "480p", "360p",
        ])
        res_map = {"best":0, "2160":1, "1440":2, "1080":3, "720":4, "480":5, "360":6}
        self.res_combo.setCurrentIndex(res_map.get(str(self.config.get("resolution", "best")), 0))
        media_layout.addWidget(self.res_combo)
        media_layout.addSpacing(20)

        self.chk_audio = QCheckBox("🎧 仅下载音频 (转 MP3)")
        self.chk_audio.setChecked(bool(self.config.get("audio_only", False)))
        media_layout.addWidget(self.chk_audio)
        media_layout.addStretch()
        layout.addLayout(media_layout)

        # 勾选“仅音频”时禁用画质选择
        self.chk_audio.toggled.connect(lambda on: self.res_combo.setEnabled(not on))
        self.res_combo.setEnabled(not self.chk_audio.isChecked())

        self.chk_shutdown = QCheckBox("📴 所有任务彻底完成后，自动关闭计算机")
        self.chk_shutdown.setChecked(self.config.get("shutdown_enabled", False))
        layout.addWidget(self.chk_shutdown)
        # ==========================================
        # 🌟 组件升级区
        # ==========================================
        upgrade_label = QLabel("🔧 组件升级")
        upgrade_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #2980b9; margin-top: 8px;")
        layout.addWidget(upgrade_label)

        self.upgrade_grid = QHBoxLayout()
        self.upgrade_buttons = {}
        self.upgrade_status = {}
        self.upgrade_latest = {}

        for tool in ["yt-dlp", "ffmpeg", "ffprobe", "node"]:
            vbox = QVBoxLayout()
            vbox.setSpacing(4)

            # 先显示"查询中"，不阻塞 UI
            status_label = QLabel(f"{tool}\n本地: 查询中…\n最新: 查询中…")
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setStyleSheet("font-size: 10px; color: #bdc3c7; padding: 2px;")
            status_label.setFixedWidth(120)
            self.upgrade_status[tool] = status_label
            self.upgrade_latest[tool] = "查询中"

            btn = QPushButton("⬆️ 升级")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("padding: 4px 10px; font-size: 11px; background-color: #e67e22; color: white; border-radius: 4px;")
            btn.setFixedWidth(120)
            btn.setEnabled(False)  # 查询完成后再启用
            btn.clicked.connect(lambda checked, t=tool: self.start_upgrade(t))
            self.upgrade_buttons[tool] = btn

            vbox.addWidget(status_label)
            vbox.addWidget(btn)
            self.upgrade_grid.addLayout(vbox)

        layout.addLayout(self.upgrade_grid)

        # 刷新 / 重试按钮
        refresh_layout = QHBoxLayout()
        refresh_layout.addStretch()
        self.btn_refresh_versions = QPushButton("🔄 刷新版本信息")
        self.btn_refresh_versions.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_versions.setStyleSheet("padding: 2px 10px; font-size: 10px; background-color: #95a5a6; color: white; border-radius: 3px;")
        self.btn_refresh_versions.clicked.connect(self._refresh_versions_async)
        refresh_layout.addWidget(self.btn_refresh_versions)
        layout.addLayout(refresh_layout)

        # 如果有缓存，直接显示上次查询结果；否则后台查询
        if _VERSION_CACHE:
            self._display_cached_versions()
        else:
            self._start_version_check()


        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_save = QPushButton("保存设置")
        btn_save.setStyleSheet("background-color: #3498db; color: white; font-weight: bold; padding: 6px;")
        btn_cancel = QPushButton("取消")
        btn_save.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def browse_path(self):
        p = QFileDialog.getExistingDirectory(self, "选择默认下载保存目录", self.path_input.text())
        if p: self.path_input.setText(p)

    def browse_cookie_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 Cookie 文本文件", "", "Text files (*.txt)")
        if p:
            self.config["cookie_path"] = p
            self.update_cookie_label_display()

    def on_cookie_changed(self, idx):
        if idx == 4:
            if not self.config.get("cookie_path"):
                self.browse_cookie_file()
                if not self.config.get("cookie_path"):
                    self.cookie_combo.setCurrentIndex(0)
        self.update_cookie_label_display()

    def update_cookie_label_display(self):
        if self.cookie_combo.currentIndex() == 4:
            self.btn_browse_cookie.show()
            if self.config.get("cookie_path"):
                filename = os.path.basename(self.config["cookie_path"])
                self.cookie_label.setText(f"[{filename}]")
                self.cookie_label.show()
            else:
                self.cookie_label.hide()
        else:
            self.btn_browse_cookie.hide()
            self.cookie_label.hide()

    def get_updated_config(self):
        self.config["final_save_path"] = self.path_input.text()
        self.config["proxy_enabled"] = self.chk_proxy.isChecked()
        self.config["proxy_address"] = self.proxy_input.text()
        self.config["cookie_mode"] = self.cookie_combo.currentIndex()
        self.config["concurrent"] = self.spin_concurrent.value()
        self.config["shutdown_enabled"] = self.chk_shutdown.isChecked()
        res_values = ["best", "2160", "1440", "1080", "720", "480", "360"]
        self.config["resolution"] = res_values[self.res_combo.currentIndex()]
        self.config["audio_only"] = self.chk_audio.isChecked()
        return self.config

    def closeEvent(self, event):
        """关闭对话框时标记已关闭，后台线程由模块级列表持有，安全结束"""
        self._closing = True
        super().closeEvent(event)

    def _on_versions_ready(self, local_vers, latest_vers):
        """后台版本查询完成后的回调"""
        if getattr(self, '_closing', False):
            return
        # 更新全局缓存
        _update_version_cache(local_vers, latest_vers)
        for tool in ["yt-dlp", "ffmpeg", "ffprobe", "node"]:
            local_ver = local_vers.get(tool, "获取失败")
            latest_ver = latest_vers.get(tool, "获取失败")
            self.upgrade_latest[tool] = latest_ver
            self.upgrade_status[tool].setText(f"{tool}\n本地: {local_ver}\n最新: {latest_ver}")

            # 版本相同时禁用升级按钮，无法获取版本时也禁用
            can_upgrade = (latest_ver not in ("获取失败", "未知", "未安装", "本地异常")
                           and local_ver not in ("获取失败", "未知", "本地异常")
                           and latest_ver != local_ver)
            self.upgrade_buttons[tool].setEnabled(can_upgrade)

    def _display_cached_versions(self):
        """直接显示缓存的版本信息，不发起网络请求"""
        for tool in ["yt-dlp", "ffmpeg", "ffprobe", "node"]:
            cache = _VERSION_CACHE.get(tool, {})
            local_ver = cache.get("local", "获取失败")
            latest_ver = cache.get("latest", "获取失败")
            self.upgrade_latest[tool] = latest_ver
            self.upgrade_status[tool].setText(f"{tool}\n本地: {local_ver}\n最新: {latest_ver}")

            # 版本相同时禁用升级按钮，无法获取版本时也禁用
            can_upgrade = (latest_ver not in ("获取失败", "未知", "未安装", "本地异常")
                           and local_ver not in ("获取失败", "未知", "本地异常")
                           and latest_ver != local_ver)
            self.upgrade_buttons[tool].setEnabled(can_upgrade)

    def _on_proxy_changed(self):
        """代理开关变化时，立即同步到全局并重新查询版本"""
        from config import set_proxy_config
        set_proxy_config(
            self.chk_proxy.isChecked(),
            self.proxy_input.text().strip()
        )
        self._refresh_versions_async()

    def start_upgrade(self, tool):
        """启动升级线程"""
        btn = self.upgrade_buttons[tool]
        btn.setEnabled(False)
        btn.setText("⏳ 升级中...")
        self.upgrade_status[tool].setText(f"{tool}\n⏳ 升级中...")

        self.upgrade_worker = UpgradeWorker(tool)
        self.upgrade_worker.log_signal.connect(lambda msg: self.parent().update_log(msg) if self.parent() else None)
        self.upgrade_worker.finished_signal.connect(lambda t, ok: self.on_upgrade_finished(t, ok))
        self.upgrade_worker.start()

    def on_upgrade_finished(self, tool, success):
        btn = self.upgrade_buttons[tool]
        btn.setText("⬆️ 升级")

        if success:
            self.upgrade_status[tool].setStyleSheet("font-size: 10px; color: #2ecc71; padding: 2px;")
        else:
            self.upgrade_status[tool].setStyleSheet("font-size: 10px; color: #e74c3c; padding: 2px;")

        # 后台重新查询版本（不阻塞 UI）
        self._refresh_versions_async()

    def _start_version_check(self):
        """启动后台版本检查（QObject + Signal 跨线程安全回调）"""
        import threading
        from PySide6.QtCore import QObject, Signal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # 用 QObject + Signal 确保跨线程安全（QTimer.singleShot 从后台线程调用无效！）
        class _SignalBridge(QObject):
            done = Signal(dict, dict)

        if not hasattr(self, '_version_bridges'):
            self._version_bridges = []
        bridge = _SignalBridge()
        self._version_bridges.append(bridge)  # 防止被 GC
        bridge.done.connect(self._on_versions_ready)

        def _check():
            try:
                tools = ["yt-dlp", "ffmpeg", "ffprobe", "node"]
                local_vers = {t: get_tool_version(t) for t in tools}
                latest_vers = {}
                with ThreadPoolExecutor(max_workers=len(tools)) as executor:
                    fut_map = {executor.submit(get_latest_version, t): t for t in tools}
                    for fut in as_completed(fut_map):
                        t = fut_map[fut]
                        try:
                            latest_vers[t] = fut.result()
                        except Exception:
                            latest_vers[t] = "获取失败"
                bridge.done.emit(local_vers, latest_vers)
            except Exception:
                bridge.done.emit({}, {})

        t = threading.Thread(target=_check, daemon=True)
        t.start()

    def _refresh_versions_async(self):
        """后台刷新所有组件版本信息"""
        self._start_version_check()


# ==========================================
# 🌟 主控大脑
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.queue = [] 
        self.active_workers = {} 
        self.task_widgets = {} 
        self.is_queue_running = False 
        self.running_info_threads = []
        self.version = "1.3.0" 
        
        self.is_force_quit = False
        self._last_clipboard_text = ""

        self.config = {
            "final_save_path": os.path.join(os.path.expanduser("~"), "Videos"),
            "proxy_enabled": False,
            "proxy_address": "http://127.0.0.1:7890",
            "cookie_mode": 0,
            "cookie_path": "",
            "concurrent": 3,
            "shutdown_enabled": False,
            "resolution": "best",
            "audio_only": False,
            "last_dialog_dir": BASE_DIR
        }
        self.load_config()
        try:
            ensure_binaries_ready()
        except Exception:
            pass

        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        
        self.os_name = "Win" if platform.system() == "Windows" else "Mac"
        self.setWindowTitle(f"YTLdowner {self.os_name} v{self.version} - 专业视频下载工作站")
        
        self.bind_events()
        self.auto_load_queue()
        

        
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.timeout.connect(self.manual_save)
        self.auto_save_timer.start(3000)

        self.setup_tray_icon()

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Icon.ico")
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
            
        tray_menu = QMenu()
        
        show_action = QAction("🖥️ 显示下载面板", self)
        show_action.triggered.connect(self.showNormal)
        tray_menu.addAction(show_action)
        
        tray_menu.addSeparator() 
        
        quit_action = QAction("❌ 完全退出程序", self)
        quit_action.triggered.connect(self.force_quit)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        
        self.tray_icon.activated.connect(self.on_tray_icon_activated)

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick or reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.showNormal()
            self.activateWindow()

    def force_quit(self):
        self.is_force_quit = True
        self.tray_icon.hide() 
        QApplication.quit()

    def closeEvent(self, event):
        if self.is_force_quit:
            if self.active_workers:
                for w in self.active_workers.values(): w.pause()
            self.manual_save()
            event.accept()
            return

        if hasattr(self, 'tray_icon') and self.tray_icon.isVisible():
            self.hide()
            self.update_log("🔽 软件已最小化到系统托盘后台运行。")
            event.ignore() 
        else:
            if self.active_workers:
                reply = QMessageBox.question(self, '确认退出', "任务正在下载，确定要中断并暂停吗？", QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    for w in self.active_workers.values(): w.pause()
                    self.manual_save(); event.accept()
                else: event.ignore()
            else:
                self.manual_save(); event.accept()

    def bind_events(self):
        self.ui.url_input.returnPressed.connect(self.add_task)
        self.ui.btn_quick_download.clicked.connect(self.add_task)
        self.ui.btn_start.clicked.connect(self.toggle_global_state)
        
        self.ui.action_preferences.triggered.connect(self.open_preferences)
        self.ui.action_load.triggered.connect(self.manual_load)
        self.ui.action_export.triggered.connect(self.export_queue)
        self.ui.action_exit.triggered.connect(self.force_quit) 
        
        self.ui.action_help_doc.triggered.connect(self.show_help_doc)
        self.ui.action_about.triggered.connect(self.show_about)

        # 用定时器轮询剪贴板，避免 macOS 上 dataChanged 信号导致线程崩溃
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.timeout.connect(self.check_clipboard)
        self._clipboard_timer.start(1000)

    def update_log(self, text):
        self.ui.log_output.append(text)
        self.ui.log_output.verticalScrollBar().setValue(self.ui.log_output.verticalScrollBar().maximum())

    def open_preferences(self):
        dialog = PreferencesDialog(self, self.config)
        if dialog.exec() == QDialog.Accepted:
            self.config = dialog.get_updated_config()
            self.save_config()
            self.update_log("⚙️ 软件首选项设置已更新保存。")
            self.refresh_ui_list() 

    def show_help_doc(self):
        dialog = HelpDialog(self)
        dialog.exec()

    def show_about(self):
        QMessageBox.about(self, "关于 YTLdowner", 
                          f"YTLdowner {self.os_name} v{self.version}\n\n"
                          "专业级双端视频下载工作站。\n"
                          "基于 yt-dlp & FFmpeg 引擎驱动。\n\n"
                          "支持全网断点续传、无限重试、智能后台缓存隔离及代理隧道穿越。\n\n"
                          "Designed for Professional Video Editors.")

    def check_clipboard(self):
        try:
            text = QApplication.clipboard().text().strip()
        except Exception:
            return
        if text == self._last_clipboard_text:
            return
        self._last_clipboard_text = text
        if text.startswith("http://") or text.startswith("https://"):
            if self.ui.url_input.text() != text:
                self.ui.url_input.setText(text)
                self.update_log(f"📋 检测到剪切板新链接，已自动填入。")

    def get_temp_path(self):
        return os.path.join(self.config["final_save_path"], "temp")

    def get_extra_args(self):
        args = []
        if self.config.get("proxy_enabled") and self.config.get("proxy_address"):
            args.extend(["--proxy", self.config["proxy_address"]])
            
        mapping = {1: "safari", 2: "chrome", 3: "edge"}
        idx = self.config.get("cookie_mode", 0)
        if idx in mapping: args.extend(["--cookies-from-browser", mapping[idx]])
        elif idx == 4 and self.config.get("cookie_path"): args.extend(["--cookies", self.config["cookie_path"]])
        
        return args

    def get_media_args(self):
        """根据设置里的画质 / 仅音频偏好，生成 yt-dlp 媒体参数。"""
        return build_media_args(
            resolution=self.config.get("resolution", "best"),
            audio_only=bool(self.config.get("audio_only", False)),
        )

    # 🌟 核心升级区：添加新任务时，动态寻找正确的位置进行插队！
    def add_task(self):
        url = self.ui.url_input.text().strip()
        self.ui.url_input.clear()
        if not url: return

        # 系列 / 播放列表：先让用户选择“整个系列”还是“仅当前视频”
        if looks_like_playlist(url):
            self._choose_playlist_scope(url)
            return

        self._enqueue_url(url)

    def _choose_playlist_scope(self, url):
        box = QMessageBox(self)
        box.setWindowTitle("检测到系列 / 播放列表")
        box.setIcon(QMessageBox.Question)
        box.setText(
            "这个链接属于一个系列（播放列表 / 频道合集）。\n\n"
            "请选择下载方式：")
        btn_series = box.addButton("📚 下载整个系列", QMessageBox.AcceptRole)
        btn_single = box.addButton("🎬 仅下载当前视频", QMessageBox.RejectRole)
        btn_cancel = box.addButton("取消", QMessageBox.DestructiveRole)
        box.setDefaultButton(btn_single)
        box.exec()
        clicked = box.clickedButton()

        if clicked is btn_cancel:
            self.update_log("🚫 已取消添加该系列任务。")
            return
        mode = "series" if clicked is btn_series else "single"
        extra_args = self.get_extra_args()
        self.update_log(f"🔎 正在展开{'整个系列' if mode=='series' else '当前视频'}: {url}")
        et = PlaylistExpandThread(url, extra_args, mode=mode)
        self.running_info_threads.append(et)
        et.log_signal.connect(self.update_log)
        et.finished_signal.connect(self.on_playlist_expanded)
        et.finished.connect(lambda: self.cleanup_thread(et))
        et.start()

    def on_playlist_expanded(self, orig_url, items, err):
        if not items:
            self.update_log(f"⚠️ 系列解析失败：{err or '未解析到视频'}，将按单个链接处理。")
            self._enqueue_url(orig_url)
            return
        self.update_log(f"📚 系列展开成功，共 {len(items)} 个视频，开始加入队列。")
        added = 0
        for it in items:
            u = it.get("url")
            if not u:
                continue
            # flat-playlist 的相对 id 尽量补成完整链接
            if not u.startswith("http"):
                if "youtube" in orig_url and not u.startswith("http"):
                    u = "https://www.youtube.com/watch?v=" + u
                else:
                    continue
            if any(t['url'] == u for t in self.queue):
                continue
            self._enqueue_url(u, title_hint=it.get("title"))
            added += 1
        self.update_log(f"✅ 已加入 {added} 个新任务（重复链接已自动跳过）。")

    def _enqueue_url(self, url, title_hint=""):
        if any(t['url'] == url for t in self.queue):
            self.update_log(f"⚠️ 该链接已在列表中，跳过：{url}")
            return

        extra_args = self.get_extra_args()
        media_args = self.get_media_args()
        self.update_log(f"🆕 加入下载调度矩阵: {url}")

        new_task = {
            "url": url,
            "title": title_hint or "⏳ 正在解析视频信息...",
            "thumb": "",
            "status": "waiting",
            "save_path": self.config["final_save_path"]
        }

        # 🌟 动态插队算法：找到所有未完成任务的尾部，将新任务放在已完成任务的最前面
        insert_idx = len(self.queue)
        for i, t in enumerate(self.queue):
            if t.get('status') == 'finished':
                insert_idx = i
                break

        self.queue.insert(insert_idx, new_task)

        self.refresh_ui_list()
        self.manual_save()

        t = InfoThread(url, extra_args, media_args=media_args)
        self.running_info_threads.append(t)
        t.log_signal.connect(self.update_log)
        t.finished_signal.connect(self.on_info_fetched)
        t.finished.connect(lambda: self.cleanup_thread(t))
        t.start()

        if not self.is_queue_running:
            self.update_log("▶️ 检测到新任务，自动唤醒下载引擎...")
            self.start_queue_engine()
        else:
            self.dispatch_tasks()

    def cleanup_thread(self, thread_obj):
        if thread_obj in self.running_info_threads:
            self.running_info_threads.remove(thread_obj)

    def on_info_fetched(self, data):
        for t in self.queue:
            if t['url'] == data['url']:
                t['title'], t['thumb'] = data['title'], data['thumb']
                break
        self.refresh_ui_list()
        self.manual_save()

    def toggle_global_state(self):
        if self.is_queue_running:
            self.pause_all()
        else:
            self.start_all()

    def start_all(self):
        if not self.queue: return
        for t in self.queue: 
            if t.get('status') in ['paused', 'error']: 
                t['status'] = 'waiting'
        self.start_queue_engine()
        self.update_log("▶️ 已全局启动所有待机和异常任务。")

    def start_queue_engine(self):
        self.is_queue_running = True
        self.ui.btn_start.setText("⏸️ 全部暂停")
        self.ui.btn_start.setStyleSheet("background-color: #e67e22; color: white; font-weight: bold; border-radius: 6px;")
        self.dispatch_tasks()
        self.refresh_ui_list()

    def pause_all(self):
        self.is_queue_running = False
        self.ui.btn_start.setText("🚀 全局启动")
        self.ui.btn_start.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; border-radius: 6px;")
        
        for t in self.queue:
            if t.get('status') == 'waiting':
                t['status'] = 'paused'
                
        for url, worker in list(self.active_workers.items()):
            worker.pause()
            for t in self.queue:
                if t['url'] == url:
                    t['status'] = 'paused'
                    break
                    
        self.refresh_ui_list()
        self.manual_save()
        self.update_log("⏸️ 全局下载已强行暂停。")

    def dispatch_tasks(self):
        if not self.is_queue_running: return
        concurrent_limit = self.config.get("concurrent", 3)
        
        for t in self.queue:
            if len(self.active_workers) >= concurrent_limit: break
            url = t['url']
            
            if url not in self.active_workers and t.get('status') == 'waiting':
                w = SingleDownloadWorker(url, self.config["final_save_path"], self.get_temp_path(),
                                          self.get_extra_args(), media_args=self.get_media_args())
                w.output_signal.connect(self.update_log)
                w.progress_signal.connect(self.on_task_progress)
                w.finished_signal.connect(self.on_task_finished)
                
                self.active_workers[url] = w
                w.start()
        self.refresh_ui_list() 

    def on_task_progress(self, url, p, s, e):
        if url in self.task_widgets:
            self.task_widgets[url].update_progress(p, s, e)
        for t in self.queue:
            if t['url'] == url:
                t['progress'] = p
                t['speed'] = s
                t['eta'] = e
                break

    def on_task_finished(self, url, success, msg):
        if url in self.active_workers: del self.active_workers[url]
        
        task_to_move = None
        
        if success:
            self.update_log(f"✅ 下载完成: {url}")
            for i, t in enumerate(self.queue):
                if t['url'] == url:
                    t['status'] = 'finished'
                    t['progress'] = 100
                    t['speed'] = '已完成'
                    t['eta'] = '--'
                    if msg and os.path.isabs(msg):
                        t['exact_filepath'] = os.path.normpath(msg)
                    task_to_move = self.queue.pop(i)
                    break
            
            if task_to_move:
                insert_idx = len(self.queue)
                for i, t in enumerate(self.queue):
                    if t.get('status') == 'finished':
                        insert_idx = i
                        break
                self.queue.insert(insert_idx, task_to_move)

        elif msg != "paused":
            for t in self.queue: 
                if t['url'] == url: t['status'] = 'error' 
                    
        self.refresh_ui_list()
        self.manual_save()
        self.dispatch_tasks() 
        
        if self.is_queue_running and len(self.active_workers) == 0 and not any(t.get('status') == 'waiting' for t in self.queue):
            self.is_queue_running = False
            self.ui.btn_start.setText("🚀 全局启动")
            self.ui.btn_start.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; border-radius: 6px;")
            self.update_log("🎉 队列内任务已全部处理完毕！")
            
            if self.config.get("shutdown_enabled"):
                self.update_log("⚠️ 触发自动关机指令...")
                if os.name == 'nt': os.system("shutdown -s -t 60")
                elif sys.platform == 'darwin': os.system("osascript -e 'tell app \"System Events\" to shut down'")

    def handle_task_action(self, act, url):
        idx = next((i for i, t in enumerate(self.queue) if t['url'] == url), -1)
        if idx == -1: return
        
        if act in ["play_video", "open_folder"]:
            exact_path = self.queue[idx].get('exact_filepath')
            task_title = self.queue[idx]['title']
            task_save_path = self.queue[idx].get('save_path', self.config["final_save_path"])
            
            target_file = None
            
            if exact_path and os.path.exists(exact_path):
                target_file = exact_path
            else:
                target_file = self.find_downloaded_file(task_title, task_save_path)
            
            if target_file and os.path.exists(target_file):
                try:
                    target_file = os.path.normpath(target_file) 
                    if act == "open_folder":
                        if os.name == 'nt':
                            subprocess.Popen(['explorer', '/select,', target_file])
                        else:
                            subprocess.Popen(['open', '-R', target_file])
                    elif act == "play_video":
                        if os.name == 'nt':
                            os.startfile(target_file)
                        else:
                            subprocess.Popen(['open', target_file])
                except Exception as e:
                    self.update_log(f"❌ 系统调用失败: {str(e)}")
                return
            else:
                if os.path.exists(task_save_path):
                    task_save_path = os.path.normpath(task_save_path)
                    if act == "open_folder":
                        if os.name == 'nt':
                            os.startfile(task_save_path)
                        else:
                            subprocess.Popen(['open', task_save_path])
                    elif act == "play_video":
                        QMessageBox.warning(self, "定位失败", "精准寻址与雷达搜索均失败！\n\n文件已被您重命名、移出当前目录，或这是未记录路径的旧任务。已为您打开存放文件夹。")
                else:
                    QMessageBox.warning(self, "文件丢失", "定位失败！\n\n您不仅移走了文件，连原文件夹也被删除了。")
                return
            
        if act == "pause":
            if url in self.active_workers: self.active_workers[url].pause()
            self.queue[idx]['status'] = 'paused'
        elif act == "start": 
            self.queue[idx]['status'] = 'waiting'
            self.start_queue_engine()
        elif act == "delete": 
            reply = QMessageBox.question(
                self, '确认删除', 
                "⚠️ 确定要删除该任务吗？\n\n如果你点击确定，该任务将从列表中移除，且下载进程会被强制终止。",
                QMessageBox.Yes | QMessageBox.No, 
                QMessageBox.No 
            )
            if reply == QMessageBox.Yes:
                if url in self.active_workers: self.active_workers[url].pause()
                self.queue.pop(idx)
                self.update_log("🗑️ 任务已被手动删除。")
            else:
                return 
        elif act == "top" and idx > 0: 
            task = self.queue.pop(idx)
            self.queue.insert(0, task)
        elif act == "up" and idx > 0: 
            self.queue[idx], self.queue[idx-1] = self.queue[idx-1], self.queue[idx]
        elif act == "down" and idx < len(self.queue) - 1: 
            self.queue[idx], self.queue[idx+1] = self.queue[idx+1], self.queue[idx]
        elif act == "copy_url": 
            QApplication.clipboard().setText(url)
            self.update_log(f"📋 已成功复制视频网址: {url}")
            return 
            
        self.refresh_ui_list()
        self.manual_save()

    def find_downloaded_file(self, title, save_dir):
        if not os.path.exists(save_dir): return None
        
        clean_title = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', title).lower()
        if not clean_title: return None
        
        try:
            files = sorted(os.listdir(save_dir), key=lambda x: os.path.getmtime(os.path.join(save_dir, x)), reverse=True)
        except:
            files = os.listdir(save_dir)
            
        for f in files:
            if f.startswith('.') or f.endswith(('.part', '.ytdl', '.temp')):
                continue
                
            clean_f = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', f).lower()
            
            keyword_head = clean_title[:8]
            if keyword_head and keyword_head in clean_f:
                return os.path.join(save_dir, f)
                
            if len(clean_title) > 10:
                keyword_mid = clean_title[4:10]
                if keyword_mid and keyword_mid in clean_f:
                    return os.path.join(save_dir, f)
                    
        return None

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f: 
                    saved_config = json.load(f)
                    self.config.update(saved_config)
            except: pass
        # 同步代理配置到全局，供版本查询和升级使用
        set_proxy_config(
            self.config.get("proxy_enabled", False),
            self.config.get("proxy_address", "")
        )

    def save_config(self):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f)
        # 同步代理配置到全局
        set_proxy_config(
            self.config.get("proxy_enabled", False),
            self.config.get("proxy_address", "")
        )

    def export_queue(self):
        if not self.queue: return
        file_path, _ = QFileDialog.getSaveFileName(self, "导出当前队列", self.config.get("last_dialog_dir", BASE_DIR), "JSON files (*.json)")
        if file_path:
            try:
                if not file_path.endswith('.json'): file_path += '.json'
                with open(file_path, 'w') as f: json.dump(self.queue, f)
                self.config["last_dialog_dir"] = os.path.dirname(file_path)
                self.save_config()
                self.update_log(f"💾 队列已成功保存至: [{os.path.basename(file_path)}]")
            except Exception as e: self.update_log(f"❌ 导出失败: {str(e)}")

    def manual_load(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "导入历史记录", self.config.get("last_dialog_dir", BASE_DIR), "JSON files (*.json)")
        if not file_path or not os.path.exists(file_path): return 
        try:
            with open(file_path, 'r') as f: saved_queue = json.load(f)
            self.config["last_dialog_dir"] = os.path.dirname(file_path)
            self.save_config()
            current_urls = {t['url'] for t in self.queue}
            added, skipped = 0, 0
            for t in saved_queue:
                if isinstance(t, dict) and 'url' in t:
                    if t['url'] not in current_urls:
                        if t.get('status') == 'downloading': t['status'] = 'waiting'
                        self.queue.append(t); added += 1
                    else: skipped += 1
            if added > 0:
                self.update_log(f"📂 成功导入 {added} 个任务。")
                self.refresh_ui_list(); self.manual_save(); self.dispatch_tasks()
        except Exception as e: self.update_log(f"❌ 导入失败: {str(e)}")

    def auto_load_queue(self):
        if os.path.exists(QUEUE_FILE):
            try:
                with open(QUEUE_FILE, 'r') as f: saved_queue = json.load(f)
                for t in saved_queue:
                    if t.get('status') == 'downloading': t['status'] = 'paused'
                self.queue = saved_queue
                self.refresh_ui_list()
            except: pass
                
    def manual_save(self):
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, 'w') as f: json.dump(self.queue, f)
            
    def refresh_ui_list(self):
        self.ui.list_widget.clear(); self.task_widgets.clear()
        
        count_downloading = 0
        count_waiting = 0
        count_paused = 0
        count_finished = 0
        count_error = 0
        
        # 先计算每个任务的实时状态
        status_map = {}
        for t in self.queue:
            status = t.get('status', 'waiting')
            if t['url'] in self.active_workers:
                if getattr(self.active_workers[t['url']], 'is_paused', False):
                    status = 'paused'
                else:
                    status = 'downloading'
            status_map[t['url']] = status
        
        # 排序优先级：downloading > waiting > paused > error > finished
        priority = {'downloading': 0, 'waiting': 1, 'paused': 2, 'error': 3, 'finished': 4}
        sorted_queue = sorted(self.queue, key=lambda t: priority.get(status_map.get(t['url'], 'waiting'), 99))
        
        for t in sorted_queue:
            item = QListWidgetItem(self.ui.list_widget); item.setSizeHint(QSize(0, 95)); item.setData(Qt.UserRole, t['url'])
            
            status = status_map[t['url']]
            
            if status == 'downloading': count_downloading += 1
            elif status == 'finished': count_finished += 1
            elif status == 'paused': count_paused += 1
            elif status == 'error': count_error += 1
            else: count_waiting += 1
            
            widget = TaskWidget(
                t['title'], t.get('thumb', ''), t['url'], status,
                t.get('progress', 0), t.get('speed', ''), t.get('eta', '')
            )
            widget.action_clicked.connect(self.handle_task_action)
            self.ui.list_widget.setItemWidget(item, widget); self.task_widgets[t['url']] = widget
        
        shutdown_indicator = " | 📴 [自动关机: 开启]" if self.config.get("shutdown_enabled") else ""

        status_text = f"🚀 下载中: {count_downloading} | ⏳ 排队: {count_waiting} | ✅ 已完成: {count_finished}"
        
        if count_paused > 0:
            status_text += f" | ⏸️ 暂停: {count_paused}"
        if count_error > 0:
            status_text += f" | ❌ 异常: {count_error}"

        if self.is_queue_running or count_downloading > 0:
            self.ui.status_label.setText(f"{status_text}{shutdown_indicator}")
            self.ui.status_label.setStyleSheet("color: #3498db; font-weight: bold; font-size: 13px; border: none;")
        else:
            self.ui.status_label.setText(f"💤 引擎待命 | {status_text}{shutdown_indicator}")
            self.ui.status_label.setStyleSheet("color: #2ecc71; font-weight: bold; font-size: 13px; border: none;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_DontShowIconsInMenus, True) 
    window = MainWindow(); window.show()
    sys.exit(app.exec())