"""Tests for pdrive.client against tests/fake_proton_drive (never the real binary)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from pdrive import client as C  # noqa: E402
from pdrive.client import Client, DriveError, Entry, NotFound, NotLoggedIn  # noqa: E402

FAKE = str(HERE / "fake_proton_drive")


class FakeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.drive = tmp / "drive"
        self.work = tmp / "work"
        self.drive.mkdir()
        self.work.mkdir()
        old = {k: os.environ.get(k) for k in ("FAKE_DRIVE_ROOT", "FAKE_DRIVE_LOGGED_OUT")}
        os.environ["FAKE_DRIVE_ROOT"] = str(self.drive)
        os.environ.pop("FAKE_DRIVE_LOGGED_OUT", None)
        self.addCleanup(self._restore, old)
        self.c = Client(binary=FAKE, sessions=0)

    @staticmethod
    def _restore(old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def mk(self, name, text="hello"):
        p = self.work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class TestWithFake(FakeBase):
    def test_login_state(self):
        self.assertTrue(self.c.is_logged_in())
        os.environ["FAKE_DRIVE_LOGGED_OUT"] = "1"
        self.assertFalse(self.c.is_logged_in())
        with self.assertRaises(NotLoggedIn):
            self.c.list("/my-files")

    def test_upload_list_info(self):
        f = self.mk("a.txt", "contenido")
        self.c.upload([f], "/my-files/Uploads/sub")  # creates the chain
        es = self.c.list("/my-files/Uploads/sub")
        self.assertEqual(len(es), 1)
        e = es[0]
        self.assertIsInstance(e, Entry)
        self.assertEqual((e.name, e.is_dir, e.size), ("a.txt", False, 9))
        self.assertEqual(e.path, "/my-files/Uploads/sub/a.txt")
        import hashlib
        self.assertEqual(e.sha1, hashlib.sha1(b"contenido").hexdigest())
        self.assertAlmostEqual(e.mtime, f.stat().st_mtime, delta=1)
        self.assertFalse(e.shared_by_url)
        self.assertEqual(self.c.info(e.path), e)
        folders = self.c.list("/my-files/Uploads")
        self.assertTrue(folders[0].is_dir)
        self.assertIsNone(folders[0].sha1)
        self.assertEqual(folders[0].size, 0)

    def test_notfound_exists(self):
        with self.assertRaises(NotFound):
            self.c.info("/my-files/nope")
        self.assertFalse(self.c.exists("/my-files/nope"))
        self.c.mkdir_p("/my-files/x/y/z")
        self.assertTrue(self.c.exists("/my-files/x/y/z"))
        self.c.mkdir_p("/my-files/x/y/z")  # idempotent
        self.c.mkdir("/my-files", "w")
        self.assertTrue(self.c.exists("/my-files/w"))

    def test_conflicts(self):
        d = "/my-files/D"
        self.c.upload([self.mk("f.txt", "uno")], d)
        self.c.upload([self.mk("f.txt", "dos")], d, on_conflict="skip")
        self.assertEqual([e.size for e in self.c.list(d)], [3])
        self.c.upload([self.mk("f.txt", "tres3")], d, on_conflict="rename")
        self.assertEqual(sorted(e.name for e in self.c.list(d)), ["f (1).txt", "f.txt"])
        self.c.upload([self.mk("f.txt", "cuatro")], d, on_conflict="revision")
        self.assertEqual(self.c.info(d + "/f.txt").size, 6)
        self.c.upload([self.mk("f.txt", "cinco")], d, on_conflict="replace")
        self.assertEqual(self.c.info(d + "/f.txt").size, 5)
        self.assertEqual(len(self.c.list(d)), 2)
        # identical: skipped without error even if rename is requested
        self.c.upload([self.mk("f.txt", "cinco")], d, on_conflict="rename")
        self.assertEqual(len(self.c.list(d)), 2)
        with self.assertRaises(ValueError):
            self.c.upload([self.mk("g")], d, on_conflict="bogus")

    def test_upload_folder_and_download(self):
        self.mk("tree/a.txt", "A")
        self.mk("tree/sub/b.txt", "BB")
        self.c.upload([self.work / "tree"], "/my-files/T")
        self.assertEqual(self.c.info("/my-files/T/tree/sub/b.txt").size, 2)
        out = self.work / "out"
        self.c.download("/my-files/T/tree", out)
        self.assertEqual((out / "tree" / "sub" / "b.txt").read_text(), "BB")
        # local conflict: rename does not overwrite, replace does
        (out / "tree" / "a.txt").write_text("local")
        self.c.download("/my-files/T/tree/a.txt", out / "tree", on_conflict="rename")
        self.assertEqual((out / "tree" / "a.txt").read_text(), "local")
        self.assertEqual((out / "tree" / "a (1).txt").read_text(), "A")
        self.c.download("/my-files/T/tree/a.txt", out / "tree", on_conflict="replace")
        self.assertEqual((out / "tree" / "a.txt").read_text(), "A")
        with self.assertRaises(NotFound):
            self.c.download("/my-files/T/missing", out)

    def test_trash(self):
        self.c.upload([self.mk("t.txt")], "/my-files/D")
        self.c.trash("/my-files/D/t.txt")
        self.assertFalse(self.c.exists("/my-files/D/t.txt"))
        with self.assertRaises(NotFound):
            self.c.trash("/my-files/D/t.txt")

    def test_sharing(self):
        self.c.upload([self.mk("s.txt")], "/my-files/D")
        url = self.c.link("/my-files/D/s.txt", password="pw", expiration="2030-01-01", role="editor")
        self.assertRegex(url, r"^https://drive\.proton\.me/urls/\w+#[\w-]+$")
        self.assertTrue(self.c.info("/my-files/D/s.txt").shared_by_url)
        self.assertEqual(self.c.link("/my-files/D/s.txt"), url)  # stable
        self.c.invite("/my-files/D/s.txt", ["a@example.com", "b@example.com"], role="editor")
        self.c.unlink("/my-files/D/s.txt")
        self.assertFalse(self.c.info("/my-files/D/s.txt").shared_by_url)
        with self.assertRaises(DriveError):
            self.c.unlink("/my-files/D/s.txt")

    def test_names_with_slash_and_unreadable(self):
        (self.drive / "a").mkdir()
        self.assertEqual(self.c.list("/my-files/a/"), [])
        self.assertEqual(self.c.list("/my-files")[0].path, "/my-files/a")

    def test_sections_and_trash_flow(self):
        self.assertEqual([e.path for e in self.c.sections()],
                         ["/my-files", "/shared-with-me", "/trash"])
        self.assertTrue(all(e.is_dir for e in self.c.sections()))
        self.assertEqual(self.c.list_trash(), [])
        self.c.upload([self.mk("t.txt"), self.mk("u.txt")], "/my-files/D")
        self.c.trash("/my-files/D/t.txt")
        self.c.trash("/my-files/D/u.txt")
        tr = self.c.list_trash()
        self.assertEqual(sorted(e.path for e in tr), ["/trash/t.txt", "/trash/u.txt"])
        self.assertEqual(self.c.info("/trash/t.txt").name, "t.txt")
        self.c.restore("/trash/t.txt")
        self.assertTrue(self.c.exists("/my-files/D/t.txt"))
        with self.assertRaises(DriveError):  # items outside the trash cannot be deleted
            self.c.delete("/my-files/D/t.txt")
        self.c.delete("/trash/u.txt")
        self.assertEqual(self.c.list_trash(), [])
        self.assertFalse(self.c.exists("/my-files/D/u.txt"))
        self.c.trash("/my-files/D/t.txt")
        self.c.empty_trash()
        self.assertEqual(self.c.list_trash(), [])

    def test_rename_move_copy_size(self):
        self.c.upload([self.mk("a.txt", "12345")], "/my-files/A")
        self.c.mkdir("/my-files", "B")
        self.c.rename("/my-files/A/a.txt", "b.txt")
        self.assertTrue(self.c.exists("/my-files/A/b.txt"))
        self.c.copy(["/my-files/A/b.txt"], "/my-files/B")
        self.c.move(["/my-files/A/b.txt"], "/my-files")
        self.assertTrue(self.c.exists("/my-files/B/b.txt"))
        self.assertFalse(self.c.exists("/my-files/A/b.txt"))
        with self.assertRaises(DriveError):  # already exists at the destination
            self.c.copy(["/my-files/B/b.txt"], "/my-files/B")
        self.assertEqual(self.c.size("/my-files/B/b.txt"), 5)
        self.assertEqual(self.c.size("/my-files/B"), 5)
        self.c.move([], "/my-files/B")  # no-op

    def test_sharing_status_and_remove_access(self):
        self.c.upload([self.mk("s.txt")], "/my-files/D")
        r = "/my-files/D/s.txt"
        st = self.c.sharing_status(r)
        self.assertEqual((st["members"], st["invitations"], st["url"], st["has_password"]),
                         ([], [], None, False))
        url = self.c.link(r, password="pw", expiration="2030-01-01", role="editor")
        self.c.invite(r, ["a@example.com", "b@example.com"], role="editor")
        st = self.c.sharing_status(r)
        self.assertEqual((st["url"], st["role"], st["has_password"], st["expiration"]),
                         (url, "editor", True, "2030-01-01"))
        self.assertEqual(sorted(i["email"] for i in st["invitations"]), ["a@example.com", "b@example.com"])
        self.assertEqual(st["invitations"][0]["role"], "editor")
        self.assertIn("urlAccess", st["raw"])
        self.assertTrue(self.c.info(r).shared_by_url)
        self.c.remove_access(r, ["a@example.com"])
        self.assertEqual([i["email"] for i in self.c.sharing_status(r)["invitations"]], ["b@example.com"])
        self.c.remove_access(r)  # everyone
        self.assertEqual(self.c.sharing_status(r)["invitations"], [])

    def test_logout(self):
        self.c.logout()


class TestRunnerInjection(unittest.TestCase):
    def make(self, rc=0, out="[]", err=""):
        calls = []

        def runner(argv, stdin=subprocess.DEVNULL):
            calls.append((argv, stdin))
            return rc, out, err
        return Client(binary="BIN", runner=runner), calls

    def test_flags_and_stdin(self):
        c, calls = self.make()
        c.list("/my-files")
        self.assertEqual(calls[0][0], ["BIN", "fs", "list", "/my-files", "--json"])
        self.assertIs(calls[0][1], subprocess.DEVNULL)

        c, calls = self.make(out=json.dumps({"name": {"ok": True, "value": "x"}, "type": "folder"}))
        e = c.info("/my-files/x")
        self.assertEqual(calls[0][0], ["BIN", "fs", "info", "/my-files/x", "--json"])
        self.assertTrue(e.is_dir)

    def test_upload_download_explicit_strategies(self):
        c, calls = self.make(out="{}")
        c.upload([Path("/tmp/a")], "/my-files", on_conflict="revision")
        argv = calls[-1][0]
        self.assertEqual(argv[:8], ["BIN", "fs", "upload", "-f", "create-new-revision", "-d", "merge", "/tmp/a"])
        self.assertEqual(argv[-1], "/my-files")
        with tempfile.TemporaryDirectory() as d:
            for oc, ff in (("rename", "rename"), ("replace", "remove"), ("skip", "skip")):
                c.download("/my-files/a", Path(d), on_conflict=oc)
                argv = calls[-1][0]
                self.assertEqual(argv[3:7], ["-f", ff, "-d", "skip" if oc == "skip" else "merge"])
                self.assertEqual(argv[-2:], ["/my-files/a", d])
        for argv, stdin in calls:
            self.assertIs(stdin, subprocess.DEVNULL)

    def test_error_mapping(self):
        c, _ = self.make(rc=1, err="Node not found: x\n")
        with self.assertRaises(NotFound):
            c.info("/my-files/x")
        c, _ = self.make(rc=1, err="Session expired, please log in\n")
        with self.assertRaises(NotLoggedIn):
            c.list("/my-files")
        c, _ = self.make(rc=1, err="boom")
        with self.assertRaises(DriveError) as cm:
            c.trash("/my-files/x")
        self.assertEqual(str(cm.exception), "boom")
        self.assertNotIsInstance(cm.exception, (NotFound, NotLoggedIn))

    def test_real_schema_tolerant_parse(self):
        data = [
            {"uid": "1", "name": {"ok": True, "value": "f.pdf"}, "type": "file", "isSharedByUrl": True,
             "modificationTime": "2026-09-28T16:36:43.000Z", "totalStorageSize": 311372,
             "activeRevision": {"claimedSize": 311249, "claimedModificationTime": "2026-07-21T18:21:21.506Z",
                                "claimedDigests": {"sha1": "4ab8", "sha1Verified": True}, "extra": 1},
             "unknownKey": [1, 2]},
            {"uid": "2", "name": {"ok": True, "value": "dir"}, "type": "folder",
             "modificationTime": "2026-09-28T16:36:43.000Z", "folder": {"isImported": False}},
            {"uid": "3", "name": {"ok": False, "error": "decrypt"}, "type": "file"},
            {"uid": "4", "name": {"ok": True, "value": "alb"}, "type": "album"},
            {"uid": "5", "name": "plain.txt", "type": "file"},  # name as str
        ]
        c, _ = self.make(out=json.dumps(data))
        es = c.list("/my-files/")
        self.assertEqual([e.name for e in es], ["f.pdf", "dir", "plain.txt"])
        f = es[0]
        self.assertEqual((f.path, f.size, f.sha1, f.shared_by_url, f.is_dir),
                         ("/my-files/f.pdf", 311249, "4ab8", True, False))
        self.assertAlmostEqual(f.mtime, 1784658081.506, places=2)
        self.assertEqual((es[1].is_dir, es[1].size, es[1].sha1), (True, 0, None))
        self.assertEqual(es[2].size, 0)

    def test_bad_json(self):
        c, _ = self.make(out="no json")
        with self.assertRaises(DriveError):
            c.list("/my-files")

    def test_link_url_extraction(self):
        for out in ("Link: https://drive.proton.me/urls/AB#cd\n",
                    json.dumps({"link": {"url": "https://drive.proton.me/urls/AB#cd"}}),
                    '{"url":"https:\\/\\/drive.proton.me\\/urls\\/AB#cd"}'):
            c, _ = self.make(out=out)
            self.assertEqual(c.link("/my-files/x"), "https://drive.proton.me/urls/AB#cd")
        c, _ = self.make(out="created")
        with self.assertRaises(DriveError):
            c.link("/my-files/x")

    def test_new_commands_argv(self):
        c, calls = self.make(out="{}")
        c.logout(); c.rename("/my-files/a", "b"); c.move(["/my-files/a", "/my-files/b"], "/my-files/c")
        c.copy(["/my-files/a"], "/my-files/c"); c.restore("/trash/a"); c.delete("/trash/a"); c.empty_trash()
        c.remove_access("/my-files/a", ["x@e.com", "y@e.com"]); c.remove_access("/my-files/a")
        self.assertEqual([a for a, _ in calls], [
            ["BIN", "auth", "logout"],
            ["BIN", "fs", "rename", "/my-files/a", "b"],
            ["BIN", "fs", "move", "/my-files/a", "/my-files/b", "/my-files/c"],
            ["BIN", "fs", "copy", "/my-files/a", "/my-files/c"],
            ["BIN", "fs", "restore", "/trash/a"],
            ["BIN", "fs", "delete", "/trash/a"],
            ["BIN", "fs", "empty-trash"],
            ["BIN", "sharing", "remove", "-e", "x@e.com", "-e", "y@e.com", "/my-files/a"],
            ["BIN", "sharing", "remove", "-a", "/my-files/a"],
        ])

    def test_real_sections_size_and_trash(self):
        c, calls = self.make(out='[{"path":"/my-files"},{"path":"/devices"},{"path":"/trash"}]')
        self.assertEqual([(e.name, e.path, e.is_dir) for e in c.sections()],
                         [("my-files", "/my-files", True), ("devices", "/devices", True),
                          ("trash", "/trash", True)])
        self.assertEqual(calls[0][0], ["BIN", "fs", "list", "/", "--json"])
        c, calls = self.make(out='{"size":9873279,"numberOfDescendants":79}')
        self.assertEqual(c.size("/my-files/x"), 9873279)
        self.assertEqual(calls[0][0], ["BIN", "fs", "size", "/my-files/x", "--json"])
        c, _ = self.make(rc=1, err="Node not found: trash")
        self.assertEqual(c.list_trash(), [])
        c, _ = self.make(out='[{"name":{"ok":true,"value":"example.pdf"},"type":"file","trashTime":"2026-01-01T00:00:00.000Z","totalStorageSize":311372}]')
        self.assertEqual(c.list_trash()[0].path, "/trash/example.pdf")

    def test_devices_listed_as_folders(self):
        c, _ = self.make(out='[{"uid":"u","type":"Windows","rootFolderUid":"r","name":{"ok":true,"value":"PC-EXAMPLE"}}]')
        e = c.list("/devices")[0]
        self.assertEqual((e.name, e.path, e.is_dir), ("PC-EXAMPLE", "/devices/PC-EXAMPLE", True))

    def test_unlink_not_implemented_is_explained(self):
        c, _ = self.make(rc=1, err="Not implemented")
        with self.assertRaises(DriveError) as cm:
            c.unlink("/my-files/a")
        self.assertIn("cannot remove public links", str(cm.exception))
        self.assertIn("web app", str(cm.exception))

    def test_real_sharing_status_parse(self):
        real = {"protonInvitations": [], "nonProtonInvitations": [], "members": [],
                "urlAccess": {"uid": "u", "creationTime": "2026-01-01T00:00:00.000Z", "role": "editor",
                              "url": "https://drive.proton.me/urls/EXAMPLE#key",
                              "creatorEmail": "user@example.com", "numberOfInitializedDownloads": 4},
                "editorsCanShare": True}
        c, calls = self.make(out=json.dumps(real))
        st = c.sharing_status("/my-files/example-folder")
        self.assertEqual(calls[0][0], ["BIN", "sharing", "status", "/my-files/example-folder", "--json"])
        self.assertEqual((st["url"], st["role"], st["has_password"], st["expiration"], st["members"]),
                         ("https://drive.proton.me/urls/EXAMPLE#key", "editor", False, None, []))
        self.assertEqual(st["raw"], real)
        # members/invitations with different shapes and without keys
        c, _ = self.make(out=json.dumps({
            "members": [{"user": {"email": "m@e.com"}, "directRole": "admin"}, "s@e.com", 7],
            "protonInvitations": [{"inviteeEmail": "i@e.com", "role": "viewer"}],
            "nonProtonInvitations": [{"email": "n@e.com", "role": "editor"}],
            "urlAccess": {"url": "https://drive.proton.me/urls/X#y", "customPassword": "pw",
                          "expirationTime": "2030-01-01T00:00:00.000Z"}}))
        st = c.sharing_status("/my-files/x")
        self.assertEqual(st["members"], [{"email": "m@e.com", "role": "admin"}, {"email": "s@e.com", "role": None}])
        self.assertEqual([(i["email"], i["role"]) for i in st["invitations"]],
                         [("i@e.com", "viewer"), ("n@e.com", "editor")])
        self.assertTrue(st["has_password"])
        self.assertTrue(st["expiration"].startswith("2030-01-01"))
        c, _ = self.make(out="[]")  # unexpected shape: everything empty
        self.assertEqual(c.sharing_status("/my-files/x")["members"], [])

    def test_missing_binary(self):
        c = Client(binary="/nonexistent/proton-drive")
        with self.assertRaises(DriveError):
            c.is_logged_in()


class TestSessions(FakeBase):
    """Persistent sessions (CLI interactive mode) against the interactive fake."""

    def setUp(self):
        super().setUp()
        self.log = Path(self._tmp.name) / "cmds.log"
        self._env = {k: os.environ.get(k) for k in (
            "FAKE_DRIVE_LOG", "FAKE_DRIVE_STALE", "FAKE_DRIVE_DIE_AFTER",
            "FAKE_DRIVE_START_DELAY", "FAKE_DRIVE_HANG_ON")}
        self.addCleanup(self._restore, self._env)
        os.environ["FAKE_DRIVE_LOG"] = str(self.log)
        for k in ("FAKE_DRIVE_STALE", "FAKE_DRIVE_DIE_AFTER", "FAKE_DRIVE_START_DELAY",
                  "FAKE_DRIVE_HANG_ON"):
            os.environ.pop(k, None)
        self.sc = None

    def client(self, **kw) -> Client:
        self.sc = Client(binary=FAKE, **kw)
        self.addCleanup(self.sc.close)
        return self.sc

    def cmds(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def seed(self):
        self.c.upload([self.mk("a.txt", "A"), self.mk("b.txt", "BB")], "/my-files/D")

    def test_reads_use_session_and_match_subprocess(self):
        self.seed()
        sc = self.client()
        self.assertTrue(sc.warm(wait=True))
        self.log.write_text("")
        self.assertEqual(sc.list("/my-files/D"), self.c.list("/my-files/D"))
        self.assertEqual(sc.info("/my-files/D/a.txt"), self.c.info("/my-files/D/a.txt"))
        self.assertEqual(sc.size("/my-files/D"), 3)
        self.assertEqual(sc.sections(), self.c.sections())
        self.assertEqual(sc.sharing_status("/my-files/D")["members"], [])
        self.assertEqual(sc.list_trash(), [])
        c = [x for x in self.cmds() if x.startswith("S ")]
        self.assertEqual(len(c), 6, self.cmds())
        self.assertIn("S fs list /my-files/D --json", c)

    def test_not_found_same_exception(self):
        sc = self.client()
        sc.warm(wait=True)
        self.log.write_text("")
        for fn, arg in ((sc.list, "/my-files/nope"), (sc.info, "/my-files/nope/x")):
            with self.assertRaises(NotFound) as a:
                fn(arg)
            with self.assertRaises(NotFound) as b:
                getattr(self.c, fn.__name__)(arg)
            self.assertEqual(str(a.exception), str(b.exception))
        self.assertFalse(sc.exists("/my-files/nope"))
        self.assertEqual(self.sessions_cmds(), 3)
        self.assertEqual(sc.list_trash(), [])  # /trash exists; no NotFound

    def sessions_cmds(self) -> int:
        return len([x for x in self.cmds() if x.startswith("S ")])

    def test_unsafe_args_use_subprocess(self):
        self.seed()
        self.c.mkdir("/my-files", "with space")
        sc = self.client()
        sc.warm(wait=True)
        self.log.write_text("")
        self.assertEqual(sc.list("/my-files/with space"), [])
        self.assertEqual(self.sessions_cmds(), 0)
        self.assertTrue(any(x.startswith("P ") for x in self.cmds()))

    def test_first_call_does_not_wait_for_startup(self):
        os.environ["FAKE_DRIVE_START_DELAY"] = "1.5"
        self.seed()
        sc = self.client()
        import time
        t = time.monotonic()
        self.assertEqual(len(sc.list("/my-files/D")), 2)   # via subprocess, without waiting
        self.assertLess(time.monotonic() - t, 1.2)
        self.assertEqual(self.sessions_cmds(), 0)
        self.assertTrue(sc.warm(wait=True))
        self.assertEqual(len(sc.list("/my-files/D")), 2)
        self.assertEqual(self.sessions_cmds(), 1)

    def test_write_recycles_sessions_no_stale_data(self):
        os.environ["FAKE_DRIVE_STALE"] = "1"  # the fake caches `fs list` forever
        self.seed()
        sc = self.client()
        sc.warm(wait=True)
        self.assertEqual(len(sc.list("/my-files/D")), 2)
        sc.upload([self.mk("c.txt", "C")], "/my-files/D")
        self.assertEqual(sorted(e.name for e in sc.list("/my-files/D")),
                         ["a.txt", "b.txt", "c.txt"])
        for fn in (lambda: sc.rename("/my-files/D/c.txt", "d.txt"),
                   lambda: sc.trash("/my-files/D/d.txt"),
                   lambda: sc.mkdir("/my-files/D", "sub")):
            fn()
            self.assertIn(len(sc.list("/my-files/D")), (2, 3))
        self.assertEqual(sorted(e.name for e in sc.list("/my-files/D")), ["a.txt", "b.txt", "sub"])
        # and the sessions are available again after recycling
        self.assertTrue(sc.warm(wait=True))
        self.log.write_text("")
        sc.list("/my-files/D")
        self.assertEqual(self.sessions_cmds(), 1)

    def test_recycle_sessions_explicit(self):
        os.environ["FAKE_DRIVE_STALE"] = "1"
        self.seed()
        sc = self.client()
        sc.warm(wait=True)
        sc.list("/my-files/D")
        self.c.upload([self.mk("z.txt")], "/my-files/D")   # another client writes
        self.assertEqual(len(sc.list("/my-files/D")), 2)   # the session is stale
        sc.recycle_sessions()
        self.assertEqual(len(sc.list("/my-files/D")), 3)   # F5: fresh

    def test_session_death_falls_back_and_restarts(self):
        os.environ["FAKE_DRIVE_DIE_AFTER"] = "1"
        self.seed()
        sc = self.client()
        sc.warm(wait=True)
        for _ in range(4):
            self.assertEqual(len(sc.list("/my-files/D")), 2)

    def test_hang_times_out_and_falls_back(self):
        os.environ["FAKE_DRIVE_HANG_ON"] = "/my-files/D"
        self.seed()
        old = C._SESSION_CMD_TIMEOUT
        C._SESSION_CMD_TIMEOUT = 0.6
        self.addCleanup(setattr, C, "_SESSION_CMD_TIMEOUT", old)
        sc = self.client()
        sc.warm(wait=True)
        self.assertEqual(len(sc.list("/my-files/D")), 2)
        self.assertEqual(sc.sections(), self.c.sections())  # another command still works

    def test_parallel_reads(self):
        from concurrent.futures import ThreadPoolExecutor
        self.seed()
        sc = self.client()
        sc.warm(wait=True)
        self.log.write_text("")
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda i: sc.list("/my-files/D"), range(24)))
        self.assertTrue(all(len(r) == 2 for r in res))
        self.assertEqual(self.sessions_cmds(), 24)

    def test_close_kills_processes_and_stops_using_them(self):
        sc = self.client()
        sc.warm(wait=True)
        procs = [s.proc for s in sc._sessions]
        self.assertTrue(all(p and p.poll() is None for p in procs))
        sc.close()
        self.assertTrue(all(p.poll() is not None for p in procs))
        sc.close()  # idempotent
        self.seed()
        self.assertEqual(len(sc.list("/my-files/D")), 2)  # subprocess

    def test_disabled_and_injected_runner(self):
        self.assertEqual(Client(binary=FAKE, sessions=0)._sessions, [])
        self.assertEqual(Client(binary="BIN", runner=lambda *a, **k: (0, "[]", ""))._sessions, [])
        self.assertEqual(len(Client(binary=FAKE)._sessions), 2)
        self.assertEqual(len(Client(binary=FAKE, sessions=1)._sessions), 1)

    def test_logged_out_raises_notloggedin(self):
        sc = self.client()
        sc.warm(wait=True)
        os.environ["FAKE_DRIVE_LOGGED_OUT"] = "1"   # the fake reads it per subprocess
        self.addCleanup(os.environ.pop, "FAKE_DRIVE_LOGGED_OUT", None)
        sc.close()
        with self.assertRaises(NotLoggedIn):
            sc.list("/my-files")

    def test_dead_binary_never_breaks_reads(self):
        sc = Client(binary="/nonexistent/proton-drive")
        self.addCleanup(sc.close)
        with self.assertRaises(DriveError):
            sc.list("/my-files")
        self.assertFalse(sc.warm(wait=True, timeout=3))


class TestSharingUndefined(unittest.TestCase):
    def test_undefined_means_not_shared(self):
        c = Client(binary="BIN", runner=lambda argv, stdin=None: (0, "undefined\n", ""))
        st = c.sharing_status("/my-files/x")
        self.assertEqual((st["url"], st["members"], st["invitations"]), (None, [], []))
        c = Client(binary="BIN", runner=lambda argv, stdin=None: (0, "<html>", ""))
        with self.assertRaises(DriveError):
            c.sharing_status("/my-files/x")


if __name__ == "__main__":
    unittest.main()
