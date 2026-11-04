"""Main window: Proton Drive file explorer."""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (QEvent, QLocale, QMimeDatabase, QModelIndex, QPoint, QRect,
                            QSettings, QSortFilterProxyModel, QStandardPaths, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import (QAction, QDesktopServices, QGuiApplication, QIcon, QKeySequence,
                           QPainter, QPalette, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSplitter,
                               QStackedWidget, QStyle, QToolBar, QToolButton, QTreeView,
                               QVBoxLayout, QWidget, QDockWidget)
from PySide6.QtCore import QAbstractTableModel, QSize
from PySide6.QtGui import QPixmap

from .. import config
from ..client import Client, DriveError, Entry, NotFound, NotLoggedIn
from . import theme
from .transfers import DONE, RUNNING, TransferManager, TransferPanel, resolve_conflicts
from .widgets import (ROW_H, ActivityBar, FlatHeader, IconLabel, RowDelegate, Tile, chip, confirm,
                      font_with, prompt)
from PySide6.QtCore import QThreadPool
from .cache import ListingCache
from .workers import run_async

TRASH = "/trash"
SECTIONS_KEY = "/:sections"   # cache key for `client.sections()`
DEFAULT_ROOT = config.REMOTE_ROOT
SECTION_LABELS = {"my-files": "My files", "shared-by-me": "Shared by me",
                  "shared-with-me": "Shared with me", "devices": "Other computers",
                  "trash": "Trash"}
SIDEBAR_SECTIONS = ("my-files", "shared-by-me", "shared-with-me", "devices", "trash")
SECTION_ICONS = {"my-files": "cloud", "shared-by-me": "share", "shared-with-me": "users",
                 "devices": "computer", "trash": "trash"}
# legacy "theme" icon names -> own illustration/icon
_ILLUSTRATIONS = {"view-refresh": "empty-loading", "dialog-password": "empty-login",
                  "dialog-error": "empty-error", "user-trash": "empty-trash",
                  "folder-open": "empty-folder", "edit-find": "empty-search"}
# accepted values for the `on_conflict` setting (legacy Spanish values kept for old config files)
# accepted values for the `on_conflict` setting (legacy Spanish values kept for old config files)
_CONFLICT_ALIASES = {"ask": "ask", "rename": "rename", "keep_both": "rename",
                     "replace": "replace", "skip": "skip"}
_MIME = QMimeDatabase()
S = QStyle.StandardPixmap


# -------------------------------------------------------------- utilities

def split_path(path: str) -> list[str]:
    """Components of a remote path (honours “\\/” inside names)."""
    return [p.replace("\\/", "/") for p in re.split(r"(?<!\\)/", path) if p]


def join_path(parts: list[str]) -> str:
    return "/" + "/".join(p.replace("/", "\\/") for p in parts)


def parent_path(path: str) -> str:
    return join_path(split_path(path)[:-1]) if len(split_path(path)) > 1 else path


def is_trash(path: str) -> bool:
    return path.rstrip("/") == TRASH


def label_for(path: str) -> str:
    """Human-readable name of a folder (sections use their display name)."""
    parts = split_path(path)
    if not parts:
        return "Proton Drive"
    return SECTION_LABELS.get(parts[0], parts[0]) if len(parts) == 1 else parts[-1]


_EN = QLocale(QLocale.Language.English)   # dates and sizes are always English, whatever the system locale


def fmt_size(n: int) -> str:
    return _EN.formattedDataSize(n, 1)


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def names_summary(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"


def entry_icon(e: Entry) -> QIcon:
    """Own icon per category (folder, image, PDF…) with its accent colour."""
    return theme.file_icon(e.name, e.is_dir)


def fmt_date(ts: float) -> str:
    """“Today, 14:30”, “Yesterday, 09:30”, “28 Sep” or “28 Sep 2025” (easy to scan in the list)."""
    from PySide6.QtCore import QDate, QDateTime
    if not ts:
        return ""
    dt, loc = QDateTime.fromSecsSinceEpoch(int(ts)), _EN
    today = QDate.currentDate()
    if dt.date() == today:
        return "Today, " + loc.toString(dt.time(), "HH:mm")
    if dt.date() == today.addDays(-1):
        return "Yesterday, " + loc.toString(dt.time(), "HH:mm")
    return loc.toString(dt.date(), "d MMM" if dt.date().year() == today.year() else "d MMM yyyy")


# ----------------------------------------------------------------- model

class DriveModel(QAbstractTableModel):
    """Entries of the current folder: Name, Size, Modified, Shared."""

    HEADERS = ("Name", "Size", "Modified", "Shared")
    SORT = Qt.ItemDataRole.UserRole
    ENTRY = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.entries: list[Entry] = []
        self._icons: dict[str, QIcon] = {}
        self._shared = theme.icon("link", "accent_text")

    def set_entries(self, entries: list[Entry]) -> None:
        self.beginResetModel()
        self.entries = list(entries)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                if section == 2 and getattr(self, "trash_mode", False):
                    return "Deleted"
                return self.HEADERS[section] + ("\u2003" if section == 1 else "")
            if role == Qt.ItemDataRole.TextAlignmentRole and section == 1:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _icon(self, e: Entry) -> QIcon:
        key = "/" if e.is_dir else Path(e.name).suffix.lower() or e.name
        if key not in self._icons:
            self._icons[key] = entry_icon(e)
        return self._icons[key]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        e, c = self.entries[index.row()], index.column()
        R = Qt.ItemDataRole
        if role == self.ENTRY:
            return e
        if role == R.DisplayRole:
            if c == 0:
                return e.name
            if c == 1:
                return "" if e.is_dir else fmt_size(e.size) + "\u2003"
            if c == 2:
                return fmt_date(e.mtime)
            return "Link" if e.shared_by_url else ""
        if role == R.DecorationRole:
            if c == 0:
                return self._icon(e)
            if c == 3 and e.shared_by_url:
                return self._shared
        if role == R.ToolTipRole and c == 3 and e.shared_by_url:
            return "Shared with a public link"
        if role == R.TextAlignmentRole and c == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == self.SORT:
            return (e.name.casefold(), e.size, e.mtime, e.shared_by_url)[c]
        return None


def _dt(ts: float):
    from PySide6.QtCore import QDateTime
    return QDateTime.fromSecsSinceEpoch(int(ts))


class SortProxy(QSortFilterProxyModel):
    """Per-column sort with folders always first, plus a name filter."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSortRole(DriveModel.SORT)
        self.setFilterKeyColumn(0)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a, b = left.data(DriveModel.ENTRY), right.data(DriveModel.ENTRY)
        if a.is_dir != b.is_dir:
            return a.is_dir if self.sortOrder() == Qt.SortOrder.AscendingOrder else not a.is_dir
        ka, kb = left.data(DriveModel.SORT), right.data(DriveModel.SORT)
        if ka == kb:
            return a.name.casefold() < b.name.casefold()
        return ka < kb


# ------------------------------------------------------------------ view

class DriveView(QTreeView):
    """File table that accepts dropped local files (onto a folder or the view)."""

    filesDropped = Signal(list, str)   # local paths, target remote folder ("" = current)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._drop_row = QModelIndex()
        self.hover_row = -1
        self.setHeader(FlatHeader(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(False)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setIndentation(0)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(RowDelegate(self))
        self.setSortingEnabled(True)

    def mouseMoveEvent(self, e) -> None:
        row = self.indexAt(e.position().toPoint()).row()
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(e)

    def resizeEvent(self, e) -> None:
        """Narrow window: secondary columns are hidden so the name always fits."""
        super().resizeEvent(e)
        self._apply_columns()

    def set_trash_mode(self, on: bool) -> None:
        """In Trash there is no “Shared” column and the date is the deletion date."""
        if on != getattr(self, "_trash_mode", False):
            self._trash_mode = on
            m = self.model()
            src = m.sourceModel() if hasattr(m, "sourceModel") else m
            src.trash_mode = on
            self.header().viewport().update()
            self._apply_columns()

    def _apply_columns(self) -> None:
        w = self.viewport().width()
        for col, min_w in ((3, 600), (2, 440)):
            self.setColumnHidden(col, w < min_w or (col == 3 and getattr(self, "_trash_mode", False)))

    def select_row(self, row: int) -> None:
        """Select only row ``row`` (proxy row)."""
        sm = self.selectionModel()
        sm.select(self.model().index(row, 0), sm.SelectionFlag.ClearAndSelect | sm.SelectionFlag.Rows)

    @staticmethod
    def _paths(e) -> list[Path]:
        return [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]

    def _folder_at(self, pos: QPoint) -> QModelIndex:
        idx = self.indexAt(pos)
        ent = idx.data(DriveModel.ENTRY) if idx.isValid() else None
        return idx.siblingAtColumn(0) if ent is not None and ent.is_dir else QModelIndex()

    def dragEnterEvent(self, e) -> None:
        e.acceptProposedAction() if self._paths(e) else e.ignore()

    def dragMoveEvent(self, e) -> None:
        if not self._paths(e):
            return e.ignore()
        self._drop_row = self._folder_at(e.position().toPoint())
        self.viewport().update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e) -> None:
        self._drop_row = QModelIndex()
        self.viewport().update()
        super().dragLeaveEvent(e)

    def dropEvent(self, e) -> None:
        paths = self._paths(e)
        idx = self._folder_at(e.position().toPoint())
        self._drop_row = QModelIndex()
        self.viewport().update()
        if paths:
            e.acceptProposedAction()
            self.filesDropped.emit(paths, idx.data(DriveModel.ENTRY).path if idx.isValid() else "")
        else:
            e.ignore()

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)   # the drop-target folder of a drag is highlighted by RowDelegate


# -------------------------------------------------------------- path bar

class _Crumbs(QFrame):
    """Breadcrumb container: clicking the empty space requests path editing."""

    editRequested = Signal()

    def mousePressEvent(self, e) -> None:
        self.editRequested.emit()


class _PathEdit(QLineEdit):
    cancelled = Signal()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        if e.reason() != Qt.FocusReason.PopupFocusReason:
            self.cancelled.emit()


class PathBar(QStackedWidget):
    """Clickable breadcrumbs; Ctrl+L (or clicking the empty space) turns them into an editable field."""

    navigated = Signal(str)
    MAX_CRUMBS = 5

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.crumbs = _Crumbs()
        self.crumbs.setObjectName("navpill")
        self.crumbs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.crumbs.setCursor(Qt.CursorShape.IBeamCursor)
        self.crumbs.setToolTip("Click the empty space to type a path (Ctrl+L)")
        self.lay = QHBoxLayout(self.crumbs)
        self.lay.setContentsMargins(40, 0, 8, 0)
        self.lay.setSpacing(2)
        lead = IconLabel("cloud", 18, "text2", self.crumbs)   # fixed icon at the left of the pill
        lead.move(15, 12)
        lead.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.crumbs.editRequested.connect(self.edit)
        self.edit_box = _PathEdit()
        theme.set_props(self.edit_box, variant="path")
        self.edit_box.returnPressed.connect(self._submit)
        self.edit_box.cancelled.connect(self.show_crumbs)
        self.addWidget(self.crumbs)
        self.addWidget(self.edit_box)
        self.setFixedHeight(42)
        self.path = ""

    def set_path(self, path: str) -> None:
        self.path = path
        parts = split_path(path)
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        shown = list(enumerate(parts))
        if len(parts) > self.MAX_CRUMBS:
            shown = shown[:1] + shown[-(self.MAX_CRUMBS - 2):]
        for n, (i, name) in enumerate(shown):
            if n:
                self.lay.addWidget(IconLabel("chevron-right", 14, "text3"))
                if len(parts) > self.MAX_CRUMBS and n == 1:  # hidden stretch
                    more = self._crumb("…", join_path(parts[:i]))
                    more.setToolTip(join_path(parts[:i]))
                    self.lay.insertWidget(self.lay.count() - 1, more)
                    self.lay.insertWidget(self.lay.count() - 1, IconLabel("chevron-right", 14, "text3"))
            text = SECTION_LABELS.get(name, name) if i == 0 else name
            self.lay.addWidget(self._crumb(text, join_path(parts[:i + 1]), n == len(shown) - 1))
        self.lay.addStretch(1)
        self.show_crumbs()

    def _crumb(self, text: str, target: str, current: bool = False) -> QToolButton:
        b = QToolButton()
        b.setText(text.replace("&", "&&"))
        theme.set_props(b, variant="crumb", current=current)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: self.navigated.emit(target))
        return b

    def edit(self) -> None:
        self.edit_box.setText(self.path)
        self.setCurrentWidget(self.edit_box)
        self.edit_box.setFocus()
        self.edit_box.selectAll()

    def show_crumbs(self) -> None:
        self.setCurrentWidget(self.crumbs)

    def _submit(self) -> None:
        text = self.edit_box.text().strip()
        self.show_crumbs()
        if text:
            self.navigated.emit("/" + text.strip("/"))


# ------------------------------------------------------- banners and empty state

class Banner(QFrame):
    """Non-modal notice above the list, using theme colours."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("banner")
        self.icon = IconLabel("info", 20)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.action = QPushButton()
        theme.set_props(self.action, variant="primary")
        self.action.clicked.connect(lambda: self._cb and self._cb())
        self.close_btn = QToolButton()
        theme.set_props(self.close_btn, variant="icon")
        self.close_btn.setIcon(theme.icon("close"))
        self.close_btn.setToolTip("Dismiss")
        self.close_btn.clicked.connect(self.hide)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 8, 10)
        lay.setSpacing(12)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.action)
        lay.addWidget(self.close_btn)
        self._cb: Callable[[], None] | None = None
        self.hide()

    def show_message(self, text: str, kind: str = "info", action: str = "",
                     callback: Callable[[], None] | None = None, closable: bool = True) -> None:
        kind = kind if kind in ("error", "warning") else "info"
        theme.set_props(self, kind=kind)
        self.icon.set_icon({"error": "error", "warning": "warning"}.get(kind, "info"),
                           {"error": "danger", "warning": "warning"}.get(kind, "accent_text"))
        self.text.setText(text)
        self.action.setVisible(bool(action))
        self.action.setText(action)
        self.action.setEnabled(True)
        self._cb = callback
        self.close_btn.setVisible(closable)
        self.show()


class Placeholder(QWidget):
    """Empty state: large icon, title and explanation (and an optional button)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.icon = IconLabel("empty-folder", 112, "accent_text")
        self.title = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        theme.set_props(self.title, role="title")
        self.detail = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setMinimumWidth(360)
        self.detail.setMaximumWidth(420)
        theme.set_props(self.detail, role="muted")
        self.button = QPushButton()
        theme.set_props(self.button, variant="primary")
        self.button.clicked.connect(lambda: self._cb and self._cb())
        self._cb: Callable[[], None] | None = None
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(8)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(6)
        lay.addWidget(self.title, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.detail, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(8)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignCenter)

    def show_state(self, icons: tuple[str, ...] | str, std: object, title: str,
                   detail: str = "", button: str = "", callback: Callable[[], None] | None = None) -> None:
        """``icons``: own illustration name (or, for compatibility, a legacy name)."""
        key = icons if isinstance(icons, str) else (icons[0] if icons else "")
        self.icon.set_icon(_ILLUSTRATIONS.get(key, key if key.startswith("empty-") else "empty-folder"),
                           "danger" if key == "dialog-error" else "accent_text")
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.button.setText(button)
        self.button.setVisible(bool(button))
        self._cb = callback


# ------------------------------------------------------------ main window

class MainWindow(QMainWindow):
    """Explorer: toolbar, breadcrumbs, side panel, table and transfers."""

    def __init__(self, client: Client, cfg: dict, settings: QSettings | None = None,
                 cache: ListingCache | None = None):
        super().__init__()
        self.client, self.cfg = client, cfg
        self.settings = settings or QSettings("pdrive", "pdrive")
        self.cache = cache if cache is not None else ListingCache()   # memory only if none is given
        self.cache.load()
        self._warmed = False
        self._pre_gen = 0              # prefetch: invalidated when navigating
        self._pre_queue: list[str] = []
        self._pre_active = 0
        self._pre_tries = 0
        self._pre_pool = QThreadPool(self)
        self._pre_pool.setMaxThreadCount(2)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(3000)
        self._save_timer.timeout.connect(self._save_cache)
        self.path = ""
        self.history: list[str] = []
        self.hpos = -1
        self.sections: list[str] = []
        self._gen = 0
        self._ops = 0
        self._state = "loading"        # loading | ok | error | login
        self._error_text = ""
        self._clip: tuple[str, list[str]] | None = None
        self._sizes: dict[str, int] = {}
        self._select_after = ""
        self._tmpdirs: list[Path] = []
        self._last_download = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or Path.home())

        self.setWindowTitle("Proton Drive")
        self.setWindowIcon(self._app_icon())
        self.resize(1100, 700)
        self.setAcceptDrops(True)

        self.transfers = TransferManager(self)
        self._build_model()
        self._build_actions()
        self._build_ui()
        self._build_menus()
        self._wire()
        self._restore()
        self._update_actions()
        QTimer.singleShot(0, self.start)

    # ------------------------------------------------------------ construction
    @staticmethod
    def _app_icon() -> QIcon:
        ic = QIcon.fromTheme("pdrive")
        if ic.isNull():
            svg = Path(__file__).resolve().parents[2] / "assets" / "pdrive.svg"
            ic = QIcon(str(svg)) if svg.is_file() else theme.icon("cloud", "accent_text")
        return ic

    def _build_model(self) -> None:
        self.model = DriveModel(self)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = DriveView()
        self.view.setModel(self.proxy)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        h = self.view.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, w in ((1, 100), (2, 150), (3, 110)):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            h.resizeSection(c, w)

    def _act(self, text: str, icon_name: str, slot: Callable,
             shortcut: str | QKeySequence.StandardKey | None = None, tip: str = "") -> QAction:
        a = QAction(theme.icon(icon_name), text, self)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
        sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        a.setToolTip((tip or text.replace("…", "")) + (f" ({sc})" if sc else ""))
        a.triggered.connect(lambda _=False: slot())
        return a

    def _build_actions(self) -> None:
        A = self._act
        K = QKeySequence.StandardKey
        self.a_back = A("Back", "back", self.go_back, K.Back)
        self.a_fwd = A("Forward", "forward", self.go_forward, K.Forward)
        self.a_up = A("Up one level", "up", self.go_up, "Alt+Up")
        self.a_refresh = A("Refresh", "refresh", self.force_refresh, K.Refresh)
        self.a_upload = A("Upload files…", "upload", self.pick_upload_files, "Ctrl+U")
        self.a_upload_dir = A("Upload folder…", "folder-open", self.pick_upload_dir, "Ctrl+Shift+U")
        self.a_download = A("Download…", "download", self.download_selected, "Ctrl+Shift+D")
        self.a_newdir = A("New folder…", "folder-plus", self.new_folder, "Ctrl+Shift+N")
        self.a_share = A("Share…", "users", self.share_selected, "Ctrl+Alt+S")
        self.a_copylink = A("Copy public link", "link", self.copy_link, "Ctrl+Shift+L")
        self.a_unlink = A("Remove public link", "close", self.remove_link)
        self.a_trashview = A("Trash", "trash", self.show_trash, "Ctrl+Alt+T")
        self.a_trashview.setCheckable(True)
        self.a_open = A("Open", "external", self.open_selected)
        self.a_rename = A("Rename…", "rename", self.rename_selected, "F2")
        self.a_copy = A("Copy", "copy", lambda: self.clip_set("copy"), K.Copy)
        self.a_cut = A("Cut", "cut", lambda: self.clip_set("move"), K.Cut)
        self.a_paste = A("Paste", "paste", self.paste, K.Paste)
        self.a_trash = A("Move to Trash", "trash", self.trash_selected, K.Delete)
        self.a_props = A("Properties", "info", self.properties_selected, "Alt+Return")
        self.a_copypath = A("Copy path", "copy", self.copy_path)
        self.a_restore = A("Restore", "restore", self.restore_selected)
        self.a_delete = A("Delete permanently", "trash", self.delete_selected, K.Delete)
        self.a_empty = A("Empty Trash", "trash", self.empty_trash)
        self.a_selall = A("Select all", "check", lambda: self.view.selectAll(), K.SelectAll)
        self.a_find = A("Search in this folder", "search", self.focus_search, K.Find)
        self.a_loc = A("Edit path", "rename", self.focus_path, "Ctrl+L")
        self.a_home = A("My files", "home", lambda: self.navigate(self._home()), "Alt+Home")
        self.a_panel = A("Transfers", "download", self.toggle_transfers, "Ctrl+Shift+T")
        self.a_panel.setCheckable(True)
        self.a_prefs = A("Settings…", "settings", self.open_settings, K.Preferences)
        self.a_logout = A("Sign out", "logout", self.logout)
        self.a_quit = A("Quit", "close", self.close, K.Quit)
        # Shortcuts that would clash with a text field only work while the table has focus.
        for a in (self.a_copy, self.a_cut, self.a_paste, self.a_trash, self.a_delete, self.a_rename,
                  self.a_selall):
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.view.addAction(a)
        self.a_delete.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _build_ui(self) -> None:
        # Row of actions on the selection (not the window toolbar: it lives inside the main column)
        tb = QToolBar("Actions")
        tb.setObjectName("main-toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(18, 18))
        self.toolbar = tb
        for a in (self.a_download, self.a_share, self.a_copylink, self.a_restore, self.a_delete, self.a_empty):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.session_banner = Banner()
        self.notice_banner = Banner()
        self.pathbar = PathBar()
        self.search = QLineEdit()
        theme.set_props(self.search, variant="search")
        self.search.setPlaceholderText("Search in folder")
        self.search.setMinimumWidth(170)
        self.search.setMaximumWidth(340)
        self.search.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.search.setFixedHeight(42)
        self.search.addAction(theme.icon("search", "text2"), QLineEdit.ActionPosition.LeadingPosition)
        clear = self.search.addAction(theme.icon("close", "text2"), QLineEdit.ActionPosition.TrailingPosition)
        clear.setToolTip("Clear search")
        clear.setVisible(False)
        clear.triggered.connect(self.search.clear)
        self.search.textChanged.connect(lambda s: clear.setVisible(bool(s)))

        def nav_button(a: QAction) -> QToolButton:
            b = QToolButton()
            b.setDefaultAction(a)
            theme.set_props(b, variant="nav")
            b.setIconSize(QSize(20, 20))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            return b

        nav = QHBoxLayout()
        nav.setSpacing(2)
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_refresh):
            nav.addWidget(nav_button(a))
        nav.addSpacing(8)
        nav.addWidget(self.pathbar, 2)
        nav.addSpacing(8)
        nav.addWidget(self.search, 1)
        self.btn_more = QToolButton()
        theme.set_props(self.btn_more, variant="nav")
        self.btn_more.setIcon(theme.icon("more"))
        self.btn_more.setIconSize(QSize(20, 20))
        self.btn_more.setToolTip("More options")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        nav.addWidget(self.btn_more)

        # Left column: brand, “New” button and sections
        self.side = QWidget()
        self.side.setObjectName("side")
        self.side.setMinimumWidth(214)
        self.side.setMaximumWidth(300)
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(20, 18, 6, 12)
        sl.setSpacing(0)
        brand = QHBoxLayout()
        brand.setSpacing(11)
        logo = QLabel()
        logo.setFixedSize(34, 34)
        logo.setPixmap(self._logo_pixmap(34))
        name = QLabel("Proton Drive")
        theme.set_props(name, role="brand")
        brand.addWidget(logo)
        brand.addWidget(name, 1)
        sl.addLayout(brand)
        sl.addSpacing(18)
        self.btn_new = QToolButton()
        theme.set_props(self.btn_new, variant="primary")
        self.btn_new.setText("New")
        self.btn_new.setIcon(theme.icon("plus", "onaccent"))
        self.btn_new.setIconSize(QSize(18, 18))
        self.btn_new.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_new.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_new.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_menu = QMenu(self.btn_new)
        for a in (self.a_upload, self.a_upload_dir, None, self.a_newdir):
            self.new_menu.addSeparator() if a is None else self.new_menu.addAction(a)
        self.btn_new.setMenu(self.new_menu)
        sl.addWidget(self.btn_new)
        sl.addSpacing(16)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.viewport().setAutoFillBackground(False)
        self.sidebar.setSpacing(1)
        self.sidebar.setCursor(Qt.CursorShape.PointingHandCursor)
        sl.addWidget(self.sidebar, 1)
        self.btn_prefs = QToolButton()
        self.btn_prefs.setDefaultAction(self.a_prefs)
        self.btn_prefs.setIconSize(QSize(18, 18))
        self.btn_prefs.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_prefs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        theme.set_props(self.btn_prefs, variant="sidebar")
        sl.addWidget(self.btn_prefs)

        # Main column: banners, navigation, actions and the card with the list
        self.placeholder = Placeholder()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.placeholder)
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 4, 0, 6)
        cl.addWidget(self.stack)
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 16, 20, 0)
        ml.setSpacing(10)
        ml.addWidget(self.session_banner)
        ml.addWidget(self.notice_banner)
        ml.addLayout(nav)
        ml.addWidget(self.toolbar)
        ml.addWidget(card, 1)

        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        self.split.addWidget(self.side)
        self.split.addWidget(main)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([250, 850])
        self.setCentralWidget(self.split)

        self.panel = TransferPanel(self.transfers)
        self.dock = QDockWidget("Transfers")
        self.dock.setObjectName("transfers-dock")
        self.dock.setWidget(self.panel)
        self.dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea)
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(22, 10, 16, 2)
        ttl = QLabel("Transfers")
        theme.set_props(ttl, role="heading")
        self.tx_count = chip("0", "accent")
        close = QToolButton()
        theme.set_props(close, variant="icon")
        close.setIcon(theme.icon("close"))
        close.setToolTip("Hide transfers")
        close.clicked.connect(self.dock.hide)
        hl.addWidget(ttl)
        hl.addWidget(self.tx_count)
        hl.addStretch(1)
        hl.addWidget(close)
        self.dock.setTitleBarWidget(head)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock)
        self.dock.setMinimumHeight(150)
        self.dock.hide()

        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        sb.setContentsMargins(0, 0, 14, 6)
        self.lbl_items = QLabel()
        self.lbl_items.setContentsMargins(22, 0, 0, 0)
        sb.addWidget(self.lbl_items, 1)
        self.progress = ActivityBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedSize(90, 6)
        self.progress.hide()
        sb.addPermanentWidget(self.progress)
        self.btn_size = QToolButton()
        theme.set_props(self.btn_size, variant="link")
        self.btn_size.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_size.clicked.connect(self.calc_size)
        sb.addPermanentWidget(self.btn_size)
        self.btn_tx = QToolButton()
        theme.set_props(self.btn_tx, variant="link")
        self.btn_tx.setDefaultAction(self.a_panel)
        self.btn_tx.setIconSize(QSize(16, 16))
        self.btn_tx.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        sb.addPermanentWidget(self.btn_tx)
        self._on_activity(0, 0)

    @staticmethod
    def _logo_pixmap(size: int) -> QPixmap:
        """Application logo (assets/pdrive.svg) rendered crisply at ``size`` px; accent cloud if missing."""
        svg = Path(__file__).resolve().parents[2] / "assets" / "pdrive.svg"
        if not svg.is_file():
            return theme.pixmap("cloud", size, "accent_text")
        from PySide6.QtSvg import QSvgRenderer
        dpr = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
        pm = QPixmap(round(size * dpr), round(size * dpr))
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(str(svg)).render(p)
        p.end()
        pm.setDevicePixelRatio(dpr)
        return pm

    def _build_menus(self) -> None:
        # No classic menu bar: a “More” (⋯) menu with the essentials. All actions are added to
        # the window so their shortcuts keep working even if they are in no visible menu.
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_home, self.a_refresh, self.a_upload,
                  self.a_upload_dir, self.a_newdir, self.a_download, self.a_share, self.a_copylink,
                  self.a_unlink, self.a_trashview, self.a_open, self.a_props, self.a_copypath,
                  self.a_restore, self.a_find, self.a_loc, self.a_panel, self.a_prefs, self.a_logout,
                  self.a_quit):
            self.addAction(a)
        m = QMenu(self.btn_more)
        for a in (self.a_upload, self.a_upload_dir, self.a_newdir, None,
                  self.a_home, self.a_trashview, None, self.a_refresh, self.a_loc, self.a_panel, None,
                  self.a_prefs, self.a_logout, None, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        self.btn_more.setMenu(m)
        self.more_menu = m
        # Extra shortcuts with no visible action: Enter opens, Backspace goes up.
        for key, slot in (("Return", self.open_selected), ("Enter", self.open_selected),
                          ("Backspace", self.go_up)):
            sc = QShortcut(QKeySequence(key), self.view)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(slot)
        sc = QShortcut(QKeySequence("Escape"), self.search)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(self._escape_search)

    def _wire(self) -> None:
        self.view.doubleClicked.connect(lambda _: self.open_selected())
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.filesDropped.connect(self._dropped)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
        self.model.modelReset.connect(self._update_actions)
        self.pathbar.navigated.connect(self.navigate)
        self.search.textChanged.connect(self._filter_changed)
        self.sidebar.itemClicked.connect(lambda it: self.navigate(it.data(Qt.ItemDataRole.UserRole)))
        self.dock.visibilityChanged.connect(lambda v: self.a_panel.setChecked(v))
        self.transfers.added.connect(lambda t: self.dock.show() if t.kind != "open" else None)
        self.transfers.activity.connect(self._on_activity)
        self.transfers.session_lost.connect(self._need_login)
        self.session_banner.close_btn.hide()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.side.setVisible(self.width() >= 780)   # narrow window: no side column (the ⋯ menu remains)

    # ------------------------------------------------------------ persistence
    def _restore(self) -> None:
        st = self.settings
        g = st.value("geometry")
        if g:
            self.restoreGeometry(g)
        # Not restoreState(): it would re-dock the in-layout "main-toolbar" saved by older versions.
        dv = st.value("dockVisible")
        if dv is not None:
            self.dock.setVisible(str(dv).lower() in ("true", "1"))
        for c in (1, 2, 3):
            w = st.value(f"col{c}", type=int)
            if w:
                self.view.header().resizeSection(c, w)
        sp = st.value("splitter")
        if sp:
            self.split.restoreState(sp)
        self.a_panel.setChecked(self.dock.isVisible())

    def _save(self) -> None:
        st = self.settings
        st.setValue("geometry", self.saveGeometry())
        st.setValue("dockVisible", self.dock.isVisible())
        st.remove("windowState")   # legacy key from older versions
        st.setValue("splitter", self.split.saveState())
        for c in (1, 2, 3):
            st.setValue(f"col{c}", self.view.header().sectionSize(c))
        if self.path and not is_trash(self.path):
            st.setValue("lastPath", self.path)
        st.sync()

    def closeEvent(self, ev) -> None:
        if self.transfers.busy():
            n = self.transfers.count(RUNNING)
            if not self._confirm("Transfers in progress",
                                 f"{'There is' if n == 1 else 'There are'} {plural(n, 'transfer', 'transfers')} "
                                 "in progress. If you quit now, running transfers will finish and "
                                 "pending ones will be cancelled.", "Quit"):
                ev.ignore()
                return
        self._save()
        self._pre_gen += 1
        self._pre_queue.clear()
        self._save_timer.stop()
        self.cache.save()
        self.hide()
        self.transfers.shutdown()
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        super().closeEvent(ev)

    def _save_cache(self) -> None:
        if self.cache.dirty:
            run_async(self.cache.save, parent=self)

    # ----------------------------------------------------------------- utilities
    def _home(self) -> str:
        return next((p for p in self.sections if p.rstrip("/").endswith("my-files")), DEFAULT_ROOT)

    def _conflict_default(self) -> str:
        return _CONFLICT_ALIASES.get(str(self.cfg.get("on_conflict") or "ask").lower(), "ask")

    def notice(self, text: str, error: bool = False) -> None:
        """Non-modal message: status bar or, for an error, a dismissible banner."""
        if error:
            self.notice_banner.show_message(text, "error")
        else:
            self.notice_banner.hide()
            self.statusBar().showMessage(text, 8000)

    def _confirm(self, title: str, text: str, ok_text: str) -> bool:
        danger = ok_text.startswith(("Delete", "Empty", "Remove"))   # destructive actions in red
        return confirm(self, title, text, ok_text, danger=danger)

    def _ask_text(self, title: str, label: str, default: str = "") -> str | None:
        return prompt(self, title, label, default)

    def _async(self, fn: Callable, *args: Any, ok: Callable[[Any], None] | None = None,
               err: Callable[[Exception], bool | None] | None = None, ctx: str = "") -> None:
        """Run ``fn`` in a thread with an activity indicator and non-modal errors."""
        self._ops += 1
        self._sync_busy()

        def good(r: Any) -> None:
            self._ops -= 1
            self._sync_busy()
            if ok:
                ok(r)

        def bad(e: Exception) -> None:
            self._ops -= 1
            self._sync_busy()
            if not (err and err(e)):
                self._fail(e, ctx)

        run_async(fn, *args, on_result=good, on_error=bad, parent=self)

    def _sync_busy(self) -> None:
        self.progress.setVisible(self._ops > 0)

    def _fail(self, e: Exception, ctx: str = "") -> None:
        if isinstance(e, NotLoggedIn):
            self._need_login()
        else:
            self.notice(f"{ctx}: {e}" if ctx else str(e), error=True)

    def _need_login(self) -> None:
        self._state = "login"
        self.session_banner.show_message("You are not signed in to Proton Drive, or your session has expired.",
                                         "warning", "Sign in", self.login, closable=False)
        self._refresh_view()

    def selected(self) -> list[Entry]:
        rows = {i.row() for i in self.view.selectionModel().selectedRows()}
        return [self.proxy.index(r, 0).data(DriveModel.ENTRY) for r in sorted(rows)]

    @property
    def in_trash(self) -> bool:
        return is_trash(self.path)

    # ------------------------------------------------------------------ session
    def start(self) -> None:
        """Load the sections and open the last folder."""
        last = str(self.settings.value("lastPath") or "")

        def got(entries: list[Entry] | None) -> None:
            have = {e.path.strip("/"): e.path for e in entries or []}
            paths = [have.get(n, "/" + n) for n in SIDEBAR_SECTIONS if have or n in ("my-files", "trash")]
            if not have:
                paths = [DEFAULT_ROOT, "/shared-by-me", "/shared-with-me", "/devices", TRASH]
            if paths != self.sections:
                self.sections = paths
                self._fill_sidebar()
            first = last if last and any(last == p or last.startswith(p + "/") for p in paths) else self._home()
            if not self.path:
                self.navigate(first)

        token = self.cache.token()

        def fresh(entries: list[Entry]) -> None:
            self._cache_put(SECTIONS_KEY, entries, token)
            got(entries)

        hit = self.cache.get(SECTIONS_KEY)
        if hit:   # sections already known: sidebar and folder appear instantly; revalidated quietly
            got(hit[0])
            run_async(self.client.sections, on_result=fresh, parent=self,
                      on_error=lambda e: self._fail(e) if isinstance(e, NotLoggedIn) else None)
        else:
            self._async(self.client.sections, ok=fresh, err=lambda e: got(None) or self._fail(e) or True)

    def _fill_sidebar(self) -> None:
        self.sidebar.clear()
        for p in self.sections:
            name = split_path(p)[0] if split_path(p) else p
            it = QListWidgetItem(theme.icon(SECTION_ICONS.get(name, "folder"), "text2"),
                                 SECTION_LABELS.get(name, name))
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setSizeHint(QSize(0, 42))
            self.sidebar.addItem(it)
        self._sync_sidebar()

    def _sync_sidebar(self) -> None:
        for i in range(self.sidebar.count()):
            p = self.sidebar.item(i).data(Qt.ItemDataRole.UserRole)
            if self.path == p or self.path.startswith(p + "/"):
                self.sidebar.setCurrentRow(i)
                return
        self.sidebar.clearSelection()

    def login(self) -> None:
        self.session_banner.show_message("Finish signing in in your browser…", "info", closable=False)
        self._async(self.client.login, ok=lambda _: self._logged(),
                    err=lambda e: self._login_failed(e), ctx="Could not sign in")

    def _login_failed(self, e: Exception) -> bool:
        self.session_banner.show_message(f"Could not sign in: {e}", "error", "Retry",
                                         self.login, closable=False)
        return True

    def _logged(self) -> None:
        self.session_banner.hide()
        self.notice("Signed in.")
        self._state = "loading"
        if not self.sections:
            self.start()
        else:
            self.refresh(clear=True)

    def logout(self) -> None:
        def done(_: Any) -> None:
            self.cache.clear()   # nothing from the previous Drive may remain visible or on disk
            self.cache.save()
            self.model.set_entries([])
            self._need_login()
        self._async(self.client.logout, ok=done, ctx="Could not sign out")

    # -------------------------------------------------------------- navigation
    def navigate(self, path: str, push: bool = True) -> None:
        path = "/" + "/".join(split_path(path)) if split_path(path) else "/"
        if push:
            if self.history and self.hpos >= 0 and self.history[self.hpos] == path and path == self.path:
                return self.refresh()
            del self.history[self.hpos + 1:]
            self.history.append(path)
            self.hpos = len(self.history) - 1
        self.path = path
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.setFilterFixedString("")
        self.pathbar.set_path(path)
        self.a_trashview.setChecked(self.in_trash)
        self.setWindowTitle(f"{label_for(path)} — Proton Drive")
        self._sync_sidebar()
        self._pre_gen += 1          # discards the prefetch of the previous folder
        self._pre_queue.clear()
        self.refresh(clear=True)

    def go_back(self) -> None:
        if self.hpos > 0:
            self.hpos -= 1
            self.navigate(self.history[self.hpos], push=False)

    def go_forward(self) -> None:
        if self.hpos < len(self.history) - 1:
            self.hpos += 1
            self.navigate(self.history[self.hpos], push=False)

    def go_up(self) -> None:
        if len(split_path(self.path)) > 1:
            self.navigate(parent_path(self.path))

    def show_trash(self) -> None:
        if self.in_trash:
            self.navigate(self._home())
        else:
            self.navigate(TRASH)

    def force_refresh(self) -> None:
        """Refresh (F5): ignores the cache and discards the client sessions (fresh data)."""
        self.refresh(force=True)

    def refresh(self, clear: bool = False, force: bool = False) -> None:
        """Reload the current folder. With a cache hit it paints instantly and revalidates in the
        background (the model only changes if there are differences). ``clear``: no cache, empties
        the view and shows “Loading”. ``force``: ignores the cache and recycles the client sessions."""
        if not self.path:
            return
        self._gen += 1
        gen, path, trash = self._gen, self.path, self.in_trash
        key = TRASH if trash else path
        if force:
            self.cache.invalidate(key)
            recycle = getattr(self.client, "recycle_sessions", None)
            if recycle:
                recycle()
        hit = self.cache.get(key) if clear and not force else None
        if clear:
            if hit:
                self._apply_entries(hit[0], keep=False)
                self._state = "ok"
                if self.notice_banner.isVisible():
                    self.notice_banner.hide()
            else:
                self.model.set_entries([])
                self._state = "loading"
            self._refresh_view()
        if self._state == "login" and not clear:
            self._state = "loading"
        fn = self.client.list_trash if trash else self.client.list
        args = () if trash else (path,)
        token = self.cache.token()
        cached = bool(hit)

        def ok(entries: list[Entry]) -> None:
            self._cache_put(key, entries, token)   # still valid even if we already navigated to another folder
            if gen != self._gen:
                return
            same = cached and entries == self.model.entries and self._state == "ok"
            if not same:
                self._apply_entries(entries)
                self._state = "ok"
                if self.notice_banner.isVisible() and clear:
                    self.notice_banner.hide()
                self._refresh_view()
            self._select_pending()
            self._warm_sessions()
            if not trash:
                self._prefetch(entries)

        def bad(e: Exception) -> bool:
            if gen != self._gen:
                return True
            if isinstance(e, NotLoggedIn):
                return False
            if isinstance(e, NotFound):
                self.cache.invalidate_tree(path)
                self.cache.invalidate(parent_path(path))
            elif cached:   # cached data is on screen: a network failure does not discard it
                self.notice(f"Could not refresh “{label_for(path)}”: {e}", error=True)
                return True
            self.model.set_entries([])
            self._state, self._error_text = "error", str(e)
            self._refresh_view()
            return True

        if cached:   # silent revalidation: no activity bar
            run_async(fn, *args, on_result=ok, parent=self,
                      on_error=lambda e: None if bad(e) else self._fail(e))
        else:
            self._async(fn, *args, ok=ok, err=bad)

    def _warm_sessions(self) -> None:
        """Start the client's persistent sessions (once, after the first folder is shown so it
        does not compete for CPU with the initial load)."""
        warm = None if self._warmed else getattr(self.client, "warm", None)
        self._warmed = True
        if warm:
            warm()

    def _apply_entries(self, entries: list[Entry], keep: bool = True) -> None:
        """Put ``entries`` in the model; with ``keep`` selection and scroll are preserved."""
        sel = {e.path for e in self.selected()} if keep else set()
        bar = self.view.verticalScrollBar()
        scroll = bar.value() if keep else 0
        self.model.set_entries(entries)
        if sel:
            sm = self.view.selectionModel()
            flags = sm.SelectionFlag.Select | sm.SelectionFlag.Rows
            for r in range(self.proxy.rowCount()):
                idx = self.proxy.index(r, 0)
                e = idx.data(DriveModel.ENTRY)
                if e is not None and e.path in sel:
                    sm.select(idx, flags)
        if keep and scroll:
            QTimer.singleShot(0, lambda: bar.setValue(scroll))

    def _cache_put(self, key: str, entries: list[Entry], token: int | None = None) -> None:
        if self.cache.put(key, entries, token=token) and self.cache.path:
            self._save_timer.start()   # delayed save to disk (debounce)

    def _invalidate(self, listings: Any = (), trees: Any = ()) -> None:
        """Forget cached listings (``listings``) and folders with everything below them (``trees``)."""
        for p in listings:
            self.cache.invalidate(p)
        for p in trees:
            self.cache.invalidate_tree(p)

    def _writing(self, fn: Callable, listings: Any = (), trees: Any = ()) -> Callable:
        """Wrap a writing operation: when it finishes (successfully or not) the affected cache is invalidated."""
        def run(*a: Any) -> Any:
            try:
                return fn(*a)
            finally:
                self._invalidate(listings, trees)
        return run

    # -- subfolder prefetch
    def _prefetch(self, entries: list[Entry]) -> None:
        """List up to 12 subfolders in the background (2 at a time) so entering them is instant."""
        self._pre_gen += 1
        self._pre_queue = [e.path for e in entries if e.is_dir and e.path not in self.cache][:12]
        self._pre_tries = 0
        self._pre_pump(self._pre_gen)

    def _pre_pump(self, gen: int) -> None:
        if gen != self._pre_gen or not self._pre_queue:
            return
        if not getattr(self.client, "fast_reads", False):   # not worth it without ready sessions
            self._pre_tries += 1
            if self._pre_tries <= 10:
                QTimer.singleShot(500, lambda: self._pre_pump(gen))
            return
        if self._ops > 0:   # what the user asks for comes first
            QTimer.singleShot(150, lambda: self._pre_pump(gen))
            return
        while self._pre_active < 2 and self._pre_queue:
            p = self._pre_queue.pop(0)
            if p in self.cache:
                continue
            self._pre_active += 1
            token = self.cache.token()

            def done(entries: list[Entry] | None, p: str = p, token: int = token) -> None:
                self._pre_active -= 1
                if entries is not None:
                    self._cache_put(p, entries, token)
                self._pre_pump(self._pre_gen)

            run_async(self.client.list, p, on_result=done, on_error=lambda e, d=done: d(None),
                      parent=self, pool=self._pre_pool)

    def _select_pending(self) -> None:
        if not self._select_after:
            return
        for r in range(self.proxy.rowCount()):
            if self.proxy.index(r, 0).data() == self._select_after:
                self.view.select_row(r)
                self.view.scrollTo(self.proxy.index(r, 0))
                break
        self._select_after = ""

    def _filter_changed(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)
        self._refresh_view()

    def _escape_search(self) -> None:
        self.search.clear()
        self.view.setFocus()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def focus_path(self) -> None:
        self.pathbar.edit()

    def _refresh_view(self) -> None:
        """Choose between the table and the empty/loading/error state; update the status bar."""
        ph, total, shown = self.placeholder, self.model.rowCount(), self.proxy.rowCount()
        text = self.search.text()
        if self._state == "loading":
            ph.show_state("empty-loading", None, "Loading…", "Just a moment, we're fetching your files.")
        elif self._state == "login":
            ph.show_state("empty-login", None, "Not signed in",
                          "Connect your Proton account to browse, upload and share your Drive.",
                          "Sign in", self.login)
        elif self._state == "error":
            ph.show_state("empty-error", None, "Couldn't load this folder",
                          self._error_text, "Retry", lambda: self.refresh(clear=True))
        elif total == 0 and self.in_trash:
            ph.show_state("empty-trash", None, "Trash is empty",
                          "Anything you delete will show up here, and you can restore it any time.")
        elif total == 0:
            ph.show_state("empty-folder", None, "This folder is empty",
                          "Drag files or folders here from your computer to upload them, or use “Upload files”.",
                          "Upload files…", self.pick_upload_files)
        elif shown == 0:
            ph.show_state("empty-search", None, "No results",
                          f"No items in this folder match “{text}”.")
        else:
            self.stack.setCurrentWidget(self.view)
            self._update_status()
            return
        self.stack.setCurrentWidget(self.placeholder)
        self._update_status()

    def _update_status(self) -> None:
        total, shown, sel = self.model.rowCount(), self.proxy.rowCount(), len(self.selected())
        if self._state != "ok":
            self.lbl_items.setText("")
        elif sel:
            self.lbl_items.setText(f"{sel} of {plural(shown, 'item', 'items')} selected")
        elif self.search.text():
            self.lbl_items.setText(f"{shown} of {plural(total, 'item', 'items')}")
        else:
            self.lbl_items.setText(plural(total, "item", "items"))
        size = self._sizes.get(self.path)
        self.btn_size.setVisible(not self.in_trash and self._state == "ok")
        self.btn_size.setText(f"Folder size: {fmt_size(size)}" if size is not None
                              else "Calculate folder size")
        self.btn_size.setToolTip("Calculate the total size of this folder" if size is None
                                 else "Recalculate the size")
        self.btn_size.setEnabled(True)

    def calc_size(self) -> None:
        path = self.path
        self.btn_size.setText("Calculating…")
        self.btn_size.setEnabled(False)
        self._sizes.pop(path, None)

        def ok(n: int) -> None:
            self._sizes[path] = n
            self._update_status()

        self._async(self.client.size, path, ok=ok, err=lambda e: (self._update_status(), False)[1],
                    ctx="Could not calculate the size")

    def _on_activity(self, running: int, queued: int) -> None:
        if running + queued:
            text = f"{running} active" + (f" · {queued} queued" if queued else "")
        else:
            text = "No active transfers"
        self.a_panel.setText("Transfers: " + text)
        self.a_panel.setToolTip("Show or hide transfers (Ctrl+Shift+T)")
        self.tx_count.setText(f"{running + queued} active" if running + queued else "Idle")

    def toggle_transfers(self) -> None:
        self.dock.setVisible(not self.dock.isVisible())

    # ----------------------------------------------------------------- actions
    def _update_actions(self, *_: Any) -> None:
        sel = self.selected()
        n, one = len(sel), len(sel) == 1
        drive = bool(self.path) and not self.in_trash
        can = drive and self._state != "login"
        for a, on in ((self.a_back, self.hpos > 0), (self.a_fwd, self.hpos < len(self.history) - 1),
                      (self.a_up, len(split_path(self.path)) > 1),
                      (self.a_refresh, bool(self.path)),
                      (self.a_upload, can), (self.a_upload_dir, can), (self.a_newdir, can),
                      (self.a_paste, can),
                      (self.a_download, can and n > 0), (self.a_trash, can and n > 0),
                      (self.a_share, can and one), (self.a_copylink, can and one),
                      (self.a_unlink, can and one and sel[0].shared_by_url),
                      (self.a_rename, can and one), (self.a_props, can and one),
                      (self.a_open, n > 0 and not self.in_trash), (self.a_copy, can and n > 0),
                      (self.a_cut, can and n > 0), (self.a_copypath, n > 0 or bool(self.path)),
                      (self.a_restore, self.in_trash and n > 0), (self.a_delete, self.in_trash and n > 0),
                      (self.a_empty, self.in_trash and self.model.rowCount() > 0)):
            a.setEnabled(bool(on))
        # Del: move to Trash in the Drive, delete permanently in Trash (same shortcut, never both at once).
        self.a_trash.setVisible(not self.in_trash)
        self.view.set_trash_mode(self.in_trash)
        for a in (self.a_restore, self.a_empty, self.a_delete):
            a.setVisible(self.in_trash)
        self.a_trash.setShortcutVisibleInContextMenu(True)
        for a in (self.a_upload, self.a_upload_dir, self.a_download, self.a_newdir, self.a_share, self.a_copylink):
            a.setVisible(not self.in_trash)
        self._update_status()

    def _context_menu(self, pos: QPoint) -> None:
        idx = self.view.indexAt(pos)
        if idx.isValid() and not self.view.selectionModel().isRowSelected(idx.row(), QModelIndex()):
            self.view.select_row(idx.row())
        self._build_menu().exec(self.view.viewport().mapToGlobal(pos))

    def _build_menu(self) -> QMenu:
        sel = self.selected()
        m = QMenu(self)
        if self.in_trash:
            if sel:
                m.addAction(self.a_restore)
                m.addAction(self.a_delete)
                m.addSeparator()
            m.addAction(self.a_empty)
            m.addAction(self.a_refresh)
        elif sel:
            if len(sel) == 1:
                m.addAction(self.a_open)
            m.addAction(self.a_download)
            m.addSeparator()
            for a in (self.a_cut, self.a_copy, self.a_paste):
                m.addAction(a)
            m.addSeparator()
            if len(sel) == 1:
                m.addAction(self.a_rename)
            m.addAction(self.a_trash)
            m.addSeparator()
            if len(sel) == 1:
                m.addAction(self.a_share)
                m.addAction(self.a_copylink)
                if sel[0].shared_by_url:
                    m.addAction(self.a_unlink)
                m.addSeparator()
            m.addAction(self.a_copypath)
            if len(sel) == 1:
                m.addAction(self.a_props)
        else:
            for a in (self.a_newdir, self.a_upload, self.a_upload_dir, self.a_paste, None,
                      self.a_refresh, self.a_copypath):
                m.addSeparator() if a is None else m.addAction(a)
        return m

    # -- open
    def open_selected(self) -> None:
        sel = self.selected()
        if len(sel) != 1 or self.in_trash:
            return
        e = sel[0]
        if e.is_dir:
            self.navigate(e.path)
            return
        tmp = Path(tempfile.mkdtemp(prefix="pdrive-"))
        self._tmpdirs.append(tmp)

        def opened() -> None:
            files = [p for p in tmp.iterdir() if p.is_file()]
            if files:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0])))

        self.transfers.enqueue("open", e.name, "temporary folder",
                               lambda: self.client.download(e.path, tmp, "replace"), opened)

    # -- upload
    def pick_upload_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files to " + label_for(self.path))
        if files:
            self.start_upload([Path(f) for f in files])

    def pick_upload_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Upload folder to " + label_for(self.path))
        if d:
            self.start_upload([Path(d)])

    def _dropped(self, paths: list[Path], target: str) -> None:
        self.start_upload(paths, target or None)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e) -> None:
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.start_upload(paths)

    def start_upload(self, paths: list[Path], target: str | None = None) -> None:
        """Upload ``paths`` to ``target`` (current folder by default), resolving conflicts."""
        target = target or self.path
        paths = [p for p in paths if p.exists()]
        if not paths or not target:
            return
        if self.in_trash or is_trash(target):
            self.notice("You can't upload to Trash.", error=True)
            return
        default = self._conflict_default()
        if default != "ask":
            self.transfers.enqueue(
                "upload", names_summary([p.name for p in paths]), label_for(target),
                self._writing(lambda: self.client.upload(paths, target, on_conflict=default),
                              [target], [f"{target.rstrip('/')}/{p.name}" for p in paths]),
                lambda: self.refresh() if self.path == target else None)
            return

        def listed(entries: list[Entry]) -> None:
            existing = {e.name for e in entries}
            clash = [p.name for p in paths if p.name in existing]
            choices = resolve_conflicts(self, clash, label_for(target), default) if clash else {}
            if choices is not None:
                self._enqueue_uploads(paths, target, choices)

        self._async(self.client.list, target, ok=listed,
                    err=lambda e: listed([]) or True if isinstance(e, NotFound) else False,
                    ctx="Could not check the destination folder")

    def _enqueue_uploads(self, paths: list[Path], target: str, choices: dict[str, str]) -> None:
        groups: dict[str, list[Path]] = {}
        for p in paths:
            s = choices.get(p.name, "rename")
            if s != "skip":
                groups.setdefault(s, []).append(p)
        if not groups:
            self.notice("Upload skipped: all items already exist.")
        for s, ps in groups.items():
            self.transfers.enqueue(
                "upload", names_summary([p.name for p in ps]), label_for(target),
                self._writing(lambda ps=ps, s=s: self.client.upload(ps, target, on_conflict=s),
                              [target], [f"{target.rstrip('/')}/{p.name}" for p in ps]),
                lambda: self.refresh() if self.path == target else None)

    # -- download
    def download_selected(self) -> None:
        sel = self.selected()
        if not sel:
            return
        d = QFileDialog.getExistingDirectory(self, "Download to…", str(self._last_download))
        if d:
            self._last_download = Path(d)
            self.start_download(sel, Path(d))

    def start_download(self, entries: list[Entry], dest: Path) -> None:
        clash = [e.name for e in entries if (dest / e.name).exists()]
        choices: dict[str, str] | None = {}
        if clash:
            choices = resolve_conflicts(self, clash, str(dest), self._conflict_default())
        if choices is None:
            return
        for e in entries:
            s = choices.get(e.name, "rename")
            if s == "skip":
                continue
            self.transfers.enqueue("download", e.name, str(dest),
                                   lambda e=e, s=s: self.client.download(e.path, dest, s))

    # -- create / rename / move
    def new_folder(self) -> None:
        name = self._ask_text("New folder", "Folder name:", "New folder")
        if not name:
            return
        parent = self.path

        def ok(_: Any) -> None:
            self._select_after = name
            if self.path == parent:
                self.refresh()

        self._async(self._writing(self.client.mkdir, [parent]), parent, name, ok=ok,
                    ctx="Could not create the folder")

    def rename_selected(self) -> None:
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]
        name = self._ask_text("Rename", "New name:", e.name)
        if not name or name == e.name:
            return

        def ok(_: Any) -> None:
            self._select_after = name
            self.refresh()

        self._async(self._writing(self.client.rename, [parent_path(e.path)], [e.path]), e.path, name,
                    ok=ok, ctx="Could not rename")

    def clip_set(self, mode: str) -> None:
        sel = self.selected()
        if not sel:
            return
        self._clip = (mode, [e.path for e in sel])
        QGuiApplication.clipboard().setText("\n".join(e.path for e in sel))
        self.notice(f"{plural(len(sel), 'item', 'items')} {'copied' if mode == 'copy' else 'cut'}.")

    def paste(self) -> None:
        dest = self.path
        if self._clip and QGuiApplication.clipboard().text() == "\n".join(self._clip[1]):
            mode, paths = self._clip
            if mode == "move" and all(parent_path(p) == dest for p in paths):
                return self.notice("Those items are already in this folder.")
            fn = self.client.move if mode == "move" else self.client.copy
            verb = "moved" if mode == "move" else "copied"

            def ok(_: Any) -> None:
                if mode == "move":
                    self._clip = None
                self.notice(f"{plural(len(paths), 'item', 'items')} {verb}.")
                if self.path == dest:
                    self.refresh()

            moved = mode == "move"
            fn = self._writing(fn, [dest] + ([parent_path(p) for p in paths] if moved else []),
                               paths if moved else [])
            self._async(fn, paths, dest, ok=ok, ctx=f"Could not {'move' if moved else 'copy'} the items")
            return
        urls = [Path(u.toLocalFile()) for u in QGuiApplication.clipboard().mimeData().urls() if u.isLocalFile()]
        if urls:  # files copied from the file manager (e.g. Dolphin)
            self.start_upload(urls)

    def trash_selected(self) -> None:
        sel = self.selected()
        if not sel or self.in_trash:
            return
        names = [e.name for e in sel]
        if self.cfg.get("confirm_trash", True) and not self._confirm(
                "Move to Trash",
                f"Move “{names_summary(names)}” to Trash?\nYou can restore it from Trash.",
                "Move to Trash"):
            return

        def work() -> None:
            for e in sel:
                self.client.trash(e.path)

        work = self._writing(work, [TRASH] + [parent_path(e.path) for e in sel], [e.path for e in sel])
        self._async(work, ok=lambda _: (self.notice(f"“{names_summary(names)}” moved to Trash."),
                                        self.refresh()), ctx="Could not move to Trash")

    # -- trash
    def restore_selected(self) -> None:
        sel = self.selected()
        if not sel:
            return

        def work() -> None:
            for e in sel:
                self.client.restore(e.path)

        work = self._writing(work, [TRASH], [self._home()])   # which folder it returns to is ignored
        self._async(work, ok=lambda _: (self.notice(f"“{names_summary([e.name for e in sel])}” restored."),
                                        self.refresh()), ctx="Could not restore")

    def delete_selected(self) -> None:
        sel = self.selected()
        if not sel or not self.in_trash:
            return
        names = names_summary([e.name for e in sel])
        if not self._confirm("Delete permanently",
                             f"Delete “{names}” permanently?\n\nThis can NOT be undone: "
                             "the files will be erased from Proton Drive for good.",
                             "Delete permanently"):
            return

        def work() -> None:
            for e in sel:
                self.client.delete(e.path)

        work = self._writing(work, [TRASH])
        self._async(work, ok=lambda _: (self.notice(f"“{names}” deleted permanently."), self.refresh()),
                    ctx="Could not delete")

    def empty_trash(self) -> None:
        if not self.in_trash or not self._confirm(
                "Empty Trash",
                "Empty Trash?\n\nAll items will be deleted permanently and you won't be "
                "able to recover them.", "Empty Trash"):
            return
        self._async(self._writing(self.client.empty_trash, [TRASH]),
                    ok=lambda _: (self.notice("Trash emptied."), self.refresh()),
                    ctx="Could not empty Trash")

    # -- sharing and properties (dialogs from dialogs.py, imported on use)
    def _dialogs(self):
        try:
            from . import dialogs
            return dialogs
        except Exception as e:  # noqa: BLE001 - degrade with a notice
            self.notice(f"This feature isn't available ({e.__class__.__name__}: {e}).", error=True)
            return None

    def share_selected(self) -> None:
        sel = self.selected()
        d = self._dialogs() if len(sel) == 1 else None
        if d:
            d.ShareDialog(self.client, sel[0].path, self).exec()
            self._invalidate([parent_path(sel[0].path), "/shared-by-me"])   # links/invitations change the icon
            self.refresh()

    def properties_selected(self) -> None:
        sel = self.selected()
        d = self._dialogs() if len(sel) == 1 else None
        if d:
            d.PropertiesDialog(self.client, sel[0], sel[0].path, self).exec()

    def open_settings(self) -> None:
        d = self._dialogs()
        if not d:
            return
        dlg = d.SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg = dlg.result_cfg()
            self._apply_disk_cache()
            try:
                d.save_user_config(self.cfg)
                self.notice("Settings saved.")
            except OSError as e:
                self.notice(f"Could not save settings: {e}", error=True)

    def _apply_disk_cache(self) -> None:
        """Enable or disable the disk cache according to Settings (disabling deletes the file)."""
        from .cache import default_path
        if self.cfg.get("disk_cache"):
            if self.cache.path is None:
                self.cache.path = default_path()
                run_async(self.cache.save, parent=self)
        else:
            self.cache.path = None
            try:
                default_path().unlink(missing_ok=True)
            except OSError:
                pass

    def copy_link(self) -> None:
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]

        def work() -> str:
            url = None
            if e.shared_by_url:  # do not touch a link that already exists
                try:
                    url = self.client.sharing_status(e.path).get("url")
                except NotLoggedIn:
                    raise
                except DriveError:
                    pass
            return url or self.client.link(e.path)

        def ok(url: str) -> None:
            QGuiApplication.clipboard().setText(url)
            self.notice(f"Public link for “{e.name}” copied to the clipboard.")
            self.refresh()

        self._async(self._writing(work, [parent_path(e.path), "/shared-by-me"]), ok=ok,
                    ctx="Could not get the link")

    def remove_link(self) -> None:
        sel = self.selected()
        if len(sel) != 1 or not self._confirm(
                "Remove public link",
                f"Remove the public link for “{sel[0].name}”?\nAnyone who has it will no longer be able to open it.",
                "Remove link"):
            return
        e = sel[0]
        self._async(self._writing(self.client.unlink, [parent_path(e.path), "/shared-by-me"]), e.path,
                    ok=lambda _: (self.notice("Public link removed."), self.refresh()),
                    ctx="Could not remove the link")

    def copy_path(self) -> None:
        sel = self.selected()
        text = "\n".join(e.path for e in sel) if sel else self.path
        QGuiApplication.clipboard().setText(text)
        self.notice("Path copied to the clipboard.")
