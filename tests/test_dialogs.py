"""Tests for pdrive.gui.dialogs (offscreen). In-memory FakeClient; never the real Drive."""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import tomllib  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdrive.client import DriveError, Entry, NotLoggedIn  # noqa: E402
from pdrive.gui import dialogs as D  # noqa: E402

APP = QApplication.instance() or QApplication([])


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        APP.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    APP.processEvents()
    return cond()


def idle(dlg):
    return wait(lambda: not dlg._busy())


class FakeClient:
    """In-memory client with the contract API; records calls."""

    def __init__(self, status=None):
        self.status = {"members": [], "invitations": [], "url": None,
                       "has_password": False, "expiration": None, "role": None} \
            if status is None else status
        self.calls = []
        self.fail = {}                 # method -> exception
        self.gate = None               # threading.Event that blocks sharing_status
        self.threads = set()
        self.url = "https://drive.proton.me/urls/ABC#xyz"
        self.sizes = {}

    def _rec(self, name, *a):
        self.calls.append((name, *a))
        self.threads.add(threading.current_thread())
        if name in self.fail:
            raise self.fail[name]

    def sharing_status(self, remote):
        if self.gate:
            self.gate.wait(5)
        self._rec("sharing_status", remote)
        return self.status

    def link(self, remote, password=None, expiration=None, role="viewer"):
        self._rec("link", remote, password, expiration, role)
        self.status = dict(self.status, url=self.url, has_password=bool(password),
                           expiration=expiration, role=role)
        return self.url

    def unlink(self, remote):
        self._rec("unlink", remote)
        self.status = dict(self.status, url=None)

    def invite(self, remote, emails, role="viewer"):
        self._rec("invite", remote, list(emails), role)
        self.status = dict(self.status, invitations=self.status.get("invitations", [])
                           + [{"email": e, "role": role} for e in emails])

    def remove_access(self, remote, emails=None):
        self._rec("remove_access", remote, emails)
        gone = set(emails or [])
        self.status = dict(
            self.status,
            members=[m for m in self.status.get("members", []) if m["email"] not in gone],
            invitations=[m for m in self.status.get("invitations", []) if m["email"] not in gone])

    def size(self, remote):
        self._rec("size", remote)
        return self.sizes.get(remote, 123456)


class Base(unittest.TestCase):
    def setUp(self):
        self.dlgs = []
        self.addCleanup(self._close)

    def _close(self):
        QThreadPool.globalInstance().waitForDone(3000)
        APP.processEvents()
        for d in self.dlgs:
            d.close()
            d.deleteLater()   # without this they stay alive at exit and QApplication segfaults when destroying them
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def share(self, client, remote="/my-files/doc.txt"):
        d = D.ShareDialog(client, remote)
        self.dlgs.append(d)
        return d

    def confirmed(self, d, answer=True):
        d._confirm = lambda text: answer


class TestEmails(unittest.TestCase):
    def test_valid_and_separators(self):
        ok, bad = D.parse_emails("a@b.com, c@d.org  e@f.io;g@h.net\nx.y+z@sub.dom.es")
        self.assertEqual(ok, ["a@b.com", "c@d.org", "e@f.io", "g@h.net", "x.y+z@sub.dom.es"])
        self.assertEqual(bad, [])

    def test_invalid_and_dedupe(self):
        ok, bad = D.parse_emails("a@b.com A@B.com nope a@b -u@x.com @x.com a@@b.com a b@c.com")
        self.assertEqual(ok, ["a@b.com", "b@c.com"])
        self.assertEqual(bad, ["nope", "a@b", "-u@x.com", "@x.com", "a@@b.com", "a"])

    def test_empty(self):
        self.assertEqual(D.parse_emails("  , ; "), ([], []))


class TestShareDialog(Base):
    def test_shows_real_status(self):
        c = FakeClient({"members": [{"email": "ana@x.com", "role": "editor"}],
                        "invitations": [{"email": "bob@x.com", "role": "viewer"}],
                        "url": "https://drive.proton.me/urls/Q#k", "has_password": True,
                        "expiration": "2030-05-06T00:00:00Z", "role": "editor"})
        d = self.share(c)
        self.assertFalse(d.link_btn.isEnabled())          # loading
        self.assertTrue(idle(d))
        self.assertEqual(d.url_edit.text(), "https://drive.proton.me/urls/Q#k")
        self.assertIn("Active", d.link_state.text())
        self.assertIn("password", d.link_state.text())
        self.assertIn("2030-05-06", d.link_state.text())
        self.assertEqual(d.role_combo.currentData(), "editor")
        self.assertFalse(d.no_expiry.isChecked())
        self.assertEqual(d.expiry.date().toString("yyyy-MM-dd"), "2030-05-06")
        self.assertEqual(d.link_btn.text(), "Update link")
        self.assertTrue(d.unlink_btn.isEnabled())
        rows = [[d.people.topLevelItem(i).text(k) for k in range(3)]
                for i in range(d.people.topLevelItemCount())]
        self.assertEqual(rows, [["ana@x.com", "Editor", "Member"],
                                ["bob@x.com", "Viewer", "Pending invitation"]])

    def test_no_link_state(self):
        d = self.share(FakeClient())
        self.assertTrue(idle(d))
        self.assertEqual(d.link_state.text(), "No public link")
        self.assertEqual(d.link_btn.text(), "Create link")
        self.assertFalse(d.unlink_btn.isEnabled())
        self.assertFalse(d.copy_btn.isEnabled())
        self.assertTrue(d.no_expiry.isChecked())

    def test_tolerates_missing_and_odd_keys(self):
        for st in ({}, {"raw": {}}, {"members": None, "invitations": "x", "url": 5},
                   {"members": ["solo@x.com", {"role": "viewer"}, {"email": "k@x.com"}, 7],
                    "expiration": "garbage", "role": "???"}, None):
            c = FakeClient(st)
            c.status = st
            d = self.share(c)
            self.assertTrue(idle(d), st)
            self.assertFalse(d.error.isVisible(), st)
        self.assertEqual(d.people.topLevelItemCount(), 0)  # st None
        d = self.share(FakeClient({"members": ["solo@x.com", {"email": "k@x.com"}]}))
        idle(d)
        self.assertEqual([d.people.topLevelItem(i).text(0) for i in range(2)],
                         ["solo@x.com", "k@x.com"])
        self.assertEqual(d.people.topLevelItem(0).text(1), "—")

    def test_status_error_shown(self):
        c = FakeClient()
        c.fail["sharing_status"] = DriveError("boom")
        d = self.share(c)
        d.show()
        self.assertTrue(idle(d))
        self.assertTrue(d.error.isVisible())
        self.assertIn("boom", d.error.text())
        self.assertTrue(d.link_btn.isEnabled())

    def test_not_logged_in_message(self):
        c = FakeClient()
        c.fail["sharing_status"] = NotLoggedIn("401")
        d = self.share(c)
        d.show()
        idle(d)
        self.assertIn("session", d.error.text())

    def test_link_params_passed(self):
        c = FakeClient()
        d = self.share(c)
        idle(d)
        d.role_combo.setCurrentIndex(d.role_combo.findData("editor"))
        d.pw_edit.setText("s3cr3t!")
        d.no_expiry.setChecked(False)
        d.expiry.setDate(D.QDate(2031, 2, 3))
        d.link_btn.click()
        self.assertFalse(d.link_btn.isEnabled())           # disabled while working
        self.assertTrue(idle(d))
        self.assertIn(("link", "/my-files/doc.txt", "s3cr3t!", "2031-02-03", "editor"), c.calls)
        self.assertEqual(d.url_edit.text(), c.url)
        self.assertEqual(d.pw_edit.text(), "")             # the password is not kept
        self.assertIn("password", d.link_state.text())
        self.assertEqual(d.link_btn.text(), "Update link")

    def test_link_defaults_none(self):
        c = FakeClient()
        d = self.share(c)
        idle(d)
        d.link_btn.click()
        idle(d)
        self.assertIn(("link", "/my-files/doc.txt", None, None, "viewer"), c.calls)

    def test_url_kept_if_status_lacks_it(self):
        c = FakeClient({})
        c.link = lambda *a, **k: c.url                     # the status never carries a url
        d = self.share(c)
        idle(d)
        d.link_btn.click()
        idle(d)
        self.assertEqual(d.url_edit.text(), c.url)
        self.assertTrue(d.unlink_btn.isEnabled())

    def test_copy_url(self):
        c = FakeClient({"url": "https://drive.proton.me/urls/Z#k"})
        d = self.share(c)
        idle(d)
        d.copy_btn.click()
        self.assertEqual(QGuiApplication.clipboard().text(), "https://drive.proton.me/urls/Z#k")

    def test_remove_link(self):
        c = FakeClient({"url": "https://drive.proton.me/urls/Z#k"})
        d = self.share(c)
        idle(d)
        self.confirmed(d, False)
        d.unlink_btn.click()
        self.assertFalse(any(x[0] == "unlink" for x in c.calls))
        self.confirmed(d, True)
        d.unlink_btn.click()
        idle(d)
        self.assertIn(("unlink", "/my-files/doc.txt"), c.calls)
        self.assertEqual(d.url_edit.text(), "")
        self.assertEqual(d.link_btn.text(), "Create link")
        self.assertFalse(d.unlink_btn.isEnabled())

    def test_link_error(self):
        c = FakeClient()
        d = self.share(c)
        d.show()
        idle(d)
        c.fail["link"] = DriveError("not allowed")
        d.link_btn.click()
        idle(d)
        self.assertTrue(d.error.isVisible())
        self.assertIn("not allowed", d.error.text())
        self.assertTrue(d.link_btn.isEnabled())

    def test_invite_validation_blocks_call(self):
        c = FakeClient()
        d = self.share(c)
        d.show()
        idle(d)
        for text in ("", "foo", "ok@x.com, bad", "-u@x.com"):
            d.email_edit.setText(text)
            d.invite_btn.click()
            self.assertTrue(d.error.isVisible(), text)
        self.assertFalse(any(x[0] == "invite" for x in c.calls))

    def test_invite_many_with_role(self):
        c = FakeClient()
        d = self.share(c)
        idle(d)
        d.invite_role.setCurrentIndex(d.invite_role.findData("admin"))
        d.email_edit.setText("a@x.com, b@x.com  A@X.com")
        d.invite_btn.click()
        idle(d)
        self.assertIn(("invite", "/my-files/doc.txt", ["a@x.com", "b@x.com"], "admin"), c.calls)
        self.assertEqual(d.email_edit.text(), "")
        self.assertEqual(d.people.topLevelItemCount(), 2)
        self.assertEqual(d.people.topLevelItem(0).text(1), "Admin")

    def test_remove_access_selected(self):
        c = FakeClient({"members": [{"email": "ana@x.com", "role": "viewer"},
                                    {"email": "bob@x.com", "role": "editor"}]})
        d = self.share(c)
        idle(d)
        self.assertFalse(d.remove_btn.isEnabled())
        d.people.topLevelItem(1).setSelected(True)
        APP.processEvents()
        self.assertTrue(d.remove_btn.isEnabled())
        self.confirmed(d, True)
        d.remove_btn.click()
        idle(d)
        self.assertIn(("remove_access", "/my-files/doc.txt", ["bob@x.com"]), c.calls)
        self.assertEqual(d.people.topLevelItemCount(), 1)

    def test_ui_not_blocked(self):
        c = FakeClient()
        c.gate = threading.Event()
        t0 = time.time()
        d = self.share(c)
        self.assertLess(time.time() - t0, 1.0)            # the constructor does not wait for the client
        self.assertFalse(d.invite_btn.isEnabled())
        self.assertFalse(d.link_btn.isEnabled())
        c.gate.set()
        self.assertTrue(idle(d))
        self.assertTrue(d.invite_btn.isEnabled())
        self.assertNotIn(threading.main_thread(), c.threads)

    def test_close_while_pending(self):
        c = FakeClient()
        c.gate = threading.Event()
        d = self.share(c)
        d.show()
        d.close()
        c.gate.set()
        QThreadPool.globalInstance().waitForDone(3000)
        APP.processEvents()                                # must not raise


class TestPropertiesDialog(Base):
    def make(self, c, e, path):
        d = D.PropertiesDialog(c, e, path)
        self.dlgs.append(d)
        return d

    def test_folder(self):
        c = FakeClient({"url": "https://drive.proton.me/urls/Q#k", "role": "viewer",
                        "members": [{"email": "ana@x.com", "role": "editor"}]})
        e = Entry("Photos", "/my-files/Photos", True, 0, 1700000000.0, None, True)
        d = self.make(c, e, "/my-files/Photos")
        self.assertTrue(wait(lambda: "Public link active" in d.share_lbl.text()
                             and "Calculating" not in d.size_lbl.text()))
        digits = "".join(ch for ch in d.size_lbl.text() if ch.isdigit())
        self.assertIn("123456", digits)
        self.assertIn("ana@x.com", d.share_lbl.text())
        self.assertIn(("size", "/my-files/Photos"), c.calls)
        self.assertEqual(d.size_btn.text(), "Recalculate")
        self.assertTrue(d.size_btn.isEnabled())

    def test_file(self):
        c = FakeClient({})
        e = Entry("a.pdf", "/my-files/a.pdf", False, 2048, 1700000000.0, "ab" * 20, False)
        d = self.make(c, e, "/my-files/a.pdf")
        self.assertTrue(wait(lambda: d.share_lbl.text() == "Not shared"))
        self.assertFalse(d.size_btn.isVisibleTo(d))
        self.assertFalse(any(x[0] == "size" for x in c.calls))
        texts = " | ".join(lb.text() for lb in d.findChildren(D.QLabel))
        self.assertIn("ab" * 20, texts)
        self.assertIn("2048", texts.replace(",", "").replace(".", "").replace(" ", ""))

    def test_errors(self):
        c = FakeClient()
        c.fail["size"] = DriveError("no permission")
        c.fail["sharing_status"] = DriveError("status failure")
        e = Entry("X", "/my-files/X", True, 0, 0.0, None, False)
        d = self.make(c, e, "/my-files/X")
        d.show()
        self.assertTrue(wait(lambda: not d._busy()))
        self.assertTrue(d.error.isVisible())
        self.assertTrue(d.size_btn.isEnabled())


class TestSettings(Base):
    def test_defaults_and_result(self):
        cfg = {"upload_dir": "/my-files/Uploads", "other": 1}
        d = D.SettingsDialog(cfg)
        self.assertEqual(d.conflict.currentData(), "ask")
        self.assertTrue(d.confirm_trash.isChecked())
        d.upload_dir.setText("/my-files/Docs/")
        d.conflict.setCurrentIndex(d.conflict.findData("replace"))
        d.confirm_trash.setChecked(False)
        d.accept()
        self.assertEqual(d.result(), D.QDialog.DialogCode.Accepted)
        r = d.result_cfg()
        self.assertEqual(r, {"upload_dir": "/my-files/Docs", "other": 1,
                             "on_conflict": "replace", "confirm_trash": False, "disk_cache": False})
        self.assertNotIn("inbox_remote", r)
        self.assertEqual(cfg, {"upload_dir": "/my-files/Uploads", "other": 1})

    def test_load_existing(self):
        d = D.SettingsDialog({"upload_dir": "/my-files/U", "on_conflict": "skip",
                              "confirm_trash": False})
        self.assertEqual(d.conflict.currentData(), "skip")
        self.assertFalse(d.confirm_trash.isChecked())

    def test_validation_rejects(self):
        for bad in ("", "Uploads", "/other/Uploads", "/my-files/../x", "/my-filesX"):
            d = D.SettingsDialog({"upload_dir": "/my-files/U"})
            d.show()
            d.upload_dir.setText(bad)
            d.accept()
            self.assertNotEqual(d.result(), D.QDialog.DialogCode.Accepted, bad)
            self.assertTrue(d.error.isVisible(), bad)
            d.close()


class TestSaveConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.environ.get("PDRIVE_CONFIG_DIR")
        os.environ["PDRIVE_CONFIG_DIR"] = str(Path(self.tmp.name) / "sub" / "pdrive")
        self.addCleanup(lambda: os.environ.__setitem__("PDRIVE_CONFIG_DIR", old)
                        if old is not None else os.environ.pop("PDRIVE_CONFIG_DIR", None))

    def read(self, p):
        with open(p, "rb") as f:
            return tomllib.load(f)

    def test_roundtrip_known_keys_only(self):
        p = D.save_user_config({"upload_dir": "/my-files/Uploads", "on_conflict": "rename",
                                "confirm_trash": False, "inbox_remote": "/x", "sync_pairs": [1],
                                "other": "z"})
        self.assertEqual(p, Path(os.environ["PDRIVE_CONFIG_DIR"]) / "config.toml")
        self.assertEqual(self.read(p), {"upload_dir": "/my-files/Uploads",
                                        "on_conflict": "rename", "confirm_trash": False})
        self.assertFalse(list(p.parent.glob("*.tmp")))

    def test_tricky_strings(self):
        for s in ('/my-files/Café "x" \\ y', "/my-files/\U0001F600 emoji", "/my-files/a\tb\x7f",
                  "/my-files/l1\nl2"):
            p = D.save_user_config({"upload_dir": s})
            self.assertEqual(self.read(p)["upload_dir"], s)

    def test_overwrites(self):
        D.save_user_config({"upload_dir": "/my-files/A"})
        p = D.save_user_config({"upload_dir": "/my-files/B"})
        self.assertEqual(self.read(p)["upload_dir"], "/my-files/B")

    def test_loadable_by_config(self):
        """config.load() reads what was written (if D makes CONFIG_DIR honor the variable)."""
        from pdrive import config
        p = D.save_user_config({"upload_dir": "/my-files/Z", "on_conflict": "skip"})
        old = config.CONFIG_DIR
        config.CONFIG_DIR = p.parent
        self.addCleanup(setattr, config, "CONFIG_DIR", old)
        cfg = config.load()
        self.assertEqual(cfg["upload_dir"], "/my-files/Z")
        self.assertEqual(cfg["on_conflict"], "skip")


class TestMisc(unittest.TestCase):
    def test_copy_text(self):
        D.copy_text("hello ñ")
        self.assertEqual(QGuiApplication.clipboard().text(), "hello ñ")

    def test_svg_renders(self):
        r = QSvgRenderer(str(ROOT / "assets" / "pdrive.svg"))
        self.assertTrue(r.isValid())
        for n in (16, 48, 256):
            im = QImage(n, n, QImage.Format.Format_ARGB32)
            im.fill(0)
            p = QPainter(im)
            r.render(p)
            p.end()
            c = im.pixelColor(n // 2, n // 2)
            self.assertGreater(c.alpha(), 0)
            self.assertEqual(im.pixelColor(n // 8, n // 2).name(), "#6d4aff")  # purple background

    def test_desktop_file(self):
        p = ROOT / "desktop" / "pdrive.desktop"
        txt = p.read_text(encoding="utf-8")
        for k in ("Type=Application", "Name=Proton Drive", "Exec=@PDRIVE@ gui", "Icon=pdrive",
                  "Terminal=false", "StartupWMClass=pdrive"):
            self.assertIn(k, txt)
        with tempfile.TemporaryDirectory() as t:
            q = Path(t) / "pdrive.desktop"
            q.write_text(txt.replace("@PDRIVE@", "/usr/bin/true"), encoding="utf-8")
            try:
                r = subprocess.run(["desktop-file-validate", str(q)], capture_output=True, text=True)
            except FileNotFoundError:
                self.skipTest("desktop-file-validate not available")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("error", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main()
