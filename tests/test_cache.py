"""Tests for pdrive.gui.cache.ListingCache (no Qt)."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdrive.client import Entry  # noqa: E402
from pdrive.gui.cache import MAX_PERSISTED, VERSION, ListingCache, default_path  # noqa: E402


def E(name, parent="/my-files", d=False):
    return Entry(name, f"{parent}/{name}", d, 5, 1.5, None if d else "ab", False)


class TestMemory(unittest.TestCase):
    def test_put_get_copy(self):
        c = ListingCache()
        self.assertIsNone(c.get("/my-files"))
        es = [E("a"), E("b", d=True)]
        c.put("/my-files/", es, ts=10.0)
        got, ts = c.get("/my-files")
        self.assertEqual((got, ts), (es, 10.0))
        got.append(E("zzz"))           # mutating the copy does not affect the cache
        es.append(E("yyy"))
        self.assertEqual(len(c.get("/my-files/")[0]), 2)
        self.assertIn("/my-files", c)
        self.assertEqual(len(c), 1)

    def test_invalidate(self):
        c = ListingCache()
        for p in ("/my-files", "/my-files/a", "/my-files/a/b", "/my-files/ab", "/trash"):
            c.put(p, [E("x")])
        c.invalidate("/my-files/a")
        self.assertNotIn("/my-files/a", c)
        self.assertIn("/my-files/a/b", c)
        c.invalidate_tree("/my-files/a")
        self.assertNotIn("/my-files/a/b", c)
        self.assertIn("/my-files/ab", c)   # name prefix != descendant
        c.invalidate_tree("/my-files")
        self.assertEqual(len(c), 1)
        c.invalidate_tree("/")
        self.assertEqual(len(c), 0)
        c.put("/x", [])
        c.clear()
        self.assertEqual(len(c), 0)

    def test_thread_safety(self):
        c = ListingCache()
        errs = []

        def work(i):
            try:
                for j in range(300):
                    c.put(f"/my-files/{j % 20}", [E(str(i))])
                    c.get(f"/my-files/{j % 20}")
                    if j % 7 == 0:
                        c.invalidate_tree("/my-files/3")
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])


class TestTokens(unittest.TestCase):
    """put(token=...) discards listings requested before a later invalidation."""

    def test_exact_and_tree(self):
        c = ListingCache()
        t = c.token()
        c.invalidate("/my-files/a")
        self.assertFalse(c.put("/my-files/a", [E("x")], token=t))
        self.assertTrue(c.put("/my-files/a/b", [E("x")], token=t))   # exact: does not affect children
        self.assertTrue(c.put("/my-files", [E("x")], token=t))
        c.invalidate_tree("/my-files/a")
        self.assertFalse(c.put("/my-files/a/b", [E("x")], token=t))
        self.assertTrue(c.put("/my-files/ab", [E("x")], token=t))
        self.assertTrue(c.put("/my-files/a", [E("x")], token=c.token()))   # new token: accepted
        self.assertTrue(c.put("/my-files/a", [E("y")]))                     # no token: always

    def test_clear_and_root(self):
        c = ListingCache()
        t = c.token()
        c.clear()
        self.assertFalse(c.put("/my-files", [], token=t))
        t = c.token()
        c.invalidate_tree("/")
        self.assertFalse(c.put("/x/y", [], token=t))

    def test_history_overflow_is_conservative(self):
        c = ListingCache()
        t = c.token()
        for i in range(300):
            c.invalidate(f"/other/{i}")
        self.assertFalse(c.put("/my-files", [], token=t))
        self.assertTrue(c.put("/my-files", [], token=c.token()))


class TestDisk(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.f = Path(self._t.name) / "sub" / "listings.json"

    def test_roundtrip(self):
        c = ListingCache(self.f)
        c.put("/my-files", [E("a"), E("é ñ", d=True)], ts=5.0)
        c.put("/my-files/x", [], ts=6.0)
        self.assertTrue(c.dirty)
        self.assertTrue(c.save())
        self.assertFalse(c.dirty)
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)
        d = ListingCache(self.f)
        self.assertEqual(d.load(), 2)
        self.assertEqual(d.get("/my-files"), ([E("a"), E("é ñ", d=True)], 5.0))
        self.assertEqual(d.get("/my-files/x"), ([], 6.0))
        self.assertFalse(d.dirty)
        self.assertEqual([p.name for p in self.f.parent.iterdir()], ["listings.json"])

    def test_tolerates_garbage(self):
        c = ListingCache(self.f)
        self.assertEqual(c.load(), 0)                      # does not exist
        self.f.parent.mkdir(parents=True)
        for txt in ("", "no json", "[]", '{"version": 99, "items": {}}',
                    json.dumps({"version": VERSION, "items": []}), "\x00\xff"):
            self.f.write_text(txt)
            self.assertEqual(c.load(), 0, txt)
        self.f.write_text(json.dumps({"version": VERSION, "items": {
            "/ok": {"ts": 1, "entries": [{"name": "a", "path": "/ok/a", "is_dir": False,
                                          "size": 1, "mtime": 2.0, "sha1": None,
                                          "shared_by_url": False}]},
            "/bad1": {"ts": "x", "entries": []},
            "/bad2": {"ts": 1, "entries": [{"name": "a"}]},
            "/bad3": 7,
            "nopath": {"ts": 1, "entries": []}}}))
        self.assertEqual(c.load(), 1)
        self.assertEqual(c.get("/ok")[0][0].name, "a")
        self.assertEqual(len(c), 1)

    def test_memory_wins_and_unwritable(self):
        c = ListingCache(self.f)
        c.put("/my-files", [E("old")], ts=1.0)
        c.save()
        d = ListingCache(self.f)
        d.put("/my-files", [E("new")], ts=2.0)
        d.load()
        self.assertEqual(d.get("/my-files")[0][0].name, "new")
        bad = ListingCache(Path(self._t.name) / "sub" / "listings.json" / "x" / "y.json")
        bad.put("/a", [])
        self.assertFalse(bad.save())                        # does not raise
        self.assertFalse(ListingCache().save())

    def test_cap_keeps_most_recent(self):
        c = ListingCache(self.f)
        for i in range(MAX_PERSISTED + 20):
            c.put(f"/my-files/{i}", [], ts=float(i))
        c.save()
        d = ListingCache(self.f)
        self.assertEqual(d.load(), MAX_PERSISTED)
        self.assertIn(f"/my-files/{MAX_PERSISTED + 19}", d)
        self.assertNotIn("/my-files/0", d)

    def test_default_path_env(self):
        import os
        old = os.environ.get("PDRIVE_CACHE_DIR")
        self.addCleanup(lambda: os.environ.__setitem__("PDRIVE_CACHE_DIR", old) if old
                        else os.environ.pop("PDRIVE_CACHE_DIR", None))
        os.environ["PDRIVE_CACHE_DIR"] = self._t.name
        self.assertEqual(default_path(), Path(self._t.name) / "listings.json")


if __name__ == "__main__":
    unittest.main()
