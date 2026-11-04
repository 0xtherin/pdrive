"""pdrive custom theme: design tokens, palette, QSS stylesheet and SVG icons.

* ``Fusion`` base style + custom palette + QSS generated from ``Tokens`` (light and dark).
* ``icon(name, color=None)`` returns a ``QIcon`` whose engine paints the SVG on the fly with
  the current theme color: switching from light to dark does not require rebuilding any icon.
* ``install(app)`` applies the theme and re-applies it live when the system color scheme changes.
  ``PDRIVE_THEME=light|dark`` forces a scheme (useful for screenshots and tests).
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QIconEngine, QImage, QPainter,
                           QPalette, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QMenu, QStyleFactory, QWidget

ACCENT = "#6d4aff"
FONT_FAMILIES = ["Inter", "Adwaita Sans", "Noto Sans", "Cantarell", "Open Sans", "Sans Serif"]
FONT_PT = 10.0
R_SM, R_MD, R_LG = 8, 10, 14   # corner radii


# ------------------------------------------------------------------ tokens

@dataclass(frozen=True)
class Tokens:
    """Colors of a theme. ``cat`` = color per file category."""

    name: str
    bg: str            # window background
    surface: str       # cards, list, fields
    surface2: str      # soft hover
    surface3: str      # pressed / chip background
    border: str
    border_strong: str
    text: str
    text2: str         # secondary text (AA contrast)
    text3: str         # decorative / disabled only
    accent: str        # fill of primary buttons (white text on top, AA)
    accent_hover: str
    accent_press: str
    accent_text: str   # accent as text/icon on surfaces
    accent_soft: str   # selection pill
    accent_soft2: str
    onaccent: str
    danger: str
    danger_soft: str
    warning: str
    warning_soft: str
    success: str
    success_soft: str
    info_soft: str
    shadow: str
    cat: dict = field(default_factory=dict)

    def color(self, spec: str | QColor | None) -> QColor:
        """Resolve a token name (``accent``, ``cat:pdf``…) or a ``#rrggbb``."""
        if isinstance(spec, QColor):
            return spec
        spec = spec or "text"
        if spec.startswith("#"):
            return QColor(spec)
        if spec.startswith("cat:"):
            return QColor(self.cat.get(spec[4:], self.text2))
        return QColor(getattr(self, spec, self.text))


LIGHT = Tokens(
    name="light", bg="#f4f2fb", surface="#ffffff", surface2="#f6f4fd", surface3="#ece9f8",
    border="#e3e0f0", border_strong="#cbc6e2", text="#1c1932", text2="#5f5b78", text3="#a29fb8",
    accent=ACCENT, accent_hover="#5f3df0", accent_press="#5232d6", accent_text="#5a38f0",
    accent_soft="#ebe6ff", accent_soft2="#f3f0ff", onaccent="#ffffff",
    danger="#c8283f", danger_soft="#fdecef", warning="#955000", warning_soft="#fff3df",
    success="#12795a", success_soft="#e3f6ee", info_soft="#ebe6ff", shadow="#2a1f6b",
    cat={"folder": "#6d4aff", "image": "#cc3f86", "video": "#d9641f", "audio": "#0f8f93",
         "pdf": "#cf3a35", "zip": "#a87607", "code": "#0b82b5", "doc": "#3a72d6",
         "sheet": "#238f57", "file": "#6b6785"})

DARK = Tokens(
    name="dark", bg="#131120", surface="#1c1930", surface2="#242040", surface3="#2c2749",
    border="#2c2848", border_strong="#443e6b", text="#eeedf8", text2="#a9a5c4", text3="#6c6890",
    accent=ACCENT, accent_hover="#7b5cff", accent_press="#5f3df0", accent_text="#a591ff",
    accent_soft="#31285e", accent_soft2="#27214a", onaccent="#ffffff",
    danger="#ff7a8c", danger_soft="#3d1f2b", warning="#f0a64a", warning_soft="#3a2c18",
    success="#4cc79b", success_soft="#17332b", info_soft="#31285e", shadow="#000000",
    cat={"folder": "#a591ff", "image": "#f06aa6", "video": "#f08a4b", "audio": "#3cc1c4",
         "pdf": "#f2706b", "zip": "#e0b030", "code": "#43b3e3", "doc": "#6ea0f5",
         "sheet": "#52c283", "file": "#9b97b8"})


class _Signals(QObject):
    changed = Signal()      # the theme changed: widgets with cached pixmaps must refresh


signals = _Signals()
_forced: str | None = None
_current: Tokens = LIGHT


def tokens() -> Tokens:
    return _current


def is_dark() -> bool:
    return _current.name == "dark"


def _system_scheme() -> str:
    env = os.environ.get("PDRIVE_THEME", "").lower()
    if env in ("light", "dark"):
        return env
    if _forced:
        return _forced
    hints = QGuiApplication.styleHints()
    cs = hints.colorScheme() if hints is not None else Qt.ColorScheme.Unknown
    if cs == Qt.ColorScheme.Dark:
        return "dark"
    if cs == Qt.ColorScheme.Light:
        return "light"
    pal = QGuiApplication.palette()  # no hint from the platform: infer from the palette
    return "dark" if pal.color(QPalette.ColorRole.Window).lightness() < 110 else "light"


def set_scheme(name: str | None) -> None:
    """Force ``light``/``dark`` (``None`` = follow the system) and re-apply."""
    global _forced
    _forced = name
    app = QApplication.instance()
    if app is not None:
        apply(app)


# ------------------------------------------------------------------- icons

def _icon_dirs() -> list[Path]:
    here = Path(__file__).resolve().parents[2] / "assets" / "icons"
    return [here, Path.home() / ".local" / "share" / "pdrive" / "icons"]


@lru_cache(maxsize=None)
def _svg_source(name: str) -> str:
    for d in _icon_dirs():
        f = d / f"{name}.svg"
        if f.is_file():
            return f.read_text(encoding="utf-8")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.75"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>')


@lru_cache(maxsize=512)
def _renderer(name: str, color_hex: str) -> QSvgRenderer:
    svg = _svg_source(name).replace("currentColor", color_hex)
    return QSvgRenderer(QByteArray(svg.encode("utf-8")))


class _Engine(QIconEngine):
    """Paint an SVG with the theme color at draw time."""

    def __init__(self, name: str, color: str | None):
        super().__init__()
        self.name, self.color = name, color

    def clone(self) -> "_Engine":
        return _Engine(self.name, self.color)

    def _col(self, mode: QIcon.Mode, state: QIcon.State) -> QColor:
        t = tokens()
        if self.color is None:
            if state == QIcon.State.On:
                return QColor(t.accent_text)
        if self.color in (None, "text2") and mode in (QIcon.Mode.Active, QIcon.Mode.Selected) \
                and self.name not in _NO_HOVER_ACCENT:
            return QColor(t.accent_text)   # on hover / selection, the icon takes the accent color
        return t.color(self.color)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        col = self._col(mode, state)
        painter.save()
        if mode == QIcon.Mode.Disabled:
            painter.setOpacity(painter.opacity() * 0.4)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _renderer(self.name, col.name()).render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        self.paint(p, QRect(QPoint(0, 0), size), mode, state)
        p.end()
        return pm


_NO_HOVER_ACCENT = {"more", "close", "check"}
_icons: dict[tuple[str, str | None], QIcon] = {}


def icon(name: str, color: str | None = None) -> QIcon:
    """Custom icon ``assets/icons/<name>.svg``. ``color``: token (``accent``, ``text2``, ``cat:pdf``…)
    or ``#rrggbb``; with no color it uses the text color (and the accent on hover / when checked)."""
    key = (name, color)
    if key not in _icons:
        _icons[key] = QIcon(_Engine(name, color))
    return _icons[key]


def pixmap(name: str, size: int, color: str | None = None, dpr: float | None = None) -> QPixmap:
    """Sharp (HiDPI) pixmap of an icon in the requested color."""
    dpr = dpr or (QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0)
    px = max(1, round(size * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(name, tokens().color(color).name()).render(p, QRectF(0, 0, px, px))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# file categories --------------------------------------------------
_EXT = {
    "image": "png jpg jpeg gif webp bmp svg heic heif tif tiff ico avif raw cr2 nef",
    "video": "mp4 mkv mov avi webm m4v wmv flv mpg mpeg 3gp",
    "audio": "mp3 flac wav ogg oga opus m4a aac wma aiff mid midi",
    "pdf": "pdf",
    "zip": "zip rar 7z tar gz tgz bz2 xz zst iso deb rpm dmg appimage",
    "code": ("py js ts tsx jsx json html css scss c h cpp hpp rs go java kt sh bash zsh yml yaml toml xml "
             "sql php rb lua md ini conf cfg ipynb"),
    "doc": "doc docx odt rtf txt pages tex epub ppt pptx odp key",
    "sheet": "xls xlsx ods csv tsv numbers",
}
_CAT_BY_EXT = {e: c for c, exts in _EXT.items() for e in exts.split()}
CATEGORY_LABEL = {"folder": "Folder", "image": "Image", "video": "Video", "audio": "Audio",
                  "pdf": "PDF document", "zip": "Archive", "code": "Code",
                  "doc": "Document", "sheet": "Spreadsheet", "file": "File"}
_CAT_ICON = {"folder": "folder", "image": "image", "video": "video", "audio": "audio", "pdf": "pdf",
             "zip": "zip", "code": "code", "doc": "doc", "sheet": "sheet", "file": "file"}


def category(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "folder"
    return _CAT_BY_EXT.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", "file")


def kind_label(name: str, is_dir: bool = False) -> str:
    """\"Image · PNG\", \"Folder\"… (secondary line of each row)."""
    cat = category(name, is_dir)
    label = CATEGORY_LABEL[cat]
    if not is_dir and "." in name.strip("."):
        ext = name.rsplit(".", 1)[-1]
        if 0 < len(ext) <= 5 and cat != "pdf":
            return f"{label} · {ext.upper()}"
    return label


def file_icon(name: str, is_dir: bool = False) -> QIcon:
    cat = category(name, is_dir)
    return icon(_CAT_ICON[cat], f"cat:{cat}")


# ---------------------------------------------------------- QSS and palette

_assets_dir: Path | None = None


def _asset_dir() -> Path:
    global _assets_dir
    if _assets_dir is None or not _assets_dir.is_dir():
        _assets_dir = Path(tempfile.mkdtemp(prefix="pdrive-theme-"))
        atexit.register(shutil.rmtree, _assets_dir, True)
    return _assets_dir


def _png(name: str, draw, size: int = 18) -> str:
    """Generate ``name.png`` and ``name@2x.png`` (QSS indicators) and return the base path."""
    d = _asset_dir()
    for scale, suffix in ((1, ""), (2, "@2x")):
        img = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(scale, scale)
        draw(p, size)
        p.end()
        img.save(str(d / f"{name}{suffix}.png"))
    return (d / f"{name}.png").as_posix()


def _indicators(t: Tokens) -> dict[str, str]:
    def box(checked: bool, hover: bool = False):
        def draw(p: QPainter, s: int) -> None:
            r = QRectF(1, 1, s - 2, s - 2)
            p.setPen(Qt.PenStyle.NoPen if checked else QColor(t.accent if hover else t.border_strong))
            p.setBrush(QColor(t.accent) if checked else QColor(t.surface))
            p.drawRoundedRect(r, 5, 5)
            if checked:
                _renderer("check", t.onaccent).render(p, QRectF(2.2, 2.2, s - 4.4, s - 4.4))
        return draw

    def chevron(name: str, color: str):
        def draw(p: QPainter, s: int) -> None:
            _renderer(name, color).render(p, QRectF(0, 0, s, s))
        return draw

    n = t.name
    return {
        "check_off": _png(f"{n}-box-off", box(False)),
        "check_off_h": _png(f"{n}-box-off-h", box(False, True)),
        "check_on": _png(f"{n}-box-on", box(True)),
        "down": _png(f"{n}-down", chevron("chevron-down", t.text2), 16),
        "up": _png(f"{n}-up", chevron("chevron-up", t.text2), 16),
    }


def stylesheet(t: Tokens) -> str:
    """Complete stylesheet built from the tokens."""
    ind = _indicators(t)
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {t.text}; selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QMainWindow, QDialog, QWidget#central {{ background: {t.bg}; }}
QDialog {{ background: {t.surface}; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {t.text2}; }}
QLabel[role="title"] {{ font-size: 13pt; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 10pt; font-weight: 600; color: {t.text}; }}
QLabel[role="brand"] {{ font-size: 13pt; font-weight: 700; }}
QLabel[role="caption"] {{ color: {t.text2}; font-size: 9pt; }}
QLabel[role="error"] {{ color: {t.danger}; }}
QLabel[role="alert"] {{ background: {t.danger_soft}; color: {t.danger}; border: 1px solid {t.danger}; border-radius: 10px; padding: 8px 12px; }}
QLabel[role="note"] {{ background: {t.success_soft}; color: {t.success}; border-radius: 10px; padding: 8px 12px; }}
QLabel[role="ok"] {{ color: {t.success}; }}
QToolTip {{ background: {t.text}; color: {t.surface}; border: 0; padding: 5px 8px; border-radius: 6px; }}

/* ---- buttons ---- */
QPushButton {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: {R_MD}px; padding: 7px 16px; font-weight: 500; min-height: 18px; }}
QPushButton:hover {{ background: {t.surface2}; border-color: {t.accent}; }}
QPushButton:pressed {{ background: {t.surface3}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton:disabled {{ color: {t.text3}; background: {t.surface2}; border-color: {t.border}; }}
QPushButton[variant="primary"] {{ background: {t.accent}; color: {t.onaccent}; border: 1px solid {t.accent}; }}
QPushButton[variant="primary"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {t.accent_press}; }}
QPushButton[variant="primary"]:disabled {{ background: {t.surface3}; color: {t.text3}; border-color: {t.surface3}; }}
QPushButton[variant="danger"] {{ background: {t.danger}; color: {t.surface if t.name == "dark" else "#ffffff"}; border: 1px solid {t.danger}; }}
QPushButton[variant="danger"]:hover {{ background: {t.danger}; border-color: {t.text}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t.text2}; padding: 6px 12px; }}
QPushButton[variant="ghost"]:hover {{ background: {t.surface3}; color: {t.text}; border-color: transparent; }}
QPushButton[variant="dangerghost"] {{ background: transparent; color: {t.danger}; border: 1px solid {t.border_strong}; }}
QPushButton[variant="dangerghost"]:hover {{ background: {t.danger_soft}; border-color: {t.danger}; }}
QPushButton[variant="dangerghost"]:disabled {{ color: {t.text3}; border-color: {t.border}; background: transparent; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {R_MD}px;
    padding: 6px 8px; color: {t.text}; }}
QToolButton:hover {{ background: {t.surface3}; }}
QToolButton:pressed {{ background: {t.accent_soft}; }}
QToolButton:checked {{ background: {t.accent_soft}; color: {t.accent_text}; }}
QToolButton:disabled {{ color: {t.text3}; }}
QToolButton:focus {{ border-color: {t.accent}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton[variant="primary"] {{ background: {t.accent}; color: {t.onaccent}; padding: 9px 18px;
    font-weight: 600; border-radius: 12px; }}
QToolButton[variant="primary"]:hover {{ background: {t.accent_hover}; }}
QToolButton[variant="primary"]:pressed {{ background: {t.accent_press}; }}
QToolButton[variant="link"] {{ color: {t.accent_text}; padding: 3px 8px; border-radius: 8px; }}
QToolButton[variant="link"]:hover {{ background: {t.accent_soft}; }}
QToolButton[variant="crumb"] {{ padding: 4px 8px; border-radius: 8px; color: {t.text2}; }}
QToolButton[variant="crumb"]:hover {{ background: {t.surface3}; color: {t.text}; }}
QToolButton[variant="crumb"][current="true"] {{ color: {t.text}; font-weight: 600; }}
QToolButton[variant="sidebar"] {{ color: {t.text2}; text-align: left; padding: 9px 12px; font-weight: 500; }}
QToolButton[variant="sidebar"]:hover {{ background: {t.surface3}; color: {t.text}; }}
QToolButton[variant="nav"] {{ padding: 7px; border-radius: 10px; }}
QToolButton[variant="icon"] {{ padding: 5px; border-radius: 8px; }}

/* ---- fields ---- */
QLineEdit, QComboBox, QDateEdit, QSpinBox, QTextEdit, QPlainTextEdit {{ background: {t.surface};
    border: 1px solid {t.border_strong}; border-radius: {R_MD}px; padding: 7px 11px; min-height: 18px;
    selection-background-color: {t.accent}; selection-color: {t.onaccent}; }}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover {{ border-color: {t.text3}; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QComboBox:on {{ border: 1px solid {t.accent}; }}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled {{ background: {t.surface2}; color: {t.text3}; border-color: {t.border}; }}
QLineEdit:read-only {{ background: {t.surface2}; }}
QLineEdit[variant="search"], QLineEdit[variant="path"] {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; padding: 7px 12px 7px 4px; }}
QLineEdit[variant="search"]:focus, QLineEdit[variant="path"]:focus {{ border: 1px solid {t.accent}; }}
QLineEdit[variant="path"] {{ padding: 7px 12px; }}
QComboBox {{ padding-right: 30px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 28px; border: 0; }}
QComboBox::down-arrow {{ image: url("{ind['down']}"); width: 16px; height: 16px; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: {R_MD}px;
    padding: 4px; outline: 0; selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QComboBox QAbstractItemView::item {{ padding: 6px 10px; border-radius: 6px; min-height: 22px; }}
QComboBox QAbstractItemView::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QDateEdit::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 28px; border: 0; }}
QDateEdit::down-arrow {{ image: url("{ind['down']}"); width: 16px; height: 16px; }}
QCalendarWidget QWidget {{ background: {t.surface}; }}
QCheckBox {{ spacing: 9px; background: transparent; }}
QCheckBox::indicator {{ width: 18px; height: 18px; }}
QCheckBox::indicator:unchecked {{ image: url("{ind['check_off']}"); }}
QCheckBox::indicator:unchecked:hover {{ image: url("{ind['check_off_h']}"); }}
QCheckBox::indicator:checked {{ image: url("{ind['check_on']}"); }}
QCheckBox:disabled {{ color: {t.text3}; }}

/* ---- containers ---- */
QFrame#card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {R_LG}px; }}
QFrame#navpill {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; }}
QFrame#navpill[editing="true"] {{ border-color: {t.accent}; }}
QWidget#side {{ background: transparent; }}
QGroupBox {{ background: transparent; border: 1px solid {t.border}; border-radius: {R_LG}px;
    margin-top: 12px; padding: 16px 14px 12px 14px; font-weight: 400; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 14px; padding: 0 6px;
    color: {t.text}; font-weight: 600; background: {t.surface}; }}
QFrame#banner {{ border-radius: 12px; border: 1px solid {t.border}; background: {t.accent_soft2}; }}
QFrame#banner[kind="error"] {{ background: {t.danger_soft}; border-color: {t.danger}; }}
QFrame#banner[kind="warning"] {{ background: {t.warning_soft}; border-color: {t.warning}; }}
QFrame#banner[kind="info"] {{ background: {t.info_soft}; border-color: {t.accent}; }}
QFrame#transfer {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; }}
QFrame#tile {{ border-radius: 10px; background: {t.accent_soft}; border: 0; }}
QFrame#tile[tone="danger"] {{ background: {t.danger_soft}; }}
QFrame#tile[tone="success"] {{ background: {t.success_soft}; }}
QFrame#tile[tone="warning"] {{ background: {t.warning_soft}; }}
QLabel#chip {{ border-radius: 9px; padding: 2px 9px; font-size: 9pt; font-weight: 600; background: {t.surface3}; color: {t.text2}; }}
QLabel#chip[tone="accent"] {{ background: {t.accent_soft}; color: {t.accent_text}; }}
QLabel#chip[tone="success"] {{ background: {t.success_soft}; color: {t.success}; }}
QLabel#chip[tone="danger"] {{ background: {t.danger_soft}; color: {t.danger}; }}
QDockWidget {{ background: transparent; }}
QDockWidget::title {{ background: transparent; padding: 0; }}
QStatusBar {{ background: transparent; color: {t.text2}; border: 0; }}
QStatusBar::item {{ border: 0; }}
QStatusBar QLabel {{ color: {t.text2}; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QToolBar {{ background: transparent; border: 0; spacing: 4px; padding: 0; }}
QToolBar::separator {{ background: {t.border}; width: 1px; margin: 8px 6px; }}
QMenuBar {{ background: transparent; }}

/* ---- lists ---- */
QTreeView, QListView, QListWidget, QTreeWidget, QTableView {{ background: transparent; border: 0; }}
QListWidget#sidebar {{ background: transparent; border: 0; outline: 0; }}
QListWidget#sidebar::item {{ padding: 9px 12px; margin: 1px 0; border-radius: 10px; color: {t.text2}; font-weight: 500; }}
QListWidget#sidebar::item:hover {{ background: {t.surface3}; color: {t.text}; }}
QListWidget#sidebar::item:selected {{ background: {t.accent_soft}; color: {t.accent_text}; font-weight: 600; }}
QTreeWidget {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {R_MD}px; padding: 4px; }}
QTreeWidget::item {{ padding: 6px 4px; border-radius: 6px; }}
QTreeWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QTreeWidget::item:hover:!selected {{ background: {t.surface2}; }}
QHeaderView {{ background: transparent; border: 0; }}
QHeaderView::section {{ background: transparent; color: {t.text2}; border: 0; border-bottom: 1px solid {t.border};
    padding: 6px 8px; font-weight: 600; font-size: 9pt; }}

/* ---- scroll bars ---- */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 4px 2px 4px 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px 4px 2px 4px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 4px; min-height: 32px; margin: 0 1px; }}
QScrollBar::handle:horizontal {{ background: {t.border_strong}; border-radius: 4px; min-width: 32px; margin: 1px 0; }}
QScrollBar::handle:hover {{ background: {t.text3}; }}
QScrollBar::handle:pressed {{ background: {t.text2}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: none; border: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- menus ---- */
QMenu {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; padding: 6px; margin: 10px; }}
QMenu::item {{ padding: 7px 14px 7px 10px; border-radius: 8px; margin: 1px 0; color: {t.text}; }}
QMenu::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QMenu::item:disabled {{ color: {t.text3}; }}
QMenu::icon {{ padding-left: 6px; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 5px 8px; }}
QMenu::shortcut {{ color: {t.text2}; }}

/* ---- progress ---- */
QProgressBar {{ background: {t.surface3}; border: 0; border-radius: 3px; max-height: 6px; min-height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}
"""


def palette(t: Tokens) -> QPalette:
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    for role, col in ((R.Window, t.bg), (R.WindowText, t.text), (R.Base, t.surface), (R.AlternateBase, t.surface2),
                      (R.Text, t.text), (R.Button, t.surface), (R.ButtonText, t.text), (R.Highlight, t.accent),
                      (R.HighlightedText, t.onaccent), (R.ToolTipBase, t.text), (R.ToolTipText, t.surface),
                      (R.PlaceholderText, t.text2), (R.Link, t.accent_text), (R.LinkVisited, t.accent_text),
                      (R.BrightText, t.danger), (R.Light, t.surface), (R.Midlight, t.surface2),
                      (R.Mid, t.border_strong), (R.Dark, t.border_strong), (R.Shadow, t.shadow)):
        p.setColor(role, QColor(col))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(G.Disabled, role, QColor(t.text3))
    return p


class _MenuStyler(QObject):
    """Popup menus with rounded corners and a shadow (translucent frameless window)."""

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt API
        if ev.type() == QEvent.Type.Polish and isinstance(obj, QMenu) and not obj.property("_pd_styled"):
            obj.setProperty("_pd_styled", True)
            obj.setWindowFlags(obj.windowFlags() | Qt.WindowType.FramelessWindowHint
                               | Qt.WindowType.NoDropShadowWindowHint)
            obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            fx = QGraphicsDropShadowEffect(obj)
            fx.setBlurRadius(28)
            fx.setOffset(0, 5)
            fx.setColor(QColor(0, 0, 0, 90 if is_dark() else 55))
            obj.setGraphicsEffect(fx)
        return False


_menu_styler: _MenuStyler | None = None
_styled = False


def base_style_name() -> str:
    """Base style in use (always \"fusion\" after ``apply``)."""
    return "fusion" if _styled else ""


def apply(app: QApplication) -> None:
    """(Re)apply style, palette, font and QSS for the current scheme."""
    global _current, _menu_styler
    _current = DARK if _system_scheme() == "dark" else LIGHT
    global _styled
    if not _styled:   # Fusion only once (changing style resets the QSS)
        fusion = QStyleFactory.create("Fusion")
        if fusion is not None:
            app.setStyle(fusion)
        _styled = True
    f = QFont(app.font())
    f.setFamilies(FONT_FAMILIES)
    f.setPointSizeF(FONT_PT)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(f)
    app.setPalette(palette(_current))
    app.setStyleSheet(stylesheet(_current))
    if _menu_styler is None:
        _menu_styler = _MenuStyler(app)
        app.installEventFilter(_menu_styler)
    signals.changed.emit()


def install(app: QApplication) -> None:
    """Apply the theme and re-apply it when the system switches between light and dark."""
    apply(app)
    hints = app.styleHints()
    if hints is not None and hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda *_: apply(app))


# ---------------------------------------------------------------- utilities

def repolish(w: QWidget) -> None:
    """Re-evaluate the QSS after changing a dynamic property."""
    st = w.style()
    st.unpolish(w)
    st.polish(w)
    w.update()


def set_props(w: QWidget, **props: object) -> QWidget:
    """Set dynamic properties (``variant``, ``role``, ``kind``…) and repaint if they change."""
    changed = False
    for k, v in props.items():
        if w.property(k) != v:
            w.setProperty(k, v)
            changed = True
    if changed:
        repolish(w)
    return w
