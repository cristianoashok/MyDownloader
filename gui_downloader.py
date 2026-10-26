"""MyDownloader - IDM-style download manager window (PySide6).

Run:  python gui_downloader.py
Needs: pip install PySide6 yt-dlp requests
The download engine is in engine.py and the download list / Chrome listener in manager.py.
"""
import os
import sys
import subprocess

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QDateTime, QSize, QRectF, QByteArray, QFileInfo
from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QAction, QGuiApplication, QPalette,
                           QFont, QKeySequence)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QToolBar, QSplitter, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit, QLabel,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLineEdit, QPushButton, QComboBox,
    QCheckBox, QSpinBox, QGroupBox, QFileDialog, QMessageBox, QMenu, QSystemTrayIcon,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QFileIconProvider, QDialogButtonBox,
    QFrame, QSizePolicy, QProgressBar,
)

from engine import (human_size, human_time, find_aria2c, find_ffmpeg, find_deno, CATEGORIES, categorize,
                    app_folders)
from manager import DownloadManager, EXTENSION_PORT

APP_NAME = "MyDownloader"
ACCENT = "#1f6feb"

# ============================================================
# ICONS (small inline SVGs, so the app needs no image files)
# ============================================================
_SVG = {
    'add': '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    'resume': '<path d="M8 5.5v13l10-6.5z" fill="{c}"/>',
    'stop': '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="{c}"/>',
    'stopall': '<rect x="3" y="7" width="8" height="10" rx="1.2" fill="{c}"/>'
               '<rect x="13" y="7" width="8" height="10" rx="1.2" fill="{c}"/>',
    'delete': '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v6M14 11v6"/>',
    'clean': '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/><path d="M9.5 13.5l2 2 3.5-4"/>',
    'folder': '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1'
              '-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/>',
    'options': '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/>'
               '<circle cx="10" cy="17" r="2"/>',
    'all': '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
    'file': '<path d="M6.5 3h7.5l4.5 4.5V20a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" fill="{c}" '
            'fill-opacity=".14"/><path d="M14 3v4.5h4.5"/>',
    'unfinished': '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    'finished': '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.3l2.4 2.4 4.6-5"/>',
}


def svg_icon(name, color=None, size=24):
    color = color or T['icon']
    body = _SVG[name].replace('{c}', color)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    icon = QIcon()
    for s in (size, size * 2):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        icon.addPixmap(pm)
    return icon


_app_icon = None


def app_icon():
    """The app icon from icon.ico / icon.png next to the app; a drawn fallback if missing."""
    global _app_icon
    if _app_icon is not None:
        return _app_icon
    for folder in app_folders():
        for name in ('icon.ico', 'icon.png'):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                icon = QIcon(p)
                if not icon.isNull():
                    _app_icon = icon
                    return icon
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           f'<rect x="4" y="4" width="56" height="56" rx="14" fill="{ACCENT}"/>'
           '<path d="M32 15v24M21 29l11 11 11-11M19 48h26" fill="none" stroke="#fff" stroke-width="6" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        icon.addPixmap(pm)
    _app_icon = icon
    return icon


_icon_provider = None
_CAT_COLORS = {'Video': '#7c4dff', 'Music': '#d81b60', 'Programs': '#1f6feb',
               'Compressed': '#d98a00', 'Documents': '#c62828', 'General': '#607080'}
_cat_icons = {}


def file_icon(path):
    """The real Windows icon once the file exists; a colored icon per category until then."""
    global _icon_provider
    if os.path.isfile(path):
        if _icon_provider is None:
            _icon_provider = QFileIconProvider()
        icon = _icon_provider.icon(QFileInfo(path))
        if not icon.isNull() and icon.availableSizes():
            return icon
    cat = categorize(path)
    if cat not in _cat_icons:
        _cat_icons[cat] = svg_icon('file', _CAT_COLORS.get(cat, '#607080'))
    return _cat_icons[cat]


def open_path(path):
    """Open a file or folder with the system default app."""
    if not os.path.exists(path):
        return False
    if sys.platform.startswith('win'):
        os.startfile(path)                                   # noqa: only exists on Windows
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])
    return True


def show_in_folder(path):
    """Open the folder and select the file (Windows), or just open the folder."""
    if sys.platform.startswith('win') and os.path.exists(path):
        subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
    else:
        open_path(os.path.dirname(path) if os.path.isfile(path) else path)


# ============================================================
# THREAD BRIDGE: manager callbacks arrive on worker threads; signals move them to the UI thread
# ============================================================
class Bridge(QObject):
    log = Signal(str)
    added = Signal(object)
    removed = Signal(str)
    ask = Signal(object)
    busy = Signal(str)


# ============================================================
# TABLE PIECES
# ============================================================
COLS = ["File Name", "Size", "Status", "Time Left", "Transfer Rate", "Date Added"]
C_NAME, C_SIZE, C_STATUS, C_LEFT, C_RATE, C_DATE = range(6)
ROLE_ID = Qt.UserRole
ROLE_SORT = Qt.UserRole + 1
ROLE_PROGRESS = Qt.UserRole + 2
ROLE_STATE = Qt.UserRole + 3


class SortItem(QTableWidgetItem):
    """Sorts by a hidden number (bytes, seconds, date) instead of the text shown."""
    def __lt__(self, other):
        a, b = self.data(ROLE_SORT), other.data(ROLE_SORT)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


class StatusDelegate(QStyledItemDelegate):
    """Draws a progress bar in the Status column, like IDM."""

    def paint(self, painter, option, index):
        state = index.data(ROLE_STATE)
        if state not in ('Downloading', 'Paused'):
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ''
        QApplication.style().drawControl(QStyle.CE_ItemViewItem, opt, painter)

        pct = max(0, min(100, int(index.data(ROLE_PROGRESS) or 0)))
        r = QRectF(option.rect).adjusted(6, 5, -6, -5)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(T['track']))
        painter.drawRoundedRect(r, 3, 3)
        if pct:
            fill = QRectF(r)
            fill.setWidth(r.width() * pct / 100)
            painter.setBrush(QColor(T['accent'] if state == 'Downloading' else T['paused']))
            painter.drawRoundedRect(fill, 3, 3)
        painter.setPen(QColor('#ffffff') if pct >= 50 else QColor(T['text']))
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 0.5))
        painter.setFont(f)
        painter.drawText(r, Qt.AlignCenter, index.data(Qt.DisplayRole) or '')
        painter.restore()


# ============================================================
# DIALOGS
# ============================================================
class AddUrlDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add URL")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("Address (file link or video page):"))
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://")
        clip = QGuiApplication.clipboard().text().strip()
        if clip.startswith(('http://', 'https://')) and '\n' not in clip:
            self.url.setText(clip)
            self.url.selectAll()
        lay.addWidget(self.url)

        self.adv = QGroupBox("Use referer and cookies (for links that need a login)")
        self.adv.setCheckable(True)
        self.adv.setChecked(False)
        form = QFormLayout(self.adv)
        self.referer = QLineEdit()
        self.referer.setPlaceholderText("Page the link came from (optional)")
        self.cookie = QLineEdit()
        self.cookie.setPlaceholderText("name=value; name2=value2 (optional)")
        form.addRow("Referer:", self.referer)
        form.addRow("Cookie:", self.cookie)
        lay.addWidget(self.adv)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("OK")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        if not self.url.text().strip().startswith(('http://', 'https://')):
            QMessageBox.warning(self, "Add URL", "Please enter a link that starts with http:// or https://")
            return
        self.accept()

    def values(self):
        extra = {}
        if self.adv.isChecked():
            if self.referer.text().strip():
                extra['referer'] = self.referer.text().strip()
            if self.cookie.text().strip():
                extra['cookie'] = self.cookie.text().strip()
        return self.url.text().strip(), extra


class DownloadInfoDialog(QDialog):
    """IDM's 'Download File Info': confirm name, folder and category before starting."""
    def __init__(self, pending, mgr, on_done):
        super().__init__(None)                      # top-level, so it shows even when the app is in the tray
        self.pending, self.mgr, self.on_done = pending, mgr, on_done
        self.setWindowTitle("Download File Info")
        self.setWindowIcon(app_icon())
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumWidth(600)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(file_icon(pending.filename).pixmap(44, 44))
        icon.setContentsMargins(0, 2, 8, 0)
        icon.setAlignment(Qt.AlignTop)
        top.addWidget(icon)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        grid.addWidget(QLabel("URL:"), 0, 0)
        url = QLineEdit(pending.url)
        url.setReadOnly(True)
        url.setCursorPosition(0)
        grid.addWidget(url, 0, 1, 1, 2)

        grid.addWidget(QLabel("Category:"), 1, 0)
        self.category = QComboBox()
        self.category.addItems(['General'] + list(CATEGORIES))
        self.category.setCurrentText(categorize(pending.filename))
        self.category.currentTextChanged.connect(self._category_changed)
        grid.addWidget(self.category, 1, 1)

        grid.addWidget(QLabel("Save As:"), 2, 0)
        start_path = mgr.unique_path(os.path.join(mgr.folder_for(pending.filename), pending.filename))
        self.path = QLineEdit(start_path)
        grid.addWidget(self.path, 2, 1)
        browse = QPushButton("Browse…")
        browse.setToolTip("Choose folder and file name")
        browse.clicked.connect(self._browse)
        grid.addWidget(browse, 2, 2)

        self.details = QLabel()
        self.details.setObjectName("muted")
        grid.addWidget(self.details, 3, 1, 1, 2)
        top.addLayout(grid, 1)
        lay.addLayout(top)

        # The download is already running quietly in the background (pre-download);
        # the dialog only keeps the size line up to date once the server reports it.
        self.confirmed = False
        self._update_details()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_details)
        self.timer.start(1000)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("rule")
        lay.addWidget(line)

        btns = QHBoxLayout()
        later = QPushButton("Download Later")
        later.clicked.connect(lambda: self._finish(start=False))
        start = QPushButton("Start Download")
        start.setDefault(True)
        start.setObjectName("primary")
        start.clicked.connect(lambda: self._finish(start=True))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(later)
        btns.addStretch(1)
        btns.addWidget(start)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _update_details(self):
        p, t = self.pending, self.pending.task
        size = (t.total_size if t else 0) or p.size
        info = []
        if p.quality:
            info.append(f"Quality: {p.quality}")
        info.append(f"Size: {human_size(size) or 'Unknown'}")
        if p.source:
            info.append(f"From: {p.source}")
        self.details.setText("     ".join(info))

    def reject(self):
        if not self.confirmed:
            self.mgr.discard_pending(self.pending)     # Cancel / close: stop and delete the partial file
        self.timer.stop()
        super().reject()

    def _category_changed(self, cat):
        name = os.path.basename(self.path.text().strip()) or self.pending.filename
        self.path.setText(os.path.join(self.mgr.folder_for(name, cat), name))

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save As", self.path.text().strip())
        if path:
            self.path.setText(os.path.normpath(path))

    def _finish(self, start):
        path = self.path.text().strip()
        if not path or not os.path.basename(path):
            QMessageBox.warning(self, "Save As", "Please choose where to save the file.")
            return
        path = self.mgr.unique_path(os.path.abspath(os.path.expanduser(path)))
        self.confirmed = True
        self.timer.stop()
        self.on_done(self.pending, path, start)
        self.accept()

    def showEvent(self, e):
        super().showEvent(e)
        self.raise_()
        self.activateWindow()


class OptionsDialog(QDialog):
    def __init__(self, mgr, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        s = mgr.settings
        self.setWindowTitle("Options")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)

        g1 = QGroupBox("Saving files")
        f1 = QFormLayout(g1)
        row = QHBoxLayout()
        self.save_dir = QLineEdit(s['save_dir'])
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(self.save_dir, 1)
        row.addWidget(b)
        f1.addRow("Default folder:", row)
        self.use_cat = QCheckBox("Sort into category folders (Video, Music, Programs, …)")
        self.use_cat.setChecked(s['use_categories'])
        f1.addRow("", self.use_cat)
        self.ask = QCheckBox("Show the Download File Info dialog before starting")
        self.ask.setChecked(s['ask_before_download'])
        f1.addRow("", self.ask)
        self.pre = QCheckBox("Start downloading while that dialog is open (like IDM)")
        self.pre.setChecked(s.get('predownload', True))
        self.pre.setEnabled(self.ask.isChecked())
        self.ask.toggled.connect(self.pre.setEnabled)
        f1.addRow("", self.pre)
        lay.addWidget(g1)

        g2 = QGroupBox("Connection")
        f2 = QFormLayout(g2)
        self.conns = QSpinBox()
        self.conns.setRange(1, 16)
        self.conns.setValue(int(s['connections']))
        f2.addRow("Connections per download:", self.conns)
        self.limit = QSpinBox()
        self.limit.setRange(0, 1_000_000)
        self.limit.setSingleStep(100)
        self.limit.setSuffix(" KB/s")
        self.limit.setSpecialValueText("Unlimited")
        self.limit.setValue(int(s['speed_limit_kb']))
        f2.addRow("Speed limit:", self.limit)
        self.cookies = QComboBox()
        self.cookies.addItems(["None", "Chrome", "Edge", "Firefox", "Brave", "Opera"])
        self.cookies.setCurrentText(s.get('cookie_browser', 'None'))
        self.cookies.setToolTip("Only for links added by hand. Downloads from the Chrome extension "
                                "already include cookies.")
        f2.addRow("Cookies from browser:", self.cookies)
        lay.addWidget(g2)

        ga = QGroupBox("Appearance")
        fa = QFormLayout(ga)
        self.theme = QComboBox()
        self.theme.addItem("System default (follows Windows)", 'system')
        self.theme.addItem("Light", 'light')
        self.theme.addItem("Dark", 'dark')
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.get('theme', 'system'))))
        fa.addRow("Theme:", self.theme)
        lay.addWidget(ga)

        g3 = QGroupBox("Behavior")
        f3 = QVBoxLayout(g3)
        self.tray = QCheckBox("Keep running in the system tray when the window is closed")
        self.tray.setChecked(s['close_to_tray'])
        self.notify = QCheckBox("Notify me when a download finishes")
        self.notify.setChecked(s['notify_complete'])
        self.logging = QCheckBox("Activity log (for troubleshooting): record and show it in the window")
        self.logging.setChecked(s.get('logging', False))
        f3.addWidget(self.tray)
        f3.addWidget(self.notify)
        f3.addWidget(self.logging)
        lay.addWidget(g3)

        g4 = QGroupBox("Status")
        f4 = QFormLayout(g4)
        f4.addRow("Chrome extension:", QLabel(
            f"Listening on 127.0.0.1:{EXTENSION_PORT}" if mgr.server_ok else "Not listening (port in use?)"))
        f4.addRow("aria2c:", QLabel(find_aria2c() or "Not found (place aria2c.exe next to the app)"))
        f4.addRow("ffmpeg:", QLabel(find_ffmpeg() or "Not found (place ffmpeg.exe next to the app)"))
        f4.addRow("Deno (YouTube):", QLabel(find_deno() or "Not found (place deno.exe next to the app)"))
        lay.addWidget(g4)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Default download folder", self.save_dir.text())
        if d:
            self.save_dir.setText(os.path.normpath(d))

    def _save(self):
        self.mgr.save_settings({
            'save_dir': self.save_dir.text().strip() or self.mgr.settings['save_dir'],
            'use_categories': self.use_cat.isChecked(),
            'ask_before_download': self.ask.isChecked(),
            'connections': self.conns.value(),
            'speed_limit_kb': self.limit.value(),
            'cookie_browser': self.cookies.currentText(),
            'close_to_tray': self.tray.isChecked(),
            'notify_complete': self.notify.isChecked(),
            'logging': self.logging.isChecked(),
            'predownload': self.pre.isChecked(),
            'theme': self.theme.currentData(),
        })
        apply_theme(self.theme.currentData(), self.parent())
        self.accept()


# ============================================================
# MAIN WINDOW
# ============================================================
class MainWindow(QMainWindow):
    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr
        self.rows = {}               # task_id -> [items per column]
        self.last_status = {}        # task_id -> status, to spot finished downloads
        self.open_dialogs = []
        self.quitting = False
        self.tray_hint_shown = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1180, 680)

        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        self._build_body()
        self._build_statusbar()
        self._build_tray()

        # Manager -> window (through signals, because callbacks come from worker threads)
        self.bridge = Bridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.added.connect(self._add_row)
        self.bridge.removed.connect(self._remove_row)
        self.bridge.ask.connect(self._ask)
        self.bridge.busy.connect(self._busy)
        mgr.on_log = self.bridge.log.emit
        mgr.on_task_added = self.bridge.added.emit
        mgr.on_task_removed = self.bridge.removed.emit
        mgr.on_ask = self.bridge.ask.emit
        mgr.on_busy = self.bridge.busy.emit

        for task in mgr.tasks.values():
            self._add_row(task)
            self.last_status[task.task_id] = task.status

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self._update_actions()

    # ---------------- Building the window ----------------
    def _build_actions(self):
        def act(text, icon, slot, shortcut=None, tip=None):
            a = QAction(svg_icon(icon), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            return a
        self.a_add = act("Add URL", 'add', self.add_url, "Ctrl+N", "Add a download link (Ctrl+N)")
        self.a_resume = act("Resume", 'resume', self.resume_selected, None, "Resume selected downloads")
        self.a_stop = act("Stop", 'stop', self.stop_selected, None, "Stop selected downloads")
        self.a_stopall = act("Stop All", 'stopall', self.mgr.pause_all, None, "Stop all downloads")
        self.a_delete = act("Delete", 'delete', self.delete_selected, "Del", "Delete selected downloads")
        self.a_clean = act("Delete Completed", 'clean', self.delete_completed, None,
                           "Remove finished downloads from the list (asks whether to keep the files)")
        self.a_folder = act("Open Folder", 'folder', self.open_folder, None, "Open the download folder")
        self.a_options = act("Options", 'options', self.show_options, None, "Settings")
        self._paint_action_icons()

    def _paint_action_icons(self):
        for a, name in ((self.a_add, 'add'), (self.a_delete, 'delete'), (self.a_clean, 'clean'),
                        (self.a_folder, 'folder'), (self.a_options, 'options')):
            a.setIcon(svg_icon(name))
        self.a_resume.setIcon(svg_icon('resume', '#1f9d55' if T.get('mode') != 'dark' else '#4cc38a'))
        self.a_stop.setIcon(svg_icon('stop', '#d64545' if T.get('mode') != 'dark' else '#ff7070'))
        self.a_stopall.setIcon(svg_icon('stopall', '#d64545' if T.get('mode') != 'dark' else '#ff7070'))

    def theme_changed(self):
        """Called after switching light/dark: repaint icons and rows that draw their own colors."""
        self._paint_action_icons()
        self.sidebar.topLevelItem(0).setIcon(0, svg_icon('all', T['accent']))
        for i in range(self.sidebar.topLevelItem(0).childCount()):
            self.sidebar.topLevelItem(0).child(i).setIcon(0, svg_icon('folder', T['folder']))
        self.refresh()

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(26, 26))
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        for a in (self.a_add, self.a_resume, self.a_stop, self.a_stopall):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.a_delete, self.a_clean):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.a_folder, self.a_options):
            tb.addAction(a)
        self.addToolBar(tb)

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&Tasks")
        m.addAction(self.a_add)
        m.addSeparator()
        exit_a = QAction("Exit", self)
        exit_a.triggered.connect(self.quit_app)
        m.addAction(exit_a)
        m = mb.addMenu("&Downloads")
        for a in (self.a_resume, self.a_stop, self.a_stopall, self.a_delete, self.a_clean):
            m.addAction(a)
        m = mb.addMenu("&View")
        self.a_log = QAction("Activity Log", self, checkable=True,
                             checked=bool(self.mgr.settings.get('logging', False)))
        self.a_log.toggled.connect(self.set_logging)
        m.addAction(self.a_log)
        m.addAction(self.a_options)

    def _build_body(self):
        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderHidden(True)
        self.sidebar.setIndentation(14)
        self.sidebar.setMinimumWidth(170)
        self.sidebar.setMaximumWidth(240)
        all_item = QTreeWidgetItem(["All Downloads"])
        all_item.setIcon(0, svg_icon('all', T['accent']))
        all_item.setData(0, ROLE_ID, ('all', None))
        for cat in ['General'] + list(CATEGORIES):
            child = QTreeWidgetItem([cat])
            child.setIcon(0, svg_icon('folder', T['folder']))
            child.setData(0, ROLE_ID, ('cat', cat))
            all_item.addChild(child)
        unfinished = QTreeWidgetItem(["Unfinished"])
        unfinished.setIcon(0, svg_icon('unfinished', '#d97706'))
        unfinished.setData(0, ROLE_ID, ('unfinished', None))
        finished = QTreeWidgetItem(["Finished"])
        finished.setIcon(0, svg_icon('finished', '#1f9d55'))
        finished.setData(0, ROLE_ID, ('finished', None))
        self.sidebar.addTopLevelItems([all_item, unfinished, finished])
        all_item.setExpanded(True)
        self.sidebar.setCurrentItem(all_item)
        self.sidebar.currentItemChanged.connect(lambda *_: self._apply_filter())

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setIconSize(QSize(18, 18))
        self.table.setItemDelegateForColumn(C_STATUS, StatusDelegate(self.table))
        # Every column can be resized by dragging the line between headers; widths are remembered
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(True)
        h.setMinimumSectionSize(50)
        widths = self.mgr.settings.get('column_widths') or []
        defaults = [340, 95, 150, 100, 110, 165]
        for col, w in enumerate(defaults):
            ok = col < len(widths) and isinstance(widths[col], int) and widths[col] >= 50
            self.table.setColumnWidth(col, widths[col] if ok else w)
        self.table.setTextElideMode(Qt.ElideMiddle)        # "Anonymity - Ma…(1080p).mp4"
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        h.sectionDoubleClicked.connect(lambda col: self.table.resizeColumnToContents(col))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(C_DATE, Qt.DescendingOrder)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.cellDoubleClicked.connect(self._double_click)

        self.empty_hint = QLabel("No downloads yet.\nClick Add URL, or download something in Chrome.",
                                 self.table.viewport())
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setObjectName("hint")

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(2000)
        self.log_box.setObjectName("log")

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.table)
        right.addWidget(self.log_box)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        right.setSizes([480, 140])
        self.log_box.setVisible(bool(self.mgr.settings.get('logging', False)))

        main = QSplitter(Qt.Horizontal)
        main.addWidget(self.sidebar)
        main.addWidget(right)
        main.setStretchFactor(1, 1)
        main.setSizes([190, 990])
        self.setCentralWidget(main)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.busy_label = QLabel("")
        self.speed_label = QLabel("")
        self.speed_label.setMinimumWidth(230)
        self.speed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.ext_label = QLabel("")
        self._update_status_label()
        sb.addWidget(self.busy_label, 1)
        sb.addPermanentWidget(self.speed_label)
        sb.addPermanentWidget(self.ext_label)

    def _update_status_label(self):
        tools = ["aria2c ✓" if find_aria2c() else "aria2c ✗",
                 "ffmpeg ✓" if find_ffmpeg() else "ffmpeg ✗",
                 "deno ✓" if find_deno() else "deno ✗"]
        ext = "Chrome extension: connected" if self.mgr.server_ok else "Chrome extension: not listening"
        self.ext_label.setText(ext + "   ·   " + "   ".join(tools))

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        show = menu.addAction("Show MyDownloader")
        show.triggered.connect(self.show_window)
        menu.addAction(self.a_add)
        menu.addAction(self.a_stopall)
        menu.addSeparator()
        quit_a = menu.addAction("Exit")
        quit_a.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_window() if reason in (QSystemTrayIcon.Trigger,
                                                           QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    # ---------------- Rows ----------------
    def _add_row(self, task):
        if task.task_id in self.rows:
            return
        self.table.setSortingEnabled(False)
        row = self.table.rowCount()
        self.table.insertRow(row)
        items = [SortItem() for _ in COLS]
        items[C_NAME].setData(ROLE_ID, task.task_id)
        items[C_NAME].setIcon(file_icon(task.display_path))
        for col in (C_SIZE, C_LEFT, C_RATE):
            items[col].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for col, it in enumerate(items):
            self.table.setItem(row, col, it)
        self.rows[task.task_id] = items
        self._fill_row(task, items)
        self.table.setSortingEnabled(True)
        self._apply_filter()

    def _remove_row(self, task_id):
        items = self.rows.pop(task_id, None)
        self.last_status.pop(task_id, None)
        if items:
            self.table.removeRow(items[0].row())
        self._apply_filter()

    def _fill_row(self, task, items):
        st = task.status
        items[C_NAME].setText(task.display_name)
        items[C_NAME].setData(ROLE_SORT, task.display_name.lower())
        items[C_NAME].setToolTip(f"{task.display_path}\n{task.url}")

        items[C_SIZE].setText(human_size(task.total_size))
        items[C_SIZE].setData(ROLE_SORT, task.total_size or 0)

        s = items[C_STATUS]
        s.setData(ROLE_STATE, st)
        s.setData(ROLE_PROGRESS, task.current_progress)
        if st == 'Downloading':
            text = f"{task.current_progress}%"
        elif st == 'Paused':
            text = f"Paused · {task.current_progress}%" if task.current_progress else "Paused"
        elif st == 'Completed':
            text = "Complete"
        elif st == 'Error' and task.retryable:
            text = "Waiting for network"
        else:
            text = st
        s.setText(text)
        s.setData(ROLE_SORT, {'Downloading': 0, 'Queued': 1, 'Paused': 2, 'Error': 3,
                              'Completed': 4}.get(st, 5) * 1000 - task.current_progress)
        color = {'Error': T['err'], 'Completed': T['ok']}.get(st, T['text'])
        if st == 'Error' and task.retryable:
            color = T['muted']
        s.setForeground(QColor(color))
        s.setToolTip(task.error if st == 'Error' and task.error else "")

        active = st == 'Downloading'
        items[C_LEFT].setText(human_time(task.eta) if active else '')
        items[C_LEFT].setData(ROLE_SORT, task.eta if active and task.eta is not None else 10 ** 9)
        items[C_RATE].setText(f"{human_size(task.speed)}/s" if active and task.speed else '')
        items[C_RATE].setData(ROLE_SORT, task.speed if active else 0)

        dt = QDateTime.fromSecsSinceEpoch(int(task.date_added))
        items[C_DATE].setText(dt.toString("MMM d, yyyy  h:mm AP"))
        items[C_DATE].setData(ROLE_SORT, task.date_added)

    def refresh(self):
        """Update every row twice a second (cheaper and smoother than a signal per byte)."""
        self.table.setSortingEnabled(False)
        total_speed = 0
        active = 0
        for tid, items in list(self.rows.items()):
            task = self.mgr.tasks.get(tid)
            if not task:
                continue
            self._fill_row(task, items)
            if task.status == 'Downloading':
                total_speed += task.speed or 0
                active += 1
            prev = self.last_status.get(tid)
            if prev != task.status:
                self.last_status[tid] = task.status
                if prev == 'Downloading':
                    self._finished_notice(task)
                if task.status == 'Completed':
                    items[C_NAME].setIcon(file_icon(task.display_path))
        self.table.setSortingEnabled(True)
        speed = f" · {human_size(total_speed)}/s" if total_speed > 0 else ""
        self.speed_label.setText(f"{active} downloading{speed}   " if active else "")
        if self.tray:
            self.tray.setToolTip(f"{APP_NAME}\n" + (f"{active} downloading{speed}" if active else "Idle"))
        self._apply_filter()
        self._update_actions()

    def _finished_notice(self, task):
        if not self.tray or not self.mgr.settings.get('notify_complete', True):
            return
        if task.status == 'Completed':
            self.tray.showMessage("Download complete", task.display_name, QSystemTrayIcon.Information, 4000)
        elif task.status == 'Error' and not task.retryable:
            self.tray.showMessage("Download failed", f"{task.display_name}\n{task.error[:120]}",
                                  QSystemTrayIcon.Warning, 6000)

    def _apply_filter(self):
        cur = self.sidebar.currentItem()
        mode, cat = cur.data(0, ROLE_ID) if cur else ('all', None)
        visible = 0
        for tid, items in self.rows.items():
            task = self.mgr.tasks.get(tid)
            if not task:
                continue
            show = (mode == 'all'
                    or (mode == 'cat' and task.category == cat)
                    or (mode == 'unfinished' and task.status != 'Completed')
                    or (mode == 'finished' and task.status == 'Completed'))
            self.table.setRowHidden(items[0].row(), not show)
            visible += show
        self.empty_hint.setVisible(visible == 0)
        self.empty_hint.resize(self.table.viewport().size())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty_hint.resize(self.table.viewport().size())

    # ---------------- Selection helpers ----------------
    def selected_tasks(self):
        ids = []
        for idx in self.table.selectionModel().selectedRows(C_NAME):
            tid = self.table.item(idx.row(), C_NAME).data(ROLE_ID)
            if tid in self.mgr.tasks:
                ids.append(self.mgr.tasks[tid])
        return ids

    def _update_actions(self):
        sel = self.selected_tasks()
        self.a_resume.setEnabled(any(t.status in ('Paused', 'Queued', 'Error') for t in sel))
        self.a_stop.setEnabled(any(t.status in ('Downloading', 'Queued') for t in sel))
        self.a_delete.setEnabled(bool(sel))
        self.a_stopall.setEnabled(any(t.status == 'Downloading' for t in self.mgr.tasks.values()))
        self.a_clean.setEnabled(any(t.status == 'Completed' for t in self.mgr.tasks.values()))

    # ---------------- Actions ----------------
    def add_url(self):
        self.show_window()
        dlg = AddUrlDialog(self)
        if dlg.exec() == QDialog.Accepted:
            url, extra = dlg.values()
            self.mgr.log(f"[ADD URL] {url}")
            self.mgr.submit(url, extra, source='Add URL')

    def resume_selected(self):
        for t in self.selected_tasks():
            t.resume()
        self.refresh()

    def stop_selected(self):
        for t in self.selected_tasks():
            t.pause()
        self.refresh()

    def ask_delete(self, tasks, title="Delete"):
        """Two choices, like IDM: remove only from the list, or also delete the file from disk.
        Returns 'list', 'file' or None (cancelled)."""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Question)
        if len(tasks) == 1:
            box.setText(f"What do you want to do with “{tasks[0].display_name}”?")
        else:
            box.setText(f"What do you want to do with these {len(tasks)} downloads?")
        box.setInformativeText("Remove from List keeps the file on your disk.\n"
                               "Delete File Too also deletes the downloaded file (and any partial data).")
        b_file = box.addButton("Delete File Too", QMessageBox.DestructiveRole)
        b_file.setStyleSheet(f"color: {T['err']}; font-weight: 600;")
        b_list = box.addButton("Remove from List", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(b_list)
        box.exec()
        clicked = box.clickedButton()
        return 'file' if clicked is b_file else 'list' if clicked is b_list else None

    def _delete_tasks(self, tasks, choice):
        failed = []
        for t in tasks:
            failed += self.mgr.delete(t.task_id, delete_file=(choice == 'file'))
        if failed:
            QMessageBox.warning(self, "Delete", "These files could not be deleted (they may be open in "
                                "another program):\n\n" + "\n".join(failed[:10]))

    def delete_selected(self):
        tasks = self.selected_tasks()
        if tasks:
            choice = self.ask_delete(tasks)
            if choice:
                self._delete_tasks(tasks, choice)

    def delete_completed(self):
        done = [t for t in self.mgr.tasks.values() if t.status == 'Completed']
        if done:
            choice = self.ask_delete(done, "Delete Completed")
            if choice:
                self._delete_tasks(done, choice)

    def open_folder(self):
        sel = self.selected_tasks()
        if sel:
            t = sel[0]
            show_in_folder(t.display_path if os.path.exists(t.display_path) else os.path.dirname(t.display_path))
        else:
            os.makedirs(self.mgr.settings['save_dir'], exist_ok=True)
            open_path(self.mgr.settings['save_dir'])

    def show_options(self):
        if OptionsDialog(self.mgr, self).exec() == QDialog.Accepted:
            on = bool(self.mgr.settings.get('logging'))
            self.log_box.setVisible(on)
            self.a_log.blockSignals(True)
            self.a_log.setChecked(on)
            self.a_log.blockSignals(False)

    def set_logging(self, on):
        """Turn the activity log on/off: when off nothing is recorded and the panel is hidden."""
        self.mgr.save_settings({'logging': bool(on)})
        self.log_box.setVisible(bool(on))
        if not on:
            self.log_box.clear()
        else:
            self.mgr.log("[LOG] Activity log turned on")

    def _double_click(self, row, _col):
        tid = self.table.item(row, C_NAME).data(ROLE_ID)
        task = self.mgr.tasks.get(tid)
        if not task:
            return
        if task.status == 'Completed':
            if not open_path(task.output_path):
                QMessageBox.information(self, "Open", "The file was moved or deleted.")
        elif task.status in ('Paused', 'Queued', 'Error'):
            task.resume()
        elif task.status == 'Downloading':
            task.pause()

    def _context_menu(self, pos):
        tasks = self.selected_tasks()
        if not tasks:
            return
        t = tasks[0]
        m = QMenu(self)
        if t.status == 'Completed':
            m.addAction("Open", lambda: open_path(t.output_path))
        m.addAction(svg_icon('folder'), "Open Folder",
                    lambda: show_in_folder(t.display_path if os.path.exists(t.display_path)
                                           else os.path.dirname(t.display_path)))
        m.addSeparator()
        if any(x.status in ('Paused', 'Queued', 'Error') for x in tasks):
            m.addAction(self.a_resume)
        if any(x.status in ('Downloading', 'Queued') for x in tasks):
            m.addAction(self.a_stop)
        m.addSeparator()
        m.addAction("Copy Link", lambda: QGuiApplication.clipboard().setText(t.url))
        if t.error:
            m.addAction("Show Error", lambda: QMessageBox.warning(self, "Download error", t.error))
        m.addSeparator()
        m.addAction(self.a_delete)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---------------- Manager events ----------------
    def _append_log(self, line):
        self.log_box.appendPlainText(line)

    def _busy(self, text):
        self.busy_label.setText(text)

    def _ask(self, pending):
        dlg = DownloadInfoDialog(pending, self.mgr, self._pending_done)
        self.open_dialogs.append(dlg)
        dlg.finished.connect(lambda _r, d=dlg: self.open_dialogs.remove(d) if d in self.open_dialogs else None)
        dlg.show()

    def _pending_done(self, pending, path, start):
        self.mgr.add_pending(pending, path, start=start)

    # ---------------- Window / tray ----------------
    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if not self.quitting and self.tray and self.mgr.settings.get('close_to_tray', True):
            e.ignore()
            self.save_layout()
            self.hide()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Still running here, so Chrome downloads keep working. "
                                      "Right-click the icon to exit.", QSystemTrayIcon.Information, 4000)
            return
        self.save_layout()
        self.mgr.shutdown()
        e.accept()
        QApplication.quit()

    def save_layout(self):
        self.mgr.save_settings({'column_widths': [self.table.columnWidth(c) for c in range(len(COLS))]})

    def quit_app(self):
        self.quitting = True
        self.close()


# ============================================================
# THEMES (light / dark). T always holds the colors of the active theme.
# ============================================================
THEMES = {
    'light': dict(bg='#f5f7fa', surface='#ffffff', alt='#f8fafc', text='#1f2933', muted='#52606d',
                  faint='#8a96a3', border='#e1e6ec', border2='#cfd7e1', hover='#eef3fb', press='#dfe9f8',
                  sel='#d6e4fb', seltext='#10233f', navsel='#e3edfc', disabled='#a6b0bb', icon='#3b4a5e',
                  track='#e6ebf2', ok='#1f7a3d', err='#c62828', paused='#9aa4b2', accent=ACCENT,
                  accent_hover='#1a5fd0', readonly='#f5f7fa', folder='#d9a520'),
    'dark': dict(bg='#1b1d21', surface='#23262b', alt='#26292f', text='#e4e7eb', muted='#a1a9b4',
                 faint='#6f7883', border='#33373e', border2='#454b54', hover='#2c3138', press='#353b44',
                 sel='#27426c', seltext='#ffffff', navsel='#2a3a55', disabled='#5d6570', icon='#c9d1db',
                 track='#383d45', ok='#4cc38a', err='#ff7070', paused='#6f7883', accent='#3b82f6',
                 accent_hover='#4b8ff7', readonly='#1f2226', folder='#e0b13a'),
}
T = dict(THEMES['light'])


def build_style():
    c = T
    return f"""
QMainWindow, QDialog {{ background: {c['bg']}; color: {c['text']}; }}
QMenuBar {{ background: {c['surface']}; color: {c['text']}; }}
QMenuBar::item:selected {{ background: {c['navsel']}; }}
QLabel, QCheckBox, QRadioButton, QTreeWidget, QTableWidget {{ color: {c['text']}; }}
QLabel#muted {{ color: {c['muted']}; }}
QLabel#hint {{ color: {c['faint']}; font-size: 13px; }}
QFrame#rule {{ color: {c['border']}; }}
QToolBar {{ background: {c['surface']}; border: 0; border-bottom: 1px solid {c['border']}; padding: 4px 6px; spacing: 2px; }}
QToolBar QToolButton {{ color: {c['text']}; padding: 4px 8px; border-radius: 6px; min-width: 64px; }}
QToolBar QToolButton:hover {{ background: {c['hover']}; }}
QToolBar QToolButton:pressed {{ background: {c['press']}; }}
QToolBar QToolButton:disabled {{ color: {c['disabled']}; }}
QToolBar::separator {{ background: {c['border']}; width: 1px; margin: 6px 6px; }}
QTreeWidget {{ background: {c['surface']}; border: 0; border-right: 1px solid {c['border']}; padding-top: 6px; outline: 0; }}
QTreeWidget::item {{ padding: 5px 4px; border-radius: 4px; }}
QTreeWidget::item:selected {{ background: {c['navsel']}; color: {c['seltext']}; }}
QTableWidget {{ background: {c['surface']}; alternate-background-color: {c['alt']}; border: 0; outline: 0;
    selection-background-color: {c['sel']}; selection-color: {c['seltext']}; }}
QHeaderView::section {{ background: {c['bg']}; color: {c['muted']}; border: 0; border-bottom: 1px solid {c['border']};
    border-right: 1px solid {c['border']}; padding: 6px 8px; font-weight: 600; }}
QHeaderView::section:hover {{ background: {c['hover']}; }}
QTableCornerButton::section {{ background: {c['bg']}; border: 0; }}
QPlainTextEdit#log {{ background: #16171a; color: #a9dc76; border: 0; border-top: 1px solid {c['border']};
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }}
QStatusBar {{ background: {c['surface']}; border-top: 1px solid {c['border']}; color: {c['muted']}; }}
QStatusBar QLabel {{ padding: 0 6px; color: {c['muted']}; }}
QSplitter::handle {{ background: {c['border']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QGroupBox {{ font-weight: 600; border: 1px solid {c['border']}; border-radius: 8px; margin-top: 12px;
    padding: 12px 10px 8px 10px; background: {c['surface']}; color: {c['text']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['text']}; }}
QLineEdit {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border2']}; border-radius: 6px;
    padding: 5px 7px; min-height: 18px; selection-background-color: {c['accent']}; selection-color: #ffffff; }}
QLineEdit:focus {{ border-color: {c['accent']}; }}
QLineEdit[readOnly="true"] {{ background: {c['readonly']}; color: {c['muted']}; }}
QComboBox, QSpinBox {{ min-height: 24px; padding-left: 6px; }}
QPushButton {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border2']}; border-radius: 6px;
    padding: 6px 16px; }}
QPushButton:hover {{ background: {c['hover']}; }}
QPushButton#primary, QPushButton:default {{ background: {c['accent']}; color: #ffffff; border-color: {c['accent']};
    font-weight: 600; }}
QPushButton#primary:hover, QPushButton:default:hover {{ background: {c['accent_hover']}; }}
QPushButton#danger {{ color: {c['err']}; }}
QProgressBar {{ background: {c['track']}; border: 0; border-radius: 3px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}
QMenu {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border2']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 26px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['navsel']}; color: {c['seltext']}; }}
QMenu::item:disabled {{ color: {c['disabled']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}
QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border2']}; padding: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['border2']}; border-radius: 4px; min-height: 30px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['border2']}; border-radius: 4px; min-width: 30px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: 0; }}
"""


def build_palette():
    """A complete palette, so Windows' own light/dark setting can't leave white-on-white text."""
    c = T
    pal = QPalette()
    roles = {
        QPalette.Window: c['bg'], QPalette.WindowText: c['text'], QPalette.Base: c['surface'],
        QPalette.AlternateBase: c['alt'], QPalette.Text: c['text'], QPalette.Button: c['surface'],
        QPalette.ButtonText: c['text'], QPalette.BrightText: '#ffffff', QPalette.ToolTipBase: c['surface'],
        QPalette.ToolTipText: c['text'], QPalette.Highlight: c['sel'], QPalette.HighlightedText: c['seltext'],
        QPalette.PlaceholderText: c['faint'], QPalette.Link: c['accent'], QPalette.Light: c['surface'],
        QPalette.Midlight: c['hover'], QPalette.Mid: c['border2'], QPalette.Dark: c['paused'],
        QPalette.Shadow: '#000000',
    }
    for role, col in roles.items():
        pal.setColor(QPalette.All, role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(c['disabled']))
    return pal


def system_is_dark():
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:                      # Qt older than 6.5
        return False


def apply_theme(choice, window=None):
    """choice: 'system', 'light' or 'dark'. Also switches the Windows title bar color."""
    app = QApplication.instance()
    hints = QGuiApplication.styleHints()
    try:
        hints.setColorScheme({'light': Qt.ColorScheme.Light, 'dark': Qt.ColorScheme.Dark}
                             .get(choice, Qt.ColorScheme.Unknown))
    except AttributeError:                      # Qt older than 6.8
        pass
    mode = choice if choice in ('light', 'dark') else ('dark' if system_is_dark() else 'light')
    T.clear()
    T.update(THEMES[mode])
    T['mode'] = mode
    app.setPalette(build_palette())
    app.setStyleSheet(build_style())
    if window is not None:
        window.theme_changed()


def main():
    if sys.platform.startswith('win'):
        # Lets Windows show our icon on the taskbar instead of the Python one
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MyDownloader.App")
        except Exception:
            pass
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)          # keep running in the tray

    mgr = DownloadManager()
    apply_theme(mgr.settings.get('theme', 'system'))
    win = MainWindow(mgr)
    # Follow Windows when it switches between light and dark (theme = "System default")
    try:
        QGuiApplication.styleHints().colorSchemeChanged.connect(
            lambda *_: mgr.settings.get('theme', 'system') == 'system' and apply_theme('system', win))
    except AttributeError:
        pass
    mgr.start_extension_server()
    mgr.start_watchdog()
    win._update_status_label()
    if not mgr.server_ok:
        QMessageBox.warning(win, APP_NAME, f"Another copy of {APP_NAME} seems to be running "
                            f"(port {EXTENSION_PORT} is busy).\nThe Chrome extension will talk to that one.")
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
