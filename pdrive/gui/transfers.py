"""Transfer queue (max. 2 at a time), its panel and the conflict dialog."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, QSize, QThreadPool, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSizePolicy, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from ..client import NotLoggedIn
from . import theme
from .widgets import ActivityBar, Tile, font_with
from .workers import run_async

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"
_ids = itertools.count(1)


@dataclass
class Transfer:
    """A queued operation: ``run`` runs in a thread, ``on_done`` in the GUI thread."""

    kind: str                       # upload | download | open
    title: str
    dest: str
    run: Callable[[], None]
    on_done: Callable[[], None] | None = None
    state: str = QUEUED
    message: str = ""
    id: int = field(default_factory=lambda: next(_ids))


class TransferManager(QObject):
    """Queue with a maximum number of simultaneous transfers."""

    added = Signal(object)
    changed = Signal(object)
    removed = Signal(object)
    activity = Signal(int, int)     # running, queued
    session_lost = Signal()

    def __init__(self, parent: QObject | None = None, max_parallel: int = 2):
        super().__init__(parent)
        self.items: list[Transfer] = []
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_parallel)
        self._max = max_parallel
        self._closing = False

    def count(self, state: str) -> int:
        return sum(t.state == state for t in self.items)

    def busy(self) -> bool:
        return self.count(RUNNING) > 0

    def enqueue(self, kind: str, title: str, dest: str, run: Callable[[], None],
                on_done: Callable[[], None] | None = None) -> Transfer:
        t = Transfer(kind, title, dest, run, on_done)
        self.items.append(t)
        self.added.emit(t)
        self._pump()
        return t

    def retry(self, t: Transfer) -> None:
        if t.state in (ERROR, CANCELLED):
            t.state, t.message = QUEUED, ""
            self.changed.emit(t)
            self._pump()

    def cancel(self, t: Transfer) -> None:
        """Cancel a queued one (ones already running cannot be interrupted)."""
        if t.state == QUEUED:
            t.state = CANCELLED
            self.changed.emit(t)
            self._emit_activity()

    def remove(self, t: Transfer) -> None:
        if t.state == QUEUED:
            self.cancel(t)
        if t.state != RUNNING and t in self.items:
            self.items.remove(t)
            self.removed.emit(t)

    def clear_finished(self) -> None:
        for t in [t for t in self.items if t.state in (DONE, ERROR, CANCELLED)]:
            self.remove(t)

    def shutdown(self, timeout_ms: int = 15000) -> bool:
        """Cancel what is pending and wait for what is running."""
        self._closing = True
        for t in self.items:
            self.cancel(t)
        return self._pool.waitForDone(timeout_ms)

    # -- internals ---------------------------------------------------------
    def _emit_activity(self) -> None:
        self.activity.emit(self.count(RUNNING), self.count(QUEUED))

    def _pump(self) -> None:
        if self._closing:
            return
        for t in self.items:
            if self.count(RUNNING) >= self._max:
                break
            if t.state == QUEUED:
                t.state = RUNNING
                self.changed.emit(t)
                run_async(t.run, on_result=lambda _, t=t: self._finish(t, None),
                          on_error=lambda e, t=t: self._finish(t, e),
                          parent=self, pool=self._pool)
        self._emit_activity()

    def _finish(self, t: Transfer, err: Exception | None) -> None:
        if err is None:
            t.state, t.message = DONE, ""
        else:
            t.state, t.message = ERROR, str(err) or err.__class__.__name__
            if isinstance(err, NotLoggedIn):
                t.message = "Not signed in"
                self.session_lost.emit()
        self.changed.emit(t)
        if err is None and t.on_done:
            try:
                t.on_done()
            except Exception as e:  # noqa: BLE001 - never break the queue
                t.state, t.message = ERROR, str(e)
                self.changed.emit(t)
        self._pump()


_VERB = {"upload": "Uploading…", "download": "Downloading…", "open": "Preparing…"}


class _Row(QFrame):
    """Card for one transfer: icon, name and destination, bar/status and buttons."""

    def __init__(self, t: Transfer, mgr: TransferManager):
        super().__init__()
        self.t, self.mgr = t, mgr
        self.setObjectName("transfer")
        up = t.kind == "upload"
        self.tile = Tile("upload" if up else "download", "accent", 38, 20)
        self.name = QLabel(t.title)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name.setFont(font_with(self.name.font(), 10.5, QFont.Weight.DemiBold))
        self.dest = QLabel(("To " if up else "In ") + t.dest)
        theme.set_props(self.dest, role="caption")
        self.dest.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status = QLabel()
        self.status.setFixedWidth(220)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bar = ActivityBar()
        self.bar.setRange(0, 0)
        self.bar.setFixedSize(96, 6)
        self.retry = QToolButton()
        theme.set_props(self.retry, variant="icon")
        self.retry.setIcon(theme.icon("refresh"))
        self.retry.setToolTip("Retry")
        self.retry.clicked.connect(lambda: mgr.retry(t))
        self.close_btn = QToolButton()
        theme.set_props(self.close_btn, variant="icon")
        self.close_btn.setIcon(theme.icon("close"))
        self.close_btn.clicked.connect(lambda: mgr.remove(t))
        texts = QVBoxLayout()
        texts.setSpacing(1)
        texts.addWidget(self.name)
        texts.addWidget(self.dest)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 8, 9)
        lay.setSpacing(12)
        lay.addWidget(self.tile)
        lay.addLayout(texts, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.retry)
        lay.addWidget(self.close_btn)
        for w in (self.bar, self.retry, self.close_btn):  # columns aligned across rows
            sp = w.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            w.setSizePolicy(sp)
        self.refresh()

    def refresh(self) -> None:
        t = self.t
        text = {QUEUED: "Queued", RUNNING: _VERB.get(t.kind, "Working…"),
                DONE: "Completed", CANCELLED: "Cancelled",
                ERROR: "Error: " + t.message}[t.state]
        fm = self.status.fontMetrics()
        self.status.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 216))
        self.status.setToolTip(text if t.state == ERROR else "")
        tone = {DONE: "success", ERROR: "danger", CANCELLED: "warning"}.get(t.state)
        theme.set_props(self.status, role={DONE: "ok", ERROR: "error"}.get(t.state, "muted"))
        up = t.kind == "upload"
        if t.state == DONE:
            self.tile.set_icon("check", "success")
        elif t.state == ERROR:
            self.tile.set_icon("error", "danger")
        elif t.state == CANCELLED:
            self.tile.set_icon("close", "warning")
        else:
            self.tile.set_icon("upload" if up else "download", "accent")
        self.bar.setVisible(t.state == RUNNING)
        self.retry.setVisible(t.state in (ERROR, CANCELLED))
        self.close_btn.setVisible(t.state != RUNNING)
        self.close_btn.setToolTip("Cancel" if t.state == QUEUED else "Remove from list")


class TransferPanel(QWidget):
    """List of transfers (cards) with an empty state and a button to clear finished ones."""

    def __init__(self, mgr: TransferManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.mgr = mgr
        self.rows: dict[int, tuple[QListWidgetItem, _Row]] = {}
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setSpacing(4)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setContentsMargins(0, 0, 0, 0)
        empty = QLabel("No transfers.\nUploads and downloads will appear here.")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.set_props(empty, role="muted")
        self.stack = QStackedWidget()
        self.stack.addWidget(empty)
        self.stack.addWidget(self.list)
        self.clear_btn = QPushButton("Clear finished")
        theme.set_props(self.clear_btn, variant="ghost")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(mgr.clear_finished)
        foot = QHBoxLayout()
        foot.setContentsMargins(16, 0, 16, 8)
        foot.addStretch(1)
        foot.addWidget(self.clear_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(0)
        lay.addWidget(self.stack, 1)
        lay.addLayout(foot)
        mgr.added.connect(self._add)
        mgr.changed.connect(self._update)
        mgr.removed.connect(self._remove)
        for t in mgr.items:
            self._add(t)
        self._sync()

    def _add(self, t: Transfer) -> None:
        row = _Row(t, self.mgr)
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint() + QSize(0, 2))
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        self.rows[t.id] = (item, row)
        self.list.scrollToBottom()
        self._sync()

    def _update(self, t: Transfer) -> None:
        if t.id in self.rows:
            self.rows[t.id][1].refresh()
        self._sync()

    def _remove(self, t: Transfer) -> None:
        item, _ = self.rows.pop(t.id, (None, None))
        if item is not None:
            self.list.takeItem(self.list.row(item))
        self._sync()

    def _sync(self) -> None:
        self.stack.setCurrentIndex(1 if self.rows else 0)
        self.clear_btn.setEnabled(any(t.state in (DONE, ERROR, CANCELLED) for t in self.mgr.items))


class ConflictDialog(QDialog):
    """\"Already exists\": Replace / Keep both / Skip, with \"apply to all\"."""

    def __init__(self, name: str, where: str, remaining: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Item already exists")
        self.choice = ""
        title = QLabel("This item already exists")
        theme.set_props(title, role="title")
        text = QLabel(f'"{name}" is already in {where}.\nWhat do you want to do with the new one?')
        text.setWordWrap(True)
        theme.set_props(text, role="muted")
        self.all = QCheckBox(f"Apply to the remaining {remaining}" if remaining > 1 else "Apply to all")
        self.all.setVisible(remaining > 1)
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(Tile("warning", "warning", 44, 24), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(title)
        col.addWidget(text)
        top.addLayout(col, 1)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch(1)
        for label, key in (("Skip", "skip"), ("Replace", "replace"), ("Keep both", "rename")):
            b = QPushButton(label)
            theme.set_props(b, variant="primary" if key == "rename" else "secondary")
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            b.setDefault(key == "rename")
            btns.addWidget(b)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(16)
        lay.addLayout(top)
        lay.addWidget(self.all)
        lay.addLayout(btns)
        self.setMinimumWidth(500)

    def _pick(self, key: str) -> None:
        self.choice = key
        self.accept()


def resolve_conflicts(parent: QWidget | None, conflicts: list[str], where: str,
                      default: str = "ask") -> dict[str, str] | None:
    """Strategy per conflicting name (replace/rename/skip). None = operation cancelled."""
    if default != "ask":
        return {n: default for n in conflicts}
    out: dict[str, str] = {}
    for i, n in enumerate(conflicts):
        dlg = ConflictDialog(n, where, len(conflicts) - i, parent)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        if dlg.all.isChecked():
            return {**out, **{m: dlg.choice for m in conflicts[i:]}}
        out[n] = dlg.choice
    return out
