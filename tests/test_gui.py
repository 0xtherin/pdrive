"""Main window tests (offscreen, with an in-memory FakeClient and with the real Client + fake CLI)."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from PySide6.QtCore import QSettings, QUrl, QMimeData, Qt, QPoint
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

from pdrive.client import Client, DriveError, Entry, NotFound, NotLoggedIn

FAKE_CLI = Path(__file__).resolve().parent / "fake_proton_drive"


def E(path: str, is_dir: bool = False, size: int = 10, shared: bool = False) -> Entry:
    return Entry(path.rsplit("/", 1)[1], path, is_dir, 0 if is_dir else size, 1_700_000_000.0, None, shared)


class FakeClient:
    """In-memory Drive: {folder path: [Entry]} with the same signatures as Client."""

    def __init__(self):
        self.lock = threading.Lock()
        self.logged_in = True
        self.delay = 0.0
        self.calls: list[tuple] = []
        self.tree = {
            "/my-files": [E("/my-files/Docs", True), E("/my-files/zeta.txt", size=2048),
                          E("/my-files/Foto.png", size=500_000, shared=True), E("/my-files/Alfa", True)],
            "/my-files/Docs": [E("/my-files/Docs/a.pdf")],
            "/my-files/Alfa": [],
            "/shared-with-me": [], "/shared-by-me": [],
        }
        self.trash_items = [E("/trash/viejo.txt")]
        self.urls: dict[str, str] = {}

    def _c(self, *call):
        with self.lock:
            self.calls.append(call)
        if self.delay:
            time.sleep(self.delay)
        if not self.logged_in:
            raise NotLoggedIn("not logged in")

    def sections(self):
        self._c("sections")
        return [E("/my-files", True), E("/devices", True), E("/shared-with-me", True), E("/trash", True),
                E("/shared-by-me", True), E("/photos", True)]

    def list(self, remote):
        self._c("list", remote)
        if remote not in self.tree:
            raise NotFound(remote)
        return list(self.tree[remote])

    def list_trash(self):
        self._c("list_trash")
        return list(self.trash_items)

    def mkdir(self, parent, name):
        self._c("mkdir", parent, name)
        self.tree[parent].append(E(f"{parent}/{name}", True))
        self.tree[f"{parent}/{name}"] = []

    def upload(self, locals, remote_dir, on_conflict="rename"):
        self._c("upload", [Path(p).name for p in locals], remote_dir, on_conflict)
        for p in locals:
            self.tree[remote_dir] = [e for e in self.tree[remote_dir] if e.name != Path(p).name] + \
                [E(f"{remote_dir}/{Path(p).name}", Path(p).is_dir())]

    def download(self, remote, local_dir, on_conflict="rename"):
        self._c("download", remote, str(local_dir), on_conflict)
        (Path(local_dir) / remote.rsplit("/", 1)[1]).write_text("contenido")

    def trash(self, remote):
        self._c("trash", remote)
        parent = remote.rsplit("/", 1)[0]
        e = next(x for x in self.tree[parent] if x.path == remote)
        self.tree[parent].remove(e)
        self.trash_items.append(E("/trash/" + e.name, e.is_dir))

    def restore(self, remote):
        self._c("restore", remote)
        self.trash_items = [e for e in self.trash_items if e.path != remote]
        self.tree["/my-files"].append(E("/my-files/" + remote.rsplit("/", 1)[1]))

    def delete(self, remote):
        self._c("delete", remote)
        self.trash_items = [e for e in self.trash_items if e.path != remote]

    def empty_trash(self):
        self._c("empty_trash")
        self.trash_items = []

    def rename(self, remote, new_name):
        self._c("rename", remote, new_name)
        parent = remote.rsplit("/", 1)[0]
        self.tree[parent] = [E(f"{parent}/{new_name}", e.is_dir, e.size) if e.path == remote else e
                             for e in self.tree[parent]]

    def move(self, remotes, target_parent):
        self._c("move", remotes, target_parent)
        for r in remotes:
            parent = r.rsplit("/", 1)[0]
            e = next(x for x in self.tree[parent] if x.path == r)
            self.tree[parent].remove(e)
            self.tree[target_parent].append(E(f"{target_parent}/{e.name}", e.is_dir, e.size))

    def copy(self, remotes, target_parent):
        self._c("copy", remotes, target_parent)
        for r in remotes:
            e = next(x for x in self.tree[r.rsplit("/", 1)[0]] if x.path == r)
            self.tree[target_parent].append(E(f"{target_parent}/{e.name}", e.is_dir, e.size))

    def size(self, remote):
        self._c("size", remote)
        return 123456

    def sharing_status(self, remote):
        self._c("sharing_status", remote)
        return {"members": [], "invitations": [], "url": self.urls.get(remote), "has_password": False,
                "expiration": None, "role": None}

    def link(self, remote, password=None, expiration=None, role="viewer"):
        self._c("link", remote)
        self.urls[remote] = "https://drive.proton.me/urls/ABC#xyz"
        return self.urls[remote]

    def unlink(self, remote):
        self._c("unlink", remote)
        self.urls.pop(remote, None)

    def login(self):
        self._c("login")

    def logout(self):
        self.logged_in = False

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


def wait_for(cond, timeout=8.0):
    app = QApplication.instance()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(cond())


@unittest.skipUnless(HAVE_QT, "PySide6 no instalado")
class GuiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, client=None, cfg=None):
        from pdrive.gui.window import MainWindow
        self.tmp = Path(tempfile.mkdtemp(prefix="pdrive-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = client or FakeClient()
        st = QSettings(str(self.tmp / "s.ini"), QSettings.Format.IniFormat)
        w = MainWindow(self.client, cfg if cfg is not None else {"on_conflict": "ask", "confirm_trash": False}, st)
        self.addCleanup(self._close, w)
        # No real modal dialogs: the entry points are overridden.
        w._confirm = lambda *a, **k: True
        w.show()
        return w

    def _close(self, w):
        w.close()
        w.deleteLater()

    def names(self, w):
        return [w.proxy.index(r, 0).data() for r in range(w.proxy.rowCount())]

    def select(self, w, *names):
        w.view.selectionModel().clearSelection()
        for r in range(w.proxy.rowCount()):
            if w.proxy.index(r, 0).data() in names:
                w.view.selectionModel().select(w.proxy.index(r, 0), w.view.selectionModel().SelectionFlag.Select
                                               | w.view.selectionModel().SelectionFlag.Rows)
        self.app.processEvents()


class TestLegacyWindowState(GuiBase):
    def test_old_saved_state_does_not_move_the_actions_row(self):
        """A windowState saved when 'main-toolbar' was a window toolbar must not re-dock it on top."""
        from PySide6.QtWidgets import QMainWindow, QToolBar, QDockWidget
        from pdrive.gui.window import MainWindow
        old = QMainWindow()
        tb = QToolBar("Actions")
        tb.setObjectName("main-toolbar")
        old.addToolBar(tb)
        dock = QDockWidget("Transfers")
        dock.setObjectName("transfers-dock")
        old.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        tmp = Path(tempfile.mkdtemp(prefix="pdrive-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        st = QSettings(str(tmp / "s.ini"), QSettings.Format.IniFormat)
        st.setValue("windowState", old.saveState())
        w = MainWindow(FakeClient(), {"on_conflict": "ask"}, st)
        self.addCleanup(self._close, w)
        w.show()
        self.app.processEvents()
        self.assertEqual(w.toolBarArea(w.toolbar), Qt.ToolBarArea.NoToolBarArea)
        w.close()
        self.assertFalse(st.contains("windowState"))


class TestWindowFake(GuiBase):
    def test_startup_lists_root_dirs_first_and_sidebar(self):
        w = self.make()
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 4))
        self.assertEqual(self.names(w), ["Alfa", "Docs", "Foto.png", "zeta.txt"])
        labels = [w.sidebar.item(i).text() for i in range(w.sidebar.count())]
        self.assertEqual(labels, ["My files", "Shared by me", "Shared with me", "Other computers", "Trash"])
        self.assertEqual(w.statusBar().currentMessage() or "", "")
        self.assertEqual(w.lbl_items.text(), "4 items")

    def test_sort_descending_keeps_folders_first(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.names(w), ["Docs", "Alfa", "zeta.txt", "Foto.png"])
        w.view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.names(w)[2:], ["zeta.txt", "Foto.png"])

    def test_navigation_history_and_breadcrumbs(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.navigate("/my-files/Docs")
        self.assertTrue(wait_for(lambda: self.names(w) == ["a.pdf"]))
        self.assertTrue(w.a_back.isEnabled() and w.a_up.isEnabled())
        w.go_back()
        self.assertTrue(wait_for(lambda: w.path == "/my-files" and w.model.rowCount() == 4))
        w.go_forward()
        self.assertTrue(wait_for(lambda: w.path == "/my-files/Docs"))
        w.go_up()
        self.assertTrue(wait_for(lambda: w.path == "/my-files"))
        crumbs = [w.pathbar.lay.itemAt(i).widget().text() for i in range(w.pathbar.lay.count())
                  if w.pathbar.lay.itemAt(i).widget()]
        self.assertEqual(crumbs, ["My files"])
        w.navigate("/my-files/Docs")
        wait_for(lambda: w.path == "/my-files/Docs")
        self.assertEqual(w.pathbar.lay.itemAt(2).widget().text(), "Docs")
        w.pathbar.lay.itemAt(0).widget().click()
        self.assertTrue(wait_for(lambda: w.path == "/my-files"))

    def test_double_click_folder_enters(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        self.select(w, "Docs")
        w.open_selected()
        self.assertTrue(wait_for(lambda: w.path == "/my-files/Docs"))

    def test_search_filter_and_empty_states(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.search.setText("zet")
        self.assertEqual(self.names(w), ["zeta.txt"])
        w.search.setText("qqq")
        self.assertIs(w.stack.currentWidget(), w.placeholder)
        self.assertIn("No results", w.placeholder.title.text())
        w.search.setText("")
        self.assertIs(w.stack.currentWidget(), w.view)
        w.navigate("/my-files/Alfa")
        self.assertTrue(wait_for(lambda: "empty" in w.placeholder.title.text()))

    def test_error_state_does_not_raise(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.navigate("/my-files/NoExiste")
        self.assertTrue(wait_for(lambda: w._state == "error"))
        self.assertIs(w.stack.currentWidget(), w.placeholder)

    def test_not_logged_in_shows_banner_and_login_refreshes(self):
        c = FakeClient()
        c.logged_in = False
        w = self.make(c)
        self.assertTrue(wait_for(lambda: w.session_banner.isVisible()))
        self.assertEqual(w.placeholder.title.text(), "Not signed in")
        c.logged_in = True
        w.login()
        self.assertTrue(wait_for(lambda: not w.session_banner.isVisible() and w.model.rowCount() == 4))
        self.assertTrue(c.called("login"))

    def test_new_folder_rename_trash(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w._ask_text = lambda *a, **k: "Newdir"
        w.new_folder()
        self.assertTrue(wait_for(lambda: "Newdir" in self.names(w)))
        self.assertEqual(self.client.called("mkdir"), [("mkdir", "/my-files", "Newdir")])
        self.assertTrue(wait_for(lambda: w._ops == 0))
        self.select(w, "zeta.txt")
        w._ask_text = lambda *a, **k: "omega.txt"
        w.rename_selected()
        self.assertTrue(wait_for(lambda: "omega.txt" in self.names(w) and "zeta.txt" not in self.names(w)))
        self.assertTrue(wait_for(lambda: w._ops == 0))
        self.select(w, "omega.txt")
        w.trash_selected()
        self.assertTrue(wait_for(lambda: "omega.txt" not in self.names(w)))
        self.assertEqual(self.client.called("trash"), [("trash", "/my-files/omega.txt")])

    def test_confirm_trash_respected(self):
        w = self.make(cfg={"confirm_trash": True})
        asked = []
        w._confirm = lambda *a: asked.append(a) or False
        wait_for(lambda: w.model.rowCount() == 4)
        self.select(w, "zeta.txt")
        w.trash_selected()
        self.app.processEvents()
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.client.called("trash"), [])

    def test_copy_cut_paste_between_folders(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        self.select(w, "zeta.txt")
        w.clip_set("copy")
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        w.paste()
        self.assertTrue(wait_for(lambda: "zeta.txt" in self.names(w)))
        self.assertEqual(self.client.called("copy"), [("copy", ["/my-files/zeta.txt"], "/my-files/Docs")])
        w.navigate("/my-files")
        wait_for(lambda: w.model.rowCount() == 4)
        self.select(w, "Foto.png")
        w.clip_set("move")
        w.navigate("/my-files/Alfa")
        wait_for(lambda: w.path == "/my-files/Alfa")
        w.paste()
        self.assertTrue(wait_for(lambda: self.names(w) == ["Foto.png"]))
        self.assertEqual(self.client.called("move"), [("move", ["/my-files/Foto.png"], "/my-files/Alfa")])

    def test_upload_conflicts_ask_and_groups(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        src = self.tmp / "src"
        src.mkdir()
        for n in ("zeta.txt", "newfile.txt"):
            (src / n).write_text("x")
        import pdrive.gui.window as win
        asked = []
        orig = win.resolve_conflicts
        win.resolve_conflicts = lambda parent, clash, where, default="ask": asked.append(clash) or {n: "replace" for n in clash}
        self.addCleanup(setattr, win, "resolve_conflicts", orig)
        w.start_upload([src / "zeta.txt", src / "newfile.txt"])
        self.assertTrue(wait_for(lambda: len(self.client.called("upload")) == 2))
        self.assertEqual(asked, [["zeta.txt"]])
        ups = {c[3]: c[1] for c in self.client.called("upload")}
        self.assertEqual(ups, {"replace": ["zeta.txt"], "rename": ["newfile.txt"]})
        self.assertTrue(wait_for(lambda: all(t.state == "done" for t in w.transfers.items)))
        self.assertTrue(wait_for(lambda: "newfile.txt" in self.names(w)))

    def test_upload_default_strategy_skips_check(self):
        w = self.make(cfg={"on_conflict": "skip"})
        wait_for(lambda: w.model.rowCount() == 4)
        f = self.tmp / "f.txt"
        f.write_text("x")
        w.start_upload([f])
        self.assertTrue(wait_for(lambda: self.client.called("upload") == [("upload", ["f.txt"], "/my-files", "skip")]))

    def test_resolve_conflicts_default_and_dialog_apply_all(self):
        from pdrive.gui import transfers as T
        self.assertEqual(T.resolve_conflicts(None, ["a", "b"], "x", "skip"), {"a": "skip", "b": "skip"})
        orig = T.ConflictDialog.exec

        def fake_exec(self_):
            self_.choice = "replace"
            self_.all.setChecked(True)
            return 1
        T.ConflictDialog.exec = fake_exec
        self.addCleanup(setattr, T.ConflictDialog, "exec", orig)
        self.assertEqual(T.resolve_conflicts(None, ["a", "b", "c"], "x"), {"a": "replace", "b": "replace", "c": "replace"})
        T.ConflictDialog.exec = lambda s: 0
        self.assertIsNone(T.resolve_conflicts(None, ["a"], "x"))

    def test_drop_files_on_window_and_on_folder_row(self):
        w = self.make(cfg={"on_conflict": "rename"})
        wait_for(lambda: w.model.rowCount() == 4)
        f = self.tmp / "drop.txt"
        f.write_text("x")
        w.view.filesDropped.emit([f], "/my-files/Docs")
        self.assertTrue(wait_for(lambda: ("upload", ["drop.txt"], "/my-files/Docs", "rename") in self.client.calls))
        w.view.filesDropped.emit([f], "")
        self.assertTrue(wait_for(lambda: ("upload", ["drop.txt"], "/my-files", "rename") in self.client.calls))

    def test_transfer_queue_max_two_parallel(self):
        from pdrive.gui.transfers import TransferManager
        mgr = TransferManager()
        peak = [0]
        cur = [0]
        lock = threading.Lock()

        def job():
            with lock:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(0.15)
            with lock:
                cur[0] -= 1

        ts = [mgr.enqueue("upload", f"f{i}", "d", job) for i in range(5)]
        self.assertTrue(wait_for(lambda: all(t.state == "done" for t in ts), 10))
        self.assertEqual(peak[0], 2)

    def test_transfer_error_retry_and_cancel(self):
        from pdrive.gui.transfers import TransferManager, TransferPanel
        mgr = TransferManager()
        panel = TransferPanel(mgr)
        ok = [False]

        def flaky():
            if not ok[0]:
                raise DriveError("boom")

        t = mgr.enqueue("upload", "x", "d", flaky)
        self.assertTrue(wait_for(lambda: t.state == "error"))
        self.assertEqual(t.message, "boom")
        self.assertIn("boom", panel.rows[t.id][1].status.toolTip())
        ok[0] = True
        mgr.retry(t)
        self.assertTrue(wait_for(lambda: t.state == "done"))
        mgr.clear_finished()
        self.assertEqual(mgr.items, [])
        self.assertEqual(panel.stack.currentIndex(), 0)

    def test_transfer_not_logged_in_emits_session_lost(self):
        from pdrive.gui.transfers import TransferManager
        mgr = TransferManager()
        got = []
        mgr.session_lost.connect(lambda: got.append(1))

        def boom():
            raise NotLoggedIn("x")

        mgr.enqueue("upload", "x", "d", boom)
        self.assertTrue(wait_for(lambda: got))

    def test_download_to_folder(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        dest = self.tmp / "dl"
        dest.mkdir()
        w.start_download([E("/my-files/zeta.txt")], dest)
        self.assertTrue(wait_for(lambda: (dest / "zeta.txt").exists()))
        self.assertTrue(wait_for(lambda: w.transfers.items[0].state == "done"))

    def test_open_file_downloads_to_temp_and_opens(self):
        from PySide6.QtGui import QDesktopServices
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        opened = []
        orig = QDesktopServices.openUrl
        QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()) or True)
        self.addCleanup(setattr, QDesktopServices, "openUrl", orig)
        self.select(w, "zeta.txt")
        w.open_selected()
        self.assertTrue(wait_for(lambda: opened))
        self.assertTrue(opened[0].endswith("zeta.txt"))

    def test_trash_view_restore_delete_empty(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.show_trash()
        self.assertTrue(wait_for(lambda: self.names(w) == ["viejo.txt"]))
        self.assertTrue(w.a_restore.isEnabled() is False)
        self.select(w, "viejo.txt")
        self.assertTrue(w.a_restore.isEnabled() and w.a_delete.isEnabled() and w.a_empty.isEnabled())
        self.assertFalse(w.a_upload.isEnabled())
        w.restore_selected()
        self.assertTrue(wait_for(lambda: "Trash is empty" == w.placeholder.title.text()))
        self.assertEqual(self.client.called("restore"), [("restore", "/trash/viejo.txt")])
        self.client.trash_items = [E("/trash/a"), E("/trash/b")]
        w.refresh()
        wait_for(lambda: w.model.rowCount() == 2)
        self.select(w, "a")
        w.delete_selected()
        self.assertTrue(wait_for(lambda: self.names(w) == ["b"]))
        confirms = []
        w._confirm = lambda *a: confirms.append(a) or False
        w.empty_trash()
        self.assertEqual(len(confirms), 1)
        self.assertIn("permanently", confirms[0][1])
        self.assertEqual(self.client.called("empty_trash"), [])
        w._confirm = lambda *a: True
        w.empty_trash()
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 0))

    def test_copy_link_creates_when_missing_and_reuses_existing(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        self.select(w, "zeta.txt")
        w.copy_link()
        self.assertTrue(wait_for(lambda: QGuiApplication.clipboard().text().startswith("https://drive.proton.me/")))
        self.assertEqual(len(self.client.called("link")), 1)
        self.assertTrue(wait_for(lambda: w._ops == 0))
        QGuiApplication.clipboard().setText("")
        self.client.urls["/my-files/Foto.png"] = "https://drive.proton.me/urls/EXIST#k"
        self.select(w, "Foto.png")
        w.copy_link()
        self.assertTrue(wait_for(lambda: QGuiApplication.clipboard().text().endswith("EXIST#k")))
        self.assertEqual(len(self.client.called("link")), 1)
        self.assertTrue(w.a_unlink.isEnabled())
        w.remove_link()
        self.assertTrue(wait_for(lambda: self.client.called("unlink")))

    def test_calc_folder_size(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.calc_size()
        self.assertTrue(wait_for(lambda: "Folder size" in w.btn_size.text()))

    def test_dialogs_degrade_when_missing(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        self.select(w, "zeta.txt")
        import pdrive.gui as pkg
        saved_mod, saved_attr = sys.modules.get("pdrive.gui.dialogs"), getattr(pkg, "dialogs", None)
        sys.modules["pdrive.gui.dialogs"] = None  # the import fails as if the file did not exist
        if hasattr(pkg, "dialogs"):
            del pkg.dialogs
        try:
            w.share_selected()
            self.assertTrue(w.notice_banner.isVisible())
        finally:
            if saved_mod is not None:
                sys.modules["pdrive.gui.dialogs"] = saved_mod
            else:
                sys.modules.pop("pdrive.gui.dialogs", None)
            if saved_attr is not None:
                pkg.dialogs = saved_attr

    def test_dialogs_called_with_contract_signatures(self):
        from pdrive.gui import dialogs
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        seen = []

        class Dummy:
            def __init__(self, *a):
                seen.append(a)

            def exec(self):
                return 0

        names = ("ShareDialog", "PropertiesDialog")
        origs = {n: getattr(dialogs, n) for n in names}
        for n in names:
            setattr(dialogs, n, Dummy)
            self.addCleanup(setattr, dialogs, n, origs[n])
        self.select(w, "Foto.png")
        w.share_selected()
        w.properties_selected()
        self.assertEqual(seen[0][:2], (self.client, "/my-files/Foto.png"))
        self.assertEqual(seen[1][0], self.client)
        self.assertEqual(seen[1][1].name, "Foto.png")
        self.assertEqual(seen[1][2], "/my-files/Foto.png")

    def test_settings_saved(self):
        from pdrive.gui import dialogs
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        saved = []

        class Dummy:
            def __init__(self, cfg, parent=None):
                self.cfg = cfg

            def exec(self):
                return 1

            def result_cfg(self):
                return {**self.cfg, "on_conflict": "skip"}

        o1, o2 = dialogs.SettingsDialog, dialogs.save_user_config
        dialogs.SettingsDialog, dialogs.save_user_config = Dummy, saved.append
        self.addCleanup(lambda: (setattr(dialogs, "SettingsDialog", o1), setattr(dialogs, "save_user_config", o2)))
        w.open_settings()
        self.assertEqual(saved[0]["on_conflict"], "skip")
        self.assertEqual(w._conflict_default(), "skip")

    def test_settings_persistence_and_clean_close(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.navigate("/my-files/Docs")
        wait_for(lambda: w.path == "/my-files/Docs")
        w.close()
        self.assertEqual(w.settings.value("lastPath"), "/my-files/Docs")
        from pdrive.gui.window import MainWindow
        w2 = MainWindow(self.client, {}, w.settings)
        self.addCleanup(w2.close)
        self.assertTrue(wait_for(lambda: w2.path == "/my-files/Docs"))

    def test_logout_shows_login_state(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.logout()
        self.assertTrue(wait_for(lambda: w.session_banner.isVisible()))
        self.assertEqual(w.model.rowCount(), 0)

    def test_context_menu_builds_and_pathbar_edit(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        titles = []
        self.select(w, "zeta.txt")
        titles.append([x.text() for x in w._build_menu().actions() if x.isEnabled()])
        w.view.selectionModel().clearSelection()
        titles.append([x.text() for x in w._build_menu().actions() if x.isEnabled()])
        w.show_trash()
        wait_for(lambda: w.path == "/trash" and w.model.rowCount() == 1)
        titles.append([x.text() for x in w._build_menu().actions() if x.isEnabled()])
        self.assertIn("Rename…", titles[0])
        self.assertIn("New folder…", titles[1])
        self.assertIn("Empty Trash", titles[2])
        w.pathbar.edit()
        w.pathbar.edit_box.setText("/my-files/Docs")
        w.pathbar.edit_box.returnPressed.emit()
        self.assertTrue(wait_for(lambda: w.path == "/my-files/Docs"))

    def test_delete_key_in_trash_asks_permanent_delete(self):
        from PySide6.QtTest import QTest
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        w.show_trash()
        wait_for(lambda: w.model.rowCount() == 1)
        self.select(w, "viejo.txt")
        asked = []
        w._confirm = lambda *a: asked.append(a[0]) or False
        w.view.setFocus()
        QTest.keyClick(w.view, Qt.Key.Key_Delete)
        self.assertEqual(asked, ["Delete permanently"])

    def test_actions_enabled_state(self):
        w = self.make()
        wait_for(lambda: w.model.rowCount() == 4)
        self.assertFalse(w.a_download.isEnabled())
        self.assertTrue(w.a_upload.isEnabled() and w.a_newdir.isEnabled())
        self.select(w, "zeta.txt")
        self.assertTrue(w.a_download.isEnabled() and w.a_share.isEnabled() and w.a_rename.isEnabled())
        self.select(w, "zeta.txt", "Docs")
        self.assertFalse(w.a_share.isEnabled())
        self.assertTrue(w.a_trash.isEnabled())


@unittest.skipUnless(HAVE_QT, "PySide6 no instalado")
class TestWindowRealClient(GuiBase):
    """The real Client against tests/fake_proton_drive (never the real binary)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pdrive-fake-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "Docs").mkdir()
        (self.root / "Docs" / "a.txt").write_text("hola")
        (self.root / "nota.txt").write_text("nota")
        env_bak = {k: os.environ.get(k) for k in ("PDRIVE_BIN", "FAKE_DRIVE_ROOT")}
        os.environ["FAKE_DRIVE_ROOT"] = str(self.root)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in env_bak.items()])
        self.real = Client(binary=str(FAKE_CLI))
        self.addCleanup(self.real.close)

    def test_browse_upload_trash_restore(self):
        w = self.make(self.real, {"on_conflict": "rename", "confirm_trash": False})
        self.assertTrue(wait_for(lambda: self.names(w) == ["Docs", "nota.txt"], 15))
        self.assertEqual([w.sidebar.item(i).text() for i in range(w.sidebar.count())][0], "My files")
        src = self.tmp / "sube.txt"
        src.write_text("x")
        w.start_upload([src])
        self.assertTrue(wait_for(lambda: "sube.txt" in self.names(w), 15))
        self.assertTrue((self.root / "sube.txt").exists())
        self.select(w, "sube.txt")
        w.trash_selected()
        self.assertTrue(wait_for(lambda: "sube.txt" not in self.names(w), 15))
        w.show_trash()
        self.assertTrue(wait_for(lambda: "sube.txt" in self.names(w), 15))
        self.select(w, "sube.txt")
        w.restore_selected()
        self.assertTrue(wait_for(lambda: "sube.txt" not in self.names(w), 15))
        w.show_trash()
        self.assertTrue(wait_for(lambda: "sube.txt" in self.names(w), 15))

    def test_conflict_check_and_rename_via_real_client(self):
        w = self.make(self.real, {"on_conflict": "ask"})
        wait_for(lambda: len(self.names(w)) == 2, 15)
        import pdrive.gui.window as win
        orig = win.resolve_conflicts
        win.resolve_conflicts = lambda parent, clash, where, default="ask": {n: "rename" for n in clash}
        self.addCleanup(setattr, win, "resolve_conflicts", orig)
        src = self.tmp / "nota.txt"
        src.write_text("distinto")
        w.start_upload([src])
        self.assertTrue(wait_for(lambda: len(self.names(w)) == 3, 15))
        self.select(w, "Docs")
        w._ask_text = lambda *a, **k: "Docs2"
        w.rename_selected()
        self.assertTrue(wait_for(lambda: "Docs2" in self.names(w), 15))


class SlowFake(FakeClient):
    """FakeClient with per-path delay, concurrency tracking and a `fast_reads` hook."""

    def __init__(self):
        super().__init__()
        self.fast_reads = False
        self.delays: dict[str, float] = {}
        self.active = 0
        self.max_active = 0
        self.recycled = 0

    def recycle_sessions(self):
        self.recycled += 1

    def list(self, remote):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delays.get(remote, 0))
            return super().list(remote)
        finally:
            with self.lock:
                self.active -= 1


@unittest.skipUnless(HAVE_QT, "PySide6 no instalado")
class TestNavigationCache(GuiBase):
    """Listing cache: stale-while-revalidate, invalidation, prefetch and races."""

    def loaded(self, client=None):
        w = self.make(client or SlowFake())
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 4 and w._state == "ok"))
        return w

    def test_revisit_is_instant_and_revalidates_quietly(self):
        w = self.loaded()
        c = self.client
        w.navigate("/my-files/Docs")
        self.assertTrue(wait_for(lambda: self.names(w) == ["a.pdf"]))
        c.delay = 0.3
        n = len(c.called("list"))
        w.go_back()
        self.assertEqual(w.model.rowCount(), 4)            # without processing events: already painted
        self.assertEqual(w._state, "ok")
        self.assertIs(w.stack.currentWidget(), w.view)
        self.assertFalse(w.progress.isVisible())           # no indicator: there was a cache
        self.assertTrue(wait_for(lambda: len(c.called("list")) == n + 1))   # and it is revalidated
        self.assertTrue(wait_for(lambda: c.active == 0))

    def test_first_visit_shows_loading_without_cache(self):
        w = self.loaded()
        self.client.delay = 0.4
        w.navigate("/my-files/Docs")
        self.assertEqual(w.model.rowCount(), 0)
        self.assertEqual(w._state, "loading")
        self.assertTrue(wait_for(lambda: self.names(w) == ["a.pdf"]))

    def test_unchanged_revalidation_does_not_reset_model(self):
        w = self.loaded()
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        resets = []
        w.model.modelReset.connect(lambda: resets.append(1))
        w.go_back()                                       # 1 reset: paint cache
        n = len(self.client.called("list"))
        wait_for(lambda: len(self.client.called("list")) > n)
        wait_for(lambda: False, 0.4)
        self.assertEqual(len(resets), 1)

    def test_changed_revalidation_keeps_selection_and_filter(self):
        w = self.loaded()
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        self.client.tree["/my-files"].append(E("/my-files/newfile.txt"))
        self.client.delay = 0.2
        w.go_back()
        self.assertEqual(w.model.rowCount(), 4)            # stale cache first
        self.select(w, "zeta.txt")
        w.search.setText("t")                               # filtro activo: zeta.txt, newfile.txt...
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 5))
        self.assertEqual([e.name for e in w.selected()], ["zeta.txt"])
        self.assertEqual(w.search.text(), "t")
        self.assertNotIn("Alfa", self.names(w))
        self.assertIn("newfile.txt", self.names(w))

    def test_late_result_of_previous_navigation_is_ignored_but_cached(self):
        c = SlowFake()
        c.delays["/my-files/Docs"] = 0.5
        w = self.loaded(c)
        w.navigate("/my-files/Docs")                       # lenta
        w.navigate("/my-files/Alfa")                       # fast one: the one that counts
        self.assertTrue(wait_for(lambda: w._state == "ok" and w.path == "/my-files/Alfa"))
        self.assertEqual(self.names(w), [])
        self.assertTrue(wait_for(lambda: "/my-files/Docs" in w.cache))   # arrived late: goes to the cache
        self.assertEqual(w.path, "/my-files/Alfa")
        self.assertEqual(self.names(w), [])                # and did not overwrite the view
        self.assertIn("empty", w.placeholder.title.text())

    def test_f5_forces_fresh_and_recycles_sessions(self):
        w = self.loaded()
        n = len(self.client.called("list"))
        self.client.tree["/my-files"].append(E("/my-files/otro.txt"))
        w.force_refresh()
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 5))
        self.assertEqual(self.client.recycled, 1)
        self.assertGreater(len(self.client.called("list")), n)

    def test_errors_with_cache_keep_data_visible(self):
        w = self.loaded()
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        w.go_back()
        self.client.tree.pop("/my-files")                  # now the server answers NotFound
        wait_for(lambda: w._state == "error")
        self.assertEqual(w._state, "error")
        self.assertNotIn("/my-files", w.cache)             # and what no longer exists is forgotten

    def test_network_error_with_cache_keeps_list_and_shows_banner(self):
        w = self.loaded()
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        orig = self.client.list

        def boom(remote):
            raise DriveError("no network")
        self.client.list = boom
        w.go_back()
        wait_for(lambda: w.notice_banner.isVisible())
        self.assertEqual(w._state, "ok")
        self.assertEqual(w.model.rowCount(), 4)
        self.client.list = orig

    def test_writes_invalidate_cache(self):
        w = self.loaded()
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        self.assertIn("/my-files/Docs", w.cache)
        w.go_up()
        wait_for(lambda: w.path == "/my-files" and w._state == "ok")
        # renaming a folder: its listing and its children's leave the cache
        self.select(w, "Docs")
        w._ask_text = lambda *a, **k: "Docs2"
        w.rename_selected()
        self.assertTrue(wait_for(lambda: "Docs2" in self.names(w)))
        self.assertNotIn("/my-files/Docs", w.cache)
        # trash: the cached Trash listing is invalidated
        w.show_trash()
        wait_for(lambda: self.names(w) == ["viejo.txt"])
        w.go_back()
        wait_for(lambda: w.path == "/my-files")
        self.select(w, "zeta.txt")
        w.trash_selected()
        self.assertTrue(wait_for(lambda: "zeta.txt" not in self.names(w)))
        self.assertNotIn("/trash", w.cache)
        w.show_trash()
        self.assertTrue(wait_for(lambda: "zeta.txt" in self.names(w)))
        # new folder and upload to a folder other than the current one
        w.go_back()
        wait_for(lambda: w.path == "/my-files")
        w.navigate("/my-files/Alfa")
        wait_for(lambda: w._state == "ok" and w.path == "/my-files/Alfa")
        w.navigate("/my-files")
        wait_for(lambda: w._state == "ok" and w.path == "/my-files")
        f = self.tmp / "sube.txt"
        f.write_text("x")
        w.start_upload([f], "/my-files/Alfa")
        self.assertTrue(wait_for(lambda: self.client.called("upload")))
        self.assertTrue(wait_for(lambda: "/my-files/Alfa" not in w.cache))
        w.navigate("/my-files/Alfa")
        self.assertTrue(wait_for(lambda: self.names(w) == ["sube.txt"]))

    def test_late_read_cannot_resurrect_stale_listing_after_write(self):
        """A listing requested before a write does not enter the cache if it arrives afterwards."""
        c = SlowFake()
        w = self.loaded(c)
        c.delays["/my-files/Alfa"] = 0.4
        w.navigate("/my-files/Alfa")                       # slow read in flight (empty list)
        wait_for(lambda: False, 0.1)                       # the slow read is already in flight
        w._invalidate(["/my-files/Alfa"])                  # a write finishes in the meantime
        self.assertTrue(wait_for(lambda: w._state == "ok" and w.path == "/my-files/Alfa"))
        self.assertNotIn("/my-files/Alfa", w.cache)

    def test_prefetch_limits_and_cancel(self):
        c = SlowFake()
        c.fast_reads = True
        c.tree["/my-files"] = [E(f"/my-files/d{i:02d}", True) for i in range(20)] + [E("/my-files/f.txt")]
        for i in range(20):
            c.tree[f"/my-files/d{i:02d}"] = [E(f"/my-files/d{i:02d}/x.txt")]
            c.delays[f"/my-files/d{i:02d}"] = 0.05
        w = self.make(c)
        self.assertTrue(wait_for(lambda: w.model.rowCount() == 21))
        self.assertTrue(wait_for(lambda: sum(f"/my-files/d{i:02d}" in w.cache for i in range(20)) == 12))
        wait_for(lambda: False, 0.3)
        self.assertEqual(sum(f"/my-files/d{i:02d}" in w.cache for i in range(20)), 12)
        self.assertLessEqual(c.max_active, 2 + 0)          # the user's load does not overlap with the prefetch
        # entering a prefetched folder is instant
        w.navigate("/my-files/d03")
        self.assertEqual(self.names(w), ["x.txt"])

    def test_prefetch_cancelled_by_navigation(self):
        c = SlowFake()
        c.fast_reads = True
        c.tree["/my-files"] = [E(f"/my-files/d{i:02d}", True) for i in range(12)]
        for i in range(12):
            c.tree[f"/my-files/d{i:02d}"] = []
            c.delays[f"/my-files/d{i:02d}"] = 0.15
        w = self.make(c)
        wait_for(lambda: w.model.rowCount() == 12)
        wait_for(lambda: len(c.called("list")) >= 3)
        w.navigate("/my-files/d11")                        # to another folder: the queue is discarded
        wait_for(lambda: False, 1.2)
        done = [r for r in c.called("list") if r[1].startswith("/my-files/d")]
        self.assertLess(len({r[1] for r in done}), 12)

    def test_no_prefetch_without_fast_reads(self):
        w = self.loaded()
        wait_for(lambda: False, 0.4)
        self.assertEqual({r[1] for r in self.client.called("list")}, {"/my-files"})

    def test_persistent_cache_paints_instantly_at_startup(self):
        from pdrive.gui.cache import ListingCache
        f = Path(tempfile.mkdtemp(prefix="pdrive-cache-")) / "listings.json"
        self.addCleanup(shutil.rmtree, f.parent, True)
        w = self.make_cached(SlowFake(), ListingCache(f))
        wait_for(lambda: w.model.rowCount() == 4 and w._state == "ok")
        w.navigate("/my-files/Docs")
        wait_for(lambda: self.names(w) == ["a.pdf"])
        w.close()
        self.assertTrue(f.exists())
        c2 = SlowFake()
        c2.delay = 0.8
        w2 = self.make_cached(c2, ListingCache(f), settings="s2.ini", last="/my-files/Docs")
        self.assertTrue(wait_for(lambda: w2.path == "/my-files/Docs" and w2.model.rowCount() == 1, 0.5))
        self.assertEqual(w2._state, "ok")                  # painted from disk, without waiting for the Drive
        self.assertGreaterEqual(w2.sidebar.count(), 3)     # sections cached too
        self.assertTrue(wait_for(lambda: len(c2.called("list")) >= 1))

    def make_cached(self, client, cache, settings="s.ini", last=None):
        from pdrive.gui.window import MainWindow
        self.tmp = Path(tempfile.mkdtemp(prefix="pdrive-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = client
        st = QSettings(str(self.tmp / settings), QSettings.Format.IniFormat)
        if last:
            st.setValue("lastPath", last)
        w = MainWindow(client, {"on_conflict": "ask", "confirm_trash": False}, st, cache=cache)
        self.addCleanup(self._close, w)
        w._confirm = lambda *a, **k: True
        w.show()
        return w

    def test_logout_clears_cache(self):
        w = self.loaded()
        self.assertGreater(len(w.cache), 0)
        w.logout()
        self.assertTrue(wait_for(lambda: len(w.cache) == 0))


@unittest.skipUnless(HAVE_QT, "PySide6 no instalado")
class TestNavigationRealClient(GuiBase):
    """Persistent sessions + cache with the real Client against the fake CLI."""

    def test_sessions_used_and_writes_not_stale(self):
        root = Path(tempfile.mkdtemp(prefix="pdrive-fake-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "Docs").mkdir()
        (root / "Docs" / "a.txt").write_text("hola")
        env = {k: os.environ.get(k) for k in ("FAKE_DRIVE_ROOT", "FAKE_DRIVE_STALE", "FAKE_DRIVE_LOG")}
        log = root.parent / (root.name + ".log")
        os.environ.update(FAKE_DRIVE_ROOT=str(root), FAKE_DRIVE_STALE="1", FAKE_DRIVE_LOG=str(log))
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in env.items()])
        self.addCleanup(lambda: log.unlink() if log.exists() else None)
        client = Client(binary=str(FAKE_CLI))
        self.addCleanup(client.close)
        self.assertTrue(client.warm(wait=True))
        w = self.make(client)
        self.assertTrue(wait_for(lambda: len(self.names(w)) == 1 and w._state == "ok", 15))
        w.navigate("/my-files/Docs")
        self.assertTrue(wait_for(lambda: self.names(w) == ["a.txt"], 15))
        src = self.tmp / "b.txt"
        src.write_text("b")
        w.start_upload([src])
        self.assertTrue(wait_for(lambda: self.names(w) == ["a.txt", "b.txt"], 15))   # a stale session does not fool it
        self.assertIn("S fs list /my-files/Docs --json", log.read_text())


if __name__ == "__main__":
    unittest.main()
