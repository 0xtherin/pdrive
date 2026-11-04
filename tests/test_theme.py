"""Tests for the custom theme (pdrive.gui.theme / widgets): contrast, icons, QSS and look in light and dark."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PDRIVE_CACHE_DIR", tempfile.mkdtemp(prefix="pdrive-test-cache-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from PySide6.QtCore import QSettings, QSize, Qt, qInstallMessageHandler
    from PySide6.QtGui import QColor, QIcon
    from PySide6.QtWidgets import QApplication, QLabel
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "assets" / "icons"


def _lum(c: str) -> float:
    q = QColor(c)
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(q.redF()) + 0.7152 * f(q.greenF()) + 0.0722 * f(q.blueF())


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from pdrive.gui import theme
        cls.theme = theme

    def tearDown(self):
        self.theme.set_scheme(None)

    def test_contrast_aa_both_themes(self):
        for t in (self.theme.LIGHT, self.theme.DARK):
            for fg, bg in (("text", "bg"), ("text", "surface"), ("text2", "bg"), ("text2", "surface"),
                           ("onaccent", "accent"), ("accent_text", "surface"), ("accent_text", "accent_soft"),
                           ("danger", "surface"), ("danger", "danger_soft"), ("warning", "surface"),
                           ("warning", "warning_soft"), ("success", "surface"), ("success", "success_soft"),
                           ("text", "accent_soft"), ("accent_text", "bg")):
                c = contrast(getattr(t, fg), getattr(t, bg))
                self.assertGreaterEqual(c, 4.5, f"{t.name}: {fg} on {bg} = {c:.2f}")
            for cat, col in t.cat.items():  # category icons (graphical elements: 3:1)
                self.assertGreaterEqual(contrast(col, t.surface), 3.0, f"{t.name}: cat {cat}")

    def test_icons_exist_render_and_recolor(self):
        names = sorted(p.stem for p in ICON_DIR.glob("*.svg"))
        self.assertGreaterEqual(len(names), 36)
        need = ("folder folder-open file image video audio pdf zip code doc sheet link share users computer "
                "trash upload download folder-plus refresh back forward up search settings more copy cut "
                "paste rename info close check error warning cloud lock").split()
        for n in need:
            self.assertIn(n, names, n)
        for scheme in ("light", "dark"):
            self.theme.set_scheme(scheme)
            for n in names:
                pm = self.theme.icon(n).pixmap(QSize(32, 32))
                self.assertFalse(pm.isNull(), n)
                img = pm.toImage()
                self.assertTrue(any(img.pixelColor(x, y).alpha() for x in range(0, 32, 2) for y in range(0, 32, 2)),
                                f"{scheme}/{n} empty")
        # the same QIcon changes color when the theme changes (painted on the fly)
        ic = self.theme.icon("folder-plus")
        self.theme.set_scheme("light")
        a = ic.pixmap(QSize(24, 24)).toImage()
        self.theme.set_scheme("dark")
        b = ic.pixmap(QSize(24, 24)).toImage()
        self.assertNotEqual(a, b)

    def test_icons_are_currentcolor_svgs(self):
        for p in ICON_DIR.glob("*.svg"):
            txt = p.read_text()
            self.assertIn("currentColor", txt, p.name)
            if not p.stem.startswith("empty"):
                self.assertIn('viewBox="0 0 24 24"', txt, p.name)
                self.assertIn('stroke-width="1.75"', txt, p.name)

    def test_category_and_kind_labels(self):
        t = self.theme
        self.assertEqual(t.category("a.PDF"), "pdf")
        self.assertEqual(t.category("x.jpeg"), "image")
        self.assertEqual(t.category("x", True), "folder")
        self.assertEqual(t.category("noextension"), "file")
        self.assertEqual(t.kind_label("a.png"), "Image · PNG")
        self.assertEqual(t.kind_label("d", True), "Folder")

    def test_apply_sets_fusion_palette_and_stylesheet_without_parse_warnings(self):
        msgs: list[str] = []
        prev = qInstallMessageHandler(lambda mode, ctx, msg: msgs.append(msg))
        try:
            for scheme in ("light", "dark"):
                self.theme.set_scheme(scheme)
                self.assertEqual(self.theme.base_style_name(), "fusion")
                self.assertEqual(self.theme.tokens().name, scheme)
                self.assertEqual(self.app.palette().color(self.app.palette().ColorRole.Window).name(),
                                 getattr(self.theme, scheme.upper()).bg)
                w = QLabel("x")
                w.setStyleSheet("")
                w.ensurePolished()
        finally:
            qInstallMessageHandler(prev)
        bad = [m for m in msgs if "Could not parse" in m or "Unknown property" in m]
        self.assertEqual(bad, [])

    def test_env_forces_scheme(self):
        os.environ["PDRIVE_THEME"] = "dark"
        try:
            self.theme.apply(self.app)
            self.assertTrue(self.theme.is_dark())
        finally:
            del os.environ["PDRIVE_THEME"]
            self.theme.set_scheme(None)

    def test_no_native_icons_in_gui_sources(self):
        for f in (ROOT / "pdrive" / "gui").glob("*.py"):
            src = f.read_text()
            self.assertNotIn("fromTheme", src.replace('QIcon.fromTheme("pdrive")', ""), f.name)
            self.assertIsNone(re.search(r"\bSP_[A-Z]", src), f.name)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class WindowLookTests(unittest.TestCase):
    """The window is built and painted in both themes; the custom dialogs work."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from pdrive.gui import theme
        cls.theme = theme

    def tearDown(self):
        self.theme.set_scheme(None)

    def test_window_renders_in_both_themes(self):
        from tests.test_gui import FakeClient, wait_for  # reuse the in-memory Drive
        from pdrive.gui.window import MainWindow
        shots = {}
        for scheme in ("light", "dark"):
            self.theme.set_scheme(scheme)
            st = QSettings(tempfile.mktemp(suffix=".ini"), QSettings.Format.IniFormat)
            w = MainWindow(FakeClient(), {}, st)
            w.resize(1000, 640)
            w.show()
            self.assertTrue(wait_for(lambda: w.model.rowCount() == 4))
            img = w.grab().toImage()
            self.assertFalse(img.isNull())
            self.assertEqual(img.pixelColor(2, 2).name(), getattr(self.theme, scheme.upper()).bg)
            shots[scheme] = img
            w.close()
        self.assertNotEqual(shots["light"], shots["dark"])

    def test_row_delegate_height_and_columns_collapse_when_narrow(self):
        from tests.test_gui import FakeClient, wait_for
        from pdrive.gui.window import MainWindow
        from pdrive.gui.widgets import ROW_H
        st = QSettings(tempfile.mktemp(suffix=".ini"), QSettings.Format.IniFormat)
        w = MainWindow(FakeClient(), {}, st)
        w.resize(1000, 640)
        w.show()
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 4))
        self.assertIn(w.view.sizeHintForRow(0), (ROW_H,))
        self.assertTrue(44 <= ROW_H <= 52)
        self.assertFalse(w.view.isColumnHidden(3))
        w.resize(720, 600)
        self.app.processEvents()
        self.assertFalse(w.side.isVisible())     # no side column in narrow windows
        w.view.resize(500, 300)                   # narrow list: secondary columns hidden
        self.app.processEvents()
        self.assertTrue(w.view.isColumnHidden(3))
        w.view.resize(400, 300)
        self.app.processEvents()
        self.assertTrue(w.view.isColumnHidden(2))
        w.close()

    def test_confirm_and_prompt_dialogs(self):
        from pdrive.gui.widgets import ConfirmDialog, PromptDialog
        d = ConfirmDialog(None, "Delete", "Are you sure?", "Delete", danger=True)
        self.assertEqual(d.ok_button.text(), "Delete")
        self.assertEqual(d.ok_button.property("variant"), "danger")
        self.assertTrue(d.cancel_button.isDefault())          # Enter never confirms a deletion
        d.ok_button.click()
        self.assertEqual(d.result(), d.DialogCode.Accepted)
        p = PromptDialog(None, "New folder", "Name:", "")
        self.assertFalse(p.ok_button.isEnabled())
        p.edit.setText("  Docs ")
        self.assertTrue(p.ok_button.isEnabled())

    def test_activity_bar_animates_only_when_visible(self):
        from pdrive.gui.widgets import ActivityBar
        b = ActivityBar()
        b.setRange(0, 0)
        self.assertFalse(b._timer.isActive())
        b.show()
        self.assertTrue(b._timer.isActive())
        b.hide()
        self.assertFalse(b._timer.isActive())


if __name__ == "__main__":
    unittest.main()
