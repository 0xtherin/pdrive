"""Reusable custom visual pieces: row delegate, flat header, activity bar,
icon labels, chips and confirmation/prompt dialogs in the theme's visual language."""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QProgressBar, QPushButton, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QVBoxLayout, QWidget)

from . import theme

ROW_H = 48          # list row height
MX = 8              # side margin of the row "pill"
TILE = 34           # side of the icon with a category background


def font_with(base: QFont, pt: float | None = None, weight: QFont.Weight | None = None) -> QFont:
    f = QFont(base)
    if pt:
        f.setPointSizeF(pt)
    if weight is not None:
        f.setWeight(weight)
    return f


# ------------------------------------------------------- icon labels

class IconLabel(QLabel):
    """QLabel with its own icon that repaints when the theme changes."""

    def __init__(self, name: str = "", size: int = 20, color: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._name, self._size, self._color = name, size, color
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.signals.changed.connect(self._render)
        self._render()

    def set_icon(self, name: str, color: str | None = None, size: int | None = None) -> None:
        self._name, self._color = name, color
        if size:
            self._size = size
            self.setFixedSize(size, size)
        self._render()

    def _render(self) -> None:
        if self._name:
            self.setPixmap(theme.pixmap(self._name, self._size, self._color))
        else:
            self.clear()


class Tile(QFrame):
    """Rounded box with a centered icon; ``tone`` colors the background (accent|danger|success|warning)."""

    def __init__(self, name: str, tone: str = "accent", size: int = 40, icon_size: int = 22,
                 color: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setFixedSize(size, size)
        self.label = IconLabel(name, icon_size, color or {"accent": "accent_text"}.get(tone, tone))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label, 0, Qt.AlignmentFlag.AlignCenter)
        theme.set_props(self, tone=tone)

    def set_icon(self, name: str, tone: str, color: str | None = None) -> None:
        theme.set_props(self, tone=tone)
        self.label.set_icon(name, color or {"accent": "accent_text"}.get(tone, tone))


def chip(text: str, tone: str = "neutral") -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("chip")
    theme.set_props(lb, tone=tone)
    return lb


# ---------------------------------------------------------------- progress

class ActivityBar(QProgressBar):
    """Thin rounded bar. With range (0, 0) it animates a segment (only while visible)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setTextVisible(False)

    def _indeterminate(self) -> bool:
        return self.maximum() == self.minimum()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.02) % 1.0
        self.update()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._indeterminate():
            self._timer.start()

    def hideEvent(self, ev) -> None:
        super().hideEvent(ev)
        self._timer.stop()

    def paintEvent(self, ev) -> None:
        t = theme.tokens()
        r = QRectF(self.rect())
        h = min(r.height(), 6.0)
        r = QRectF(r.x(), r.center().y() - h / 2, r.width(), h)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.surface3))
        p.drawRoundedRect(r, h / 2, h / 2)
        p.setBrush(QColor(t.accent))
        if self._indeterminate():
            seg = r.width() * 0.38
            x = r.x() - seg + (r.width() + seg) * self._phase
            clip = QRectF(max(x, r.x()), r.y(), min(x + seg, r.right()) - max(x, r.x()), r.height())
            if clip.width() > 0:
                p.drawRoundedRect(clip, h / 2, h / 2)
        else:
            span = max(1, self.maximum() - self.minimum())
            frac = (self.value() - self.minimum()) / span
            if frac > 0:
                p.drawRoundedRect(QRectF(r.x(), r.y(), max(h, r.width() * frac), h), h / 2, h / 2)


# --------------------------------------------------- file list

class FlatHeader(QHeaderView):
    """Boxless header: muted text, thin bottom line and a sort chevron."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setFixedHeight(38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), 38)

    def paintSection(self, painter: QPainter, rect: QRect, logical: int) -> None:
        if not rect.isValid():
            return
        t = theme.tokens()
        last = self.visualIndex(logical) == self.count() - 1
        first = self.visualIndex(logical) == 0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = str(self.model().headerData(logical, Qt.Orientation.Horizontal) or "").strip("  ")
        align = self.model().headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole)
        right = bool(align) and bool(int(align) & int(Qt.AlignmentFlag.AlignRight))
        painter.setPen(QPen(QColor(t.border), 1))
        painter.drawLine(rect.left() + (MX if first else 0), rect.bottom(),
                         rect.right() - (MX if last else 0), rect.bottom())
        sorted_here = self.isSortIndicatorShown() and self.sortIndicatorSection() == logical
        f = font_with(self.font(), 9.0, QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QColor(t.accent_text if sorted_here else t.text2))
        pad_l = MX + 12 if first else 10
        inner = rect.adjusted(pad_l, 0, -(MX + 12) if last else -12, 0)
        fm = QFontMetrics(f)
        tw = fm.horizontalAdvance(text)
        ic = QRect(0, 0, 12, 12)
        if right:
            x = inner.right() - tw
            if sorted_here:
                x -= 16
            painter.drawText(QRect(x, inner.y(), tw + 2, inner.height()), Qt.AlignmentFlag.AlignVCenter, text)
            ic.moveTo(x + tw + 4, inner.center().y() - 6)
        else:
            painter.drawText(inner, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            ic.moveTo(inner.x() + tw + 4, inner.center().y() - 6)
        if sorted_here:
            name = "chevron-up" if self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder else "chevron-down"
            theme.icon(name, "accent_text").paint(painter, ic)
        painter.restore()


class RowDelegate(QStyledItemDelegate):
    """48 px row: icon with category background, name + type, size, date and link chip."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(40, ROW_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        from .window import DriveModel   # late import: window imports this module
        t = theme.tokens()
        e = index.data(DriveModel.ENTRY)
        if e is None:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        cell = option.rect
        vw = self.view.viewport().width()
        row = QRect(MX, cell.y() + 2, vw - 2 * MX, cell.height() - 4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = index.row() == getattr(self.view, "hover_row", -1)
        drop = getattr(self.view, "_drop_row", QModelIndex()).isValid() and index.row() == self.view._drop_row.row()
        if selected or hover or drop:
            p.setClipRect(cell)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent_soft if (selected or drop) else t.surface2))
            p.drawRoundedRect(row, 11, 11)
            if drop:
                p.setPen(QPen(QColor(t.accent), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(row.adjusted(1, 1, -1, -1), 10, 10)
            p.setClipping(False)
        col = index.column()
        mid = cell.center().y()
        if col == 0:
            self._paint_name(p, option, e, row, mid, t)
        elif col == 1:
            txt = str(index.data(Qt.ItemDataRole.DisplayRole) or "").strip("  ")
            self._text(p, cell.adjusted(8, 0, -12, 0), txt or "—", t.text2 if txt else t.text3,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif col == 2:
            txt = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            self._text(p, cell.adjusted(10, 0, -8, 0), txt or "—", t.text2 if txt else t.text3,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        elif e.shared_by_url:
            self._paint_link_chip(p, cell, mid, t)
        p.restore()

    def _text(self, p: QPainter, rect: QRect, text: str, color: str, align) -> None:
        f = font_with(self.view.font(), 10.0)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(rect, int(align), QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideRight, rect.width()))

    def _paint_name(self, p: QPainter, option, e, row: QRect, mid: int, t) -> None:
        cat = theme.category(e.name, e.is_dir)
        base = QColor(t.color(f"cat:{cat}"))
        tile = QRect(row.x() + 10, mid - TILE // 2, TILE, TILE)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        bg = QColor(t.surface) if selected else QColor(base)
        if not selected:
            bg.setAlpha(46 if theme.is_dark() else 30)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(tile, 10, 10)
        theme.file_icon(e.name, e.is_dir).paint(p, tile.adjusted(7, 7, -7, -7))
        x = tile.right() + 14
        w = option.rect.right() - x - 8
        f1 = font_with(self.view.font(), 10.5, QFont.Weight.Medium)
        f2 = font_with(self.view.font(), 9.0)
        fm1, fm2 = QFontMetrics(f1), QFontMetrics(f2)
        top = mid - (fm1.height() + fm2.height() - 2) // 2
        p.setFont(f1)
        p.setPen(QColor(t.text))
        p.drawText(QRect(x, top, w, fm1.height()), int(Qt.AlignmentFlag.AlignVCenter),
                   fm1.elidedText(e.name, Qt.TextElideMode.ElideMiddle if not e.is_dir else Qt.TextElideMode.ElideRight, w))
        p.setFont(f2)
        p.setPen(QColor(t.text2))
        p.drawText(QRect(x, top + fm1.height() - 2, w, fm2.height()), int(Qt.AlignmentFlag.AlignVCenter),
                   fm2.elidedText(theme.kind_label(e.name, e.is_dir), Qt.TextElideMode.ElideRight, w))

    def _paint_link_chip(self, p: QPainter, cell: QRect, mid: int, t) -> None:
        f = font_with(self.view.font(), 9.0, QFont.Weight.DemiBold)
        fm = QFontMetrics(f)
        text = "Link"
        w = fm.horizontalAdvance(text) + 36
        r = QRect(cell.x() + 8, mid - 11, w, 22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.accent_soft2 if not theme.is_dark() else t.accent_soft))
        p.setPen(QPen(QColor(t.accent_soft if not theme.is_dark() else t.border_strong), 1))
        p.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
        theme.icon("link", "accent_text").paint(p, QRect(r.x() + 8, mid - 6, 12, 12))
        p.setFont(f)
        p.setPen(QColor(t.accent_text))
        p.drawText(QRect(r.x() + 24, r.y(), w - 26, r.height()), int(Qt.AlignmentFlag.AlignVCenter), text)


# ---------------------------------------------------------------- dialogs

class _Prompt(QDialog):
    """Base for custom dialogs: no native icon, buttons on the right."""

    def __init__(self, parent: QWidget | None, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 22, 24, 20)
        self.root.setSpacing(16)

    def buttons(self, ok_text: str, ok_variant: str = "primary", cancel_text: str = "Cancel",
                default_ok: bool = True) -> tuple[QPushButton, QPushButton]:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        cancel = QPushButton(cancel_text)
        ok = QPushButton(ok_text)
        theme.set_props(ok, variant=ok_variant)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        self.root.addLayout(row)
        (ok if default_ok else cancel).setDefault(True)
        return ok, cancel


class ConfirmDialog(_Prompt):
    """Confirmation with an icon in a colored box; ``danger`` paints the button red."""

    def __init__(self, parent: QWidget | None, title: str, text: str, ok_text: str,
                 danger: bool = False, kind: str = "warning"):
        super().__init__(parent, title)
        tone = "danger" if danger else kind
        head = QHBoxLayout()
        head.setSpacing(16)
        head.addWidget(Tile({"danger": "trash", "warning": "warning", "info": "info"}.get(tone, "info"),
                            tone if tone in ("danger", "warning") else "accent", 44, 24), 0,
                       Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(6)
        ttl = QLabel(title)
        theme.set_props(ttl, role="title")
        msg = QLabel(text)
        msg.setWordWrap(True)
        theme.set_props(msg, role="muted")
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(ttl)
        body.addWidget(msg)
        head.addLayout(body, 1)
        self.root.addLayout(head)
        self.ok_button, self.cancel_button = self.buttons(ok_text, "danger" if danger else "primary",
                                                          default_ok=False)


class PromptDialog(_Prompt):
    """Ask for a line of text."""

    def __init__(self, parent: QWidget | None, title: str, label: str, default: str = "",
                 ok_text: str = "OK"):
        super().__init__(parent, title)
        ttl = QLabel(title)
        theme.set_props(ttl, role="title")
        lab = QLabel(label)
        theme.set_props(lab, role="muted")
        self.edit = QLineEdit(default)
        self.edit.selectAll()
        self.root.addWidget(ttl)
        self.root.addWidget(lab)
        self.root.addWidget(self.edit)
        self.ok_button, _ = self.buttons(ok_text)
        self.edit.textChanged.connect(lambda s: self.ok_button.setEnabled(bool(s.strip())))
        self.edit.returnPressed.connect(self.ok_button.click)
        self.ok_button.setEnabled(bool(default.strip()))


def confirm(parent: QWidget | None, title: str, text: str, ok_text: str, danger: bool = False) -> bool:
    return ConfirmDialog(parent, title, text, ok_text, danger).exec() == QDialog.DialogCode.Accepted


def prompt(parent: QWidget | None, title: str, label: str, default: str = "") -> str | None:
    dlg = PromptDialog(parent, title, label, default)
    if dlg.exec() == QDialog.DialogCode.Accepted and dlg.edit.text().strip():
        return dlg.edit.text().strip()
    return None
