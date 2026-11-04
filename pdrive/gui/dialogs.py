"""pdrive dialogs: share, properties and settings (PySide6).

Every ``Client`` access runs in threads (``_async``); the UI never blocks.
It does not depend on window.py or workers.py.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (QDate, QDateTime, QEvent, QLocale, QMimeDatabase,
                            QObject, QRunnable, Qt, QThreadPool, Signal, Slot)
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from .. import config
from ..client import DriveError, Entry, NotLoggedIn
from . import theme
from .widgets import Tile

__all__ = ["ShareDialog", "PropertiesDialog", "SettingsDialog",
           "save_user_config", "copy_text", "parse_emails"]

# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------
_LIVE: set[Any] = set()          # keeps the GC from destroying in-flight signals
_LIVE_LOCK = threading.Lock()


class _Signals(QObject):
    """Lives in the GUI thread: its slots always run there (queued)."""

    done = Signal(object)
    failed = Signal(object)

    def __init__(self, on_result: Callable | None, on_error: Callable | None):
        super().__init__()
        self._ok, self._bad = on_result, on_error
        self.done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)
        self.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)

    def _release(self) -> None:
        with _LIVE_LOCK:
            _LIVE.discard(self)

    @Slot(object)
    def _on_done(self, value: object) -> None:
        self._release()
        if self._ok:
            self._ok(value)

    @Slot(object)
    def _on_failed(self, exc: object) -> None:
        self._release()
        if self._bad:
            self._bad(exc)


class _Task(QRunnable):
    def __init__(self, fn: Callable, args: tuple, sig: _Signals):
        super().__init__()
        self.fn, self.args, self.sig = fn, args, sig

    def run(self) -> None:
        try:
            res = self.fn(*self.args)
        except BaseException as e:  # noqa: BLE001 - delivered to the GUI
            self.sig.failed.emit(e)
        else:
            self.sig.done.emit(res)


def _async(fn: Callable, *args: Any, on_result: Callable | None = None,
           on_error: Callable | None = None) -> None:
    """Run ``fn(*args)`` in the QThreadPool; callbacks run in the GUI thread."""
    sig = _Signals(on_result, on_error)
    with _LIVE_LOCK:
        _LIVE.add(sig)
    QThreadPool.globalInstance().start(_Task(fn, args, sig))


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^\s@,;<>\"'()\[\]\\-][^\s@,;<>\"'()\[\]\\]*@"
                       r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_ROLE_NAMES = {"viewer": "Viewer", "editor": "Editor", "admin": "Admin"}
_ROLE_ALIASES = {"viewer": "viewer", "reader": "viewer", "view": "viewer",
                 "editor": "editor", "edit": "editor", "writer": "editor",
                 "admin": "admin", "administrator": "admin", "owner": "admin"}


def parse_emails(text: str) -> tuple[list[str], list[str]]:
    """Split on commas/whitespace/;: return (valid without duplicates, invalid).

    An email cannot start with '-' (keeps the CLI from mistaking it for an option).
    """
    ok: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for tok in re.split(r"[,;\s]+", text.strip()):
        if not tok:
            continue
        if len(tok) > 254 or not _EMAIL_RE.match(tok):
            if tok not in bad:
                bad.append(tok)
        elif tok.lower() not in seen:
            seen.add(tok.lower())
            ok.append(tok)
    return ok, bad


def _norm_role(raw: object) -> str | None:
    return _ROLE_ALIASES.get(raw.strip().lower()) if isinstance(raw, str) else None


def _role_label(raw: object) -> str:
    r = _norm_role(raw)
    if r:
        return _ROLE_NAMES[r]
    return raw.strip() if isinstance(raw, str) and raw.strip() else "—"


def _people(raw: object) -> list[tuple[str, str]]:
    """Tolerant list of (email, role) from sharing_status."""
    out: list[tuple[str, str]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for it in raw:
        if isinstance(it, str) and it.strip():
            out.append((it.strip(), ""))
        elif isinstance(it, dict):
            email = it.get("email") or it.get("inviteeEmail") or it.get("name")
            if isinstance(email, str) and email.strip():
                role = it.get("role")
                out.append((email.strip(), role if isinstance(role, str) else ""))
    return out


def _fmt_date(raw: object) -> str:
    """Readable date (YYYY-MM-DD) from a tolerant expiration value."""
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return datetime.fromtimestamp(raw).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return str(raw)
    return raw.strip()[:10] if isinstance(raw, str) and raw.strip() else ""


def _err_text(e: object) -> str:
    if isinstance(e, NotLoggedIn):
        return "Your session has expired. Sign in again from the main window."
    msg = str(e).strip()
    return msg or type(e).__name__


def _error_color(pal: QPalette) -> QColor:
    """Theme error color: BrightText (Breeze defines it as red) or an adapted red."""
    c = pal.color(QPalette.ColorRole.BrightText)
    if c.red() > 120 and c.red() > c.green() * 1.4 and c.red() > c.blue() * 1.4:
        return c
    dark = pal.color(QPalette.ColorRole.Window).lightness() < 128
    return QColor.fromHsl(0, 200, 150 if dark else 80)


class _ErrorLabel(QLabel):
    """Error notice: critical icon + text in the theme error color."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setAccessibleName("Error")
        self._text = ""
        theme.set_props(self, role="alert")
        self.setMinimumHeight(0)
        self.hide()

    def show_error(self, msg: str) -> None:
        self._text = msg
        self.setText(msg)
        self.setVisible(bool(msg))

    def clear_error(self) -> None:
        self.show_error("")


class _AsyncDialog(QDialog):
    """Base: pending-operation counter and callbacks that are safe after closing."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._closed = False
        self._pending = 0
        self.destroyed.connect(self._mark_closed)

    def _mark_closed(self, *_: object) -> None:
        self._closed = True

    def done(self, r: int) -> None:  # noqa: D102 - override Qt
        self._closed = True
        super().done(r)

    def showEvent(self, ev: Any) -> None:  # noqa: D102 - reopen after closing
        self._closed = False
        super().showEvent(ev)

    def _busy(self) -> bool:
        return self._pending > 0

    def _on_busy_changed(self) -> None:
        """Subclasses re-evaluate their buttons."""

    def _run(self, fn: Callable, *args: Any, ok: Callable | None = None,
             err: Callable | None = None) -> None:
        """Run fn in a thread; ok/err are only called if the dialog is still alive."""
        self._pending += 1
        self._on_busy_changed()

        def good(v: object) -> None:
            if self._closed:
                return
            self._pending -= 1
            try:
                if ok:
                    ok(v)
            finally:
                self._on_busy_changed()

        def bad(e: object) -> None:
            if self._closed:
                return
            self._pending -= 1
            try:
                (err or self._fail)(e)
            finally:
                self._on_busy_changed()

        _async(fn, *args, on_result=good, on_error=bad)

    def _fail(self, e: object) -> None:  # overridden by subclasses that have a label
        pass


# --------------------------------------------------------------------------
# Share
# --------------------------------------------------------------------------
class ShareDialog(_AsyncDialog):
    """Public link (role, password, expiration) and people with access."""

    def __init__(self, client: Any, remote: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.client, self.remote = client, remote
        self._url: str | None = None
        self._has_link = False
        self.setWindowTitle(f"Share — {remote.rstrip('/').rsplit('/', 1)[-1] or remote}")
        self.setMinimumWidth(620)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(Tile("share", "accent", 44, 24), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel('Share "' + (remote.rstrip('/').rsplit('/', 1)[-1] or remote) + '"')
        theme.set_props(title, role="title")
        path = QLabel(remote)
        theme.set_props(path, role="caption")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        col.addWidget(title)
        col.addWidget(path)
        head.addLayout(col, 1)
        root.addLayout(head)

        # -- public link
        box = QGroupBox("Public link")
        form = QFormLayout(box)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(11)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.link_state = QLabel("Loading…")
        self.link_state.setWordWrap(True)
        form.addRow("Status:", self.link_state)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("No link")
        self.copy_btn = QPushButton(theme.icon("copy"), "Copy")
        self.copy_btn.clicked.connect(self._copy_url)
        row = QHBoxLayout()
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.copy_btn)
        form.addRow("Link:", row)
        self.role_combo = QComboBox()
        self.role_combo.addItem("Viewer", "viewer")
        self.role_combo.addItem("Editor", "editor")
        form.addRow("Permission:", self.role_combo)
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit.setPlaceholderText("No password")
        self.pw_show = QCheckBox("Show")
        self.pw_show.toggled.connect(
            lambda on: self.pw_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row = QHBoxLayout()
        row.addWidget(self.pw_edit, 1)
        row.addWidget(self.pw_show)
        form.addRow("Password:", row)
        self.no_expiry = QCheckBox("No expiration")
        self.no_expiry.setChecked(True)
        self.expiry = QDateEdit(QDate.currentDate().addDays(7))
        self.expiry.setCalendarPopup(True)
        self.expiry.setMinimumDate(QDate.currentDate())
        self.expiry.setDisplayFormat("yyyy-MM-dd")
        self.expiry.setEnabled(False)
        self.no_expiry.toggled.connect(lambda on: self.expiry.setEnabled(not on and not self._busy()))
        row = QHBoxLayout()
        row.addWidget(self.no_expiry)
        row.addWidget(self.expiry, 1)
        form.addRow("Expiration:", row)
        self.link_btn = QPushButton(theme.icon("link", "onaccent"), "Create link")
        theme.set_props(self.link_btn, variant="primary")
        self.link_btn.clicked.connect(self._make_link)
        self.unlink_btn = QPushButton(theme.icon("trash", "danger"), "Remove link")
        theme.set_props(self.unlink_btn, variant="dangerghost")
        self.unlink_btn.clicked.connect(self._remove_link)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.link_btn)
        row.addWidget(self.unlink_btn)
        form.addRow(row)
        root.addWidget(box)

        # -- people
        pbox = QGroupBox("People")
        pv = QVBoxLayout(pbox)
        pv.setSpacing(10)
        self.people = QTreeWidget()
        self.people.setHeaderLabels(["Email", "Permission", "Status"])
        self.people.setRootIsDecorated(False)
        self.people.setAlternatingRowColors(False)
        self.people.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.people.setMinimumHeight(110)
        self.people.itemSelectionChanged.connect(self._on_busy_changed)
        pv.addWidget(self.people)
        row = QHBoxLayout()
        self.remove_btn = QPushButton(theme.icon("close", "danger"), "Remove access")
        theme.set_props(self.remove_btn, variant="dangerghost")
        self.remove_btn.clicked.connect(self._remove_access)
        row.addStretch(1)
        row.addWidget(self.remove_btn)
        pv.addLayout(row)
        row = QHBoxLayout()
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("name@example.com, other@example.com")
        self.email_edit.setClearButtonEnabled(True)
        self.email_edit.returnPressed.connect(self._invite)
        self.invite_role = QComboBox()
        for k in ("viewer", "editor", "admin"):
            self.invite_role.addItem(_ROLE_NAMES[k], k)
        self.invite_btn = QPushButton(theme.icon("plus", "onaccent"), "Invite")
        theme.set_props(self.invite_btn, variant="primary")
        self.invite_btn.clicked.connect(self._invite)
        row.addWidget(self.email_edit, 1)
        row.addWidget(self.invite_role)
        row.addWidget(self.invite_btn)
        pv.addLayout(row)
        root.addWidget(pbox)

        self.note = QLabel()
        self.note.setWordWrap(True)
        theme.set_props(self.note, role="note")
        self.note.hide()
        self.error = _ErrorLabel()
        root.addWidget(self.note)
        root.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Close).setText("Close")
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        for lay in self.findChildren(QHBoxLayout):   # comfortable spacing between fields and buttons in a row
            lay.setSpacing(10)

        self._on_busy_changed()
        self._refresh()

    # -- state -----------------------------------------------------------
    def _on_busy_changed(self) -> None:
        idle = not self._busy()
        for w in (self.role_combo, self.pw_edit, self.no_expiry, self.email_edit,
                  self.invite_role, self.link_btn, self.invite_btn):
            w.setEnabled(idle)
        self.expiry.setEnabled(idle and not self.no_expiry.isChecked())
        self.unlink_btn.setEnabled(idle and self._has_link)
        self.copy_btn.setEnabled(bool(self.url_edit.text()))
        self.remove_btn.setEnabled(idle and bool(self.people.selectedItems()))

    def _fail(self, e: object) -> None:
        self.note.hide()
        self.error.show_error(_err_text(e))

    def _info(self, msg: str) -> None:
        self.error.clear_error()
        self.note.setText(msg)
        self.note.show()

    def _refresh(self) -> None:
        self._run(self.client.sharing_status, self.remote, ok=self._apply_status,
                  err=self._status_failed)

    def _status_failed(self, e: object) -> None:
        self.link_state.setText("Could not load the status")
        self._fail(e)

    def _apply_status(self, st: object) -> None:
        st = st if isinstance(st, dict) else {}
        url = st.get("url") if isinstance(st.get("url"), str) and st.get("url") else None
        url = url or self._url
        self._url = url
        self._has_link = bool(url)
        self.url_edit.setText(url or "")
        self.url_edit.setCursorPosition(0)
        bits = ["Active" if url else "No public link"]
        if url:
            if st.get("has_password"):
                bits.append("password protected")
            exp = _fmt_date(st.get("expiration"))
            bits.append(f"expires on {exp}" if exp else "does not expire")
        self.link_state.setText(", ".join(bits))
        self.link_btn.setText("Update link" if url else "Create link")
        self.pw_edit.setPlaceholderText(
            "Type a new one to change it" if st.get("has_password") and url
            else "No password")
        r = _norm_role(st.get("role"))
        if url and r in ("viewer", "editor"):
            self.role_combo.setCurrentIndex(self.role_combo.findData(r))
        exp_d = QDate.fromString(_fmt_date(st.get("expiration")), "yyyy-MM-dd")
        if url and exp_d.isValid():
            self.expiry.setMinimumDate(min(exp_d, QDate.currentDate()))
            self.expiry.setDate(exp_d)
            self.no_expiry.setChecked(False)
        elif url:
            self.no_expiry.setChecked(True)

        self.people.clear()
        for rows, state in ((_people(st.get("members")), "Member"),
                            (_people(st.get("invitations")), "Pending invitation")):
            for email, role in rows:
                self.people.addTopLevelItem(
                    QTreeWidgetItem([email, _role_label(role), state]))
        for c in range(3):
            self.people.resizeColumnToContents(c)

    # -- link -----------------------------------------------------------
    def _copy_url(self) -> None:
        if self.url_edit.text():
            copy_text(self.url_edit.text())
            self._info("Link copied to the clipboard.")

    def _make_link(self) -> None:
        pw = self.pw_edit.text() or None
        exp = None if self.no_expiry.isChecked() else self.expiry.date().toString("yyyy-MM-dd")
        role = self.role_combo.currentData() or "viewer"
        self.error.clear_error()

        def ok(url: object) -> None:
            if isinstance(url, str) and url:
                self._url = url
            self.pw_edit.clear()
            self._info("Link updated." if self._has_link else "Link created.")
            self._refresh()

        self._run(self.client.link, self.remote, pw, exp, role, ok=ok)

    def _confirm(self, text: str) -> bool:
        """Modal confirmation (overridable in tests)."""
        from .widgets import confirm
        return confirm(self, "Confirm", text, "Remove", danger=True)

    def _remove_link(self) -> None:
        if not self._confirm("Remove the public link? Anyone who has it will lose access."):
            return
        self.error.clear_error()

        def ok(_: object) -> None:
            self._url = None
            self._has_link = False
            self._info("Link removed.")
            self._refresh()

        self._run(self.client.unlink, self.remote, ok=ok)

    # -- people ---------------------------------------------------------
    def _invite(self) -> None:
        if self._busy():
            return
        emails, bad = parse_emails(self.email_edit.text())
        if bad:
            self.note.hide()
            self.error.show_error("Invalid email: " + ", ".join(bad))
            return
        if not emails:
            self.note.hide()
            self.error.show_error("Enter at least one email.")
            return
        role = self.invite_role.currentData() or "viewer"
        self.error.clear_error()

        def ok(_: object) -> None:
            self.email_edit.clear()
            self._info(f"Invitation sent to {', '.join(emails)}.")
            self._refresh()

        self._run(self.client.invite, self.remote, emails, role, ok=ok)

    def _remove_access(self) -> None:
        sel = self.people.selectedItems()
        if not sel or self._busy():
            return
        email = sel[0].text(0)
        if not self._confirm(f"Remove access for {email}?"):
            return
        self.error.clear_error()

        def ok(_: object) -> None:
            self._info(f"Access removed for {email}.")
            self._refresh()

        self._run(self.client.remove_access, self.remote, [email], ok=ok)


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------
class PropertiesDialog(_AsyncDialog):
    """Item details; folder size and sharing status are fetched in a thread."""

    def __init__(self, client: Any, entry: Entry, remote_path: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.client, self.entry, self.remote = client, entry, remote_path
        self.setWindowTitle(f"Properties — {entry.name}")
        self.setMinimumWidth(460)

        def val(text: str) -> QLabel:
            lb = QLabel(text)
            lb.setWordWrap(True)
            lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            return lb

        def key(text: str) -> QLabel:
            lb = QLabel(text)
            theme.set_props(lb, role="muted")
            return lb

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.path_lbl = val(remote_path)
        form.addRow(key("Path"), self.path_lbl)
        mime = ""
        if not entry.is_dir:
            mime = QMimeDatabase().mimeTypeForFile(
                entry.name, QMimeDatabase.MatchMode.MatchExtension).comment()
        form.addRow(key("Type"), val("Folder" if entry.is_dir else (mime or "File")))
        self.size_lbl = val("—" if entry.is_dir else self._fmt_size(entry.size))
        row = QHBoxLayout()
        row.addWidget(self.size_lbl, 1)
        self.size_btn = QPushButton("Calculate size")
        self.size_btn.clicked.connect(self._calc_size)
        self.size_btn.setVisible(entry.is_dir)
        row.addWidget(self.size_btn)
        form.addRow(key("Size"), row)
        mt = (QLocale().toString(QDateTime.fromSecsSinceEpoch(int(entry.mtime)),
                                 QLocale.FormatType.LongFormat)
              if entry.mtime and entry.mtime > 0 else "—")
        form.addRow(key("Modified"), val(mt))
        if not entry.is_dir:
            form.addRow(key("SHA-1"), val(entry.sha1 or "—"))
        self.share_lbl = val("Checking…" if not entry.shared_by_url
                             else "Public link active; checking…")
        form.addRow(key("Sharing"), self.share_lbl)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(16)
        cat = theme.category(entry.name, entry.is_dir)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(Tile(theme._CAT_ICON[cat], "accent", 48, 26, color=f"cat:{cat}"), 0,
                       Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        name_lbl = QLabel(entry.name)
        theme.set_props(name_lbl, role="title")
        name_lbl.setWordWrap(True)
        name_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        kind_lbl = QLabel(theme.kind_label(entry.name, entry.is_dir))
        theme.set_props(kind_lbl, role="caption")
        col.addWidget(name_lbl)
        col.addWidget(kind_lbl)
        head.addLayout(col, 1)
        root.addLayout(head)
        root.addLayout(form)
        root.addStretch(1)
        self.error = _ErrorLabel()
        root.addWidget(self.error)
        bb = QDialogButtonBox()
        copy_btn = bb.addButton("Copy path", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(lambda: copy_text(self.remote))
        bb.addButton(QDialogButtonBox.StandardButton.Close).setText("Close")
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._run(client.sharing_status, remote_path, ok=self._apply_share,
                  err=self._share_failed)
        if entry.is_dir:
            self._calc_size()

    @staticmethod
    def _fmt_size(n: int) -> str:
        return f"{QLocale().formattedDataSize(n)} ({n:,} bytes)" if n >= 1024 else f"{n} bytes"

    def _on_busy_changed(self) -> None:
        self.size_btn.setEnabled(not self._sizing)

    _sizing = False

    def _calc_size(self) -> None:
        if self._sizing:
            return
        self._sizing = True
        self.size_lbl.setText("Calculating…")
        self.size_btn.setEnabled(False)

        def ok(n: object) -> None:
            self._sizing = False
            self.size_btn.setText("Recalculate")
            self.size_lbl.setText(self._fmt_size(int(n)) if isinstance(n, (int, float)) else "—")

        def bad(e: object) -> None:
            self._sizing = False
            self.size_lbl.setText("—")
            self.error.show_error("Could not calculate the size: " + _err_text(e))

        self._run(self.client.size, self.remote, ok=ok, err=bad)

    def _share_failed(self, e: object) -> None:
        self.share_lbl.setText("Public link active" if self.entry.shared_by_url else "Unknown")
        self.error.show_error("Could not load the sharing status: " + _err_text(e))

    def _apply_share(self, st: object) -> None:
        st = st if isinstance(st, dict) else {}
        parts: list[str] = []
        url = st.get("url")
        if url or self.entry.shared_by_url:
            bits = ["Public link active"]
            r = _norm_role(st.get("role"))
            if r:
                bits.append(_ROLE_NAMES[r].lower())
            if st.get("has_password"):
                bits.append("password protected")
            exp = _fmt_date(st.get("expiration"))
            if exp:
                bits.append(f"expires on {exp}")
            parts.append(", ".join(bits))
        people = _people(st.get("members")) + _people(st.get("invitations"))
        if people:
            parts.append(f"Shared with {len(people)} "
                         + ("person" if len(people) == 1 else "people")
                         + ": " + ", ".join(e for e, _ in people))
        self.share_lbl.setText("\n".join(parts) or "Not shared")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
_CONFLICTS = (("ask", "Ask"), ("rename", "Keep both"),
              ("replace", "Replace"), ("skip", "Skip"))
_USER_KEYS = ("upload_dir", "on_conflict", "confirm_trash", "disk_cache")


class SettingsDialog(QDialog):
    """Editable settings; ``result_cfg()`` returns the resulting config."""

    def __init__(self, cfg: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = dict(cfg)
        self.setWindowTitle("Proton Drive settings")
        self.setMinimumWidth(480)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.upload_dir = QLineEdit(str(cfg.get("upload_dir") or ""))
        form.addRow("Default upload folder:", self.upload_dir)
        self.conflict = QComboBox()
        for k, label in _CONFLICTS:
            self.conflict.addItem(label, k)
        i = self.conflict.findData(cfg.get("on_conflict") or "ask")
        self.conflict.setCurrentIndex(max(i, 0))
        form.addRow("If the file already exists:", self.conflict)
        self.confirm_trash = QCheckBox("Confirm before moving to Trash")
        self.confirm_trash.setChecked(bool(cfg.get("confirm_trash", True)))
        form.addRow("", self.confirm_trash)
        self.disk_cache = QCheckBox("Keep folder cache on disk (faster startup)")
        self.disk_cache.setChecked(bool(cfg.get("disk_cache", False)))
        self.disk_cache.setToolTip("Stores the names of your folders and files in ~/.cache/pdrive "
                                   "UNENCRYPTED. Leave it off on shared computers.")
        form.addRow("", self.disk_cache)
        self.error = _ErrorLabel()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        theme.set_props(bb.button(QDialogButtonBox.StandardButton.Ok), variant="primary")
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(16)
        title = QLabel("Settings")
        theme.set_props(title, role="title")
        root.addWidget(title)
        root.addLayout(form)
        root.addWidget(self.error)
        root.addWidget(bb)

    @staticmethod
    def _remote_ok(p: str) -> bool:
        root = config.REMOTE_ROOT
        return (p == root or p.startswith(root + "/")) and ".." not in p.split("/")

    def accept(self) -> None:  # noqa: D102 - validate before accepting
        up = self.upload_dir.text().strip().rstrip("/")
        if not self._remote_ok(up):
            self.error.show_error(
                f"The upload folder must be inside {config.REMOTE_ROOT}.")
            return
        self.upload_dir.setText(up)
        super().accept()

    def result_cfg(self) -> dict:
        """Copy of the original config with the edited values."""
        out = dict(self._cfg)
        out.update(
            upload_dir=self.upload_dir.text().strip().rstrip("/"),
            on_conflict=self.conflict.currentData() or "ask",
            confirm_trash=self.confirm_trash.isChecked(),
            disk_cache=self.disk_cache.isChecked(),
        )
        return out


def _toml_str(s: str) -> str:
    """Valid basic TOML string (json escapes \" \\ and control characters; DEL separately)."""
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def save_user_config(cfg: dict) -> Path:
    """Write config.toml (known keys only) atomically; return the path.

    Directory: PDRIVE_CONFIG_DIR if set in the environment, otherwise config.CONFIG_DIR.
    """
    d = Path(os.environ.get("PDRIVE_CONFIG_DIR") or config.CONFIG_DIR)
    d.mkdir(parents=True, exist_ok=True)
    lines = ["# Generated by pdrive (Settings)"]
    for k in _USER_KEYS:
        v = cfg.get(k)
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (str, os.PathLike)) and str(v) != "":
            lines.append(f"{k} = {_toml_str(os.fspath(v))}")
    path = d / "config.toml"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def copy_text(text: str) -> None:
    """Copy text to the Qt clipboard."""
    cb = QGuiApplication.clipboard()
    if cb is not None:
        cb.setText(text)
