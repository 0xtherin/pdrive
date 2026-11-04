"""Cache of remote listings (path -> entries), thread-safe with optional persistence."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from ..client import Entry

VERSION = 1
MAX_PERSISTED = 300  # at most this many folders on disk (the most recent)


def default_path() -> Path:
    """`$PDRIVE_CACHE_DIR/listings.json` o `~/.cache/pdrive/listings.json`."""
    base = os.environ.get("PDRIVE_CACHE_DIR") or Path.home() / ".cache" / "pdrive"
    return Path(base) / "listings.json"


def norm(path: str) -> str:
    return path.rstrip("/") or "/"


class ListingCache:
    """remote path -> (entries, timestamp). `path=None`: memory only."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._d: dict[str, tuple[tuple[Entry, ...], float]] = {}
        self._lock = threading.RLock()
        self._gen = 0       # increases on every change (to know whether there is anything to save)
        self._saved_gen = 0
        self._epoch = 0     # increases on every invalidation; see token()/put()
        self._log: list[tuple[int, str, bool]] = []   # (epoch, path, tree?) of recent invalidations
        self._floor = 0     # epoch up to which the history is no longer reliable

    # -- memory ----------------------------------------------------------
    def get(self, path: str) -> tuple[list[Entry], float] | None:
        with self._lock:
            hit = self._d.get(norm(path))
        return (list(hit[0]), hit[1]) if hit else None

    def token(self) -> int:
        """Marker for `put(token=...)`: take it BEFORE requesting the listing from the Drive."""
        with self._lock:
            return self._epoch

    def put(self, path: str, entries: list[Entry], ts: float | None = None,
            token: int | None = None) -> bool:
        """Store a listing. With `token`, it is discarded (False) if this path was invalidated
        since the token was taken: the listing may be stale because of a concurrent write."""
        p = norm(path)
        with self._lock:
            if token is not None and self._invalidated_since(p, token):
                return False
            self._d[p] = (tuple(entries), time.time() if ts is None else ts)
            self._gen += 1
            return True

    def _invalidated_since(self, p: str, token: int) -> bool:
        if token < self._floor:      # history trimmed: discard to be safe
            return True
        for e, t, tree in self._log:
            if e > token and (t == p or (tree and (t == "/" or p.startswith(t.rstrip("/") + "/")))):
                return True
        return False

    def _note(self, path: str, tree: bool) -> None:
        self._epoch += 1
        self._log.append((self._epoch, path, tree))
        if len(self._log) > 256:
            self._floor = self._log[-257][0]
            del self._log[:-256]

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._note(norm(path), False)
            if self._d.pop(norm(path), None) is not None:
                self._gen += 1

    def invalidate_tree(self, path: str) -> None:
        """Remove `path` and everything under it."""
        p = norm(path)
        pre = p.rstrip("/") + "/"
        with self._lock:
            self._note(p, True)
            dead = [k for k in self._d if k == p or k.startswith(pre)]
            for k in dead:
                del self._d[k]
            if dead:
                self._gen += 1

    def clear(self) -> None:
        with self._lock:
            self._note("/", True)
            if self._d:
                self._gen += 1
            self._d.clear()

    def __contains__(self, path: str) -> bool:
        with self._lock:
            return norm(path) in self._d

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    # -- disk ------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._gen != self._saved_gen

    def load(self) -> int:
        """Read the file (tolerant of missing/corrupt/other-version files). Return how many were loaded."""
        if not self.path:
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != VERSION:
                return 0
            items = data.get("items")
            if not isinstance(items, dict):
                return 0
        except (OSError, ValueError):
            return 0
        n = 0
        loaded: dict[str, tuple[tuple[Entry, ...], float]] = {}
        for k, v in items.items():
            try:
                ts = float(v["ts"])
                es = tuple(Entry(**{**e, "size": int(e["size"]), "mtime": float(e["mtime"]),
                                    "is_dir": bool(e["is_dir"]),
                                    "shared_by_url": bool(e["shared_by_url"])})
                           for e in v["entries"])
                if not isinstance(k, str) or not k.startswith("/"):
                    raise ValueError(k)
            except (KeyError, TypeError, ValueError):
                continue  # corrupt entry: only that one is ignored
            loaded[norm(k)] = (es, ts)
            n += 1
        with self._lock:
            for k, v in loaded.items():
                self._d.setdefault(k, v)   # what is already in memory wins
            self._saved_gen = self._gen
        return n

    def save(self) -> bool:
        """Atomic write (0600). False if there is no path or it fails (never raises)."""
        if not self.path:
            return False
        with self._lock:
            gen = self._gen
            recent = sorted(self._d.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_PERSISTED]
            payload = {"version": VERSION, "items": {
                k: {"ts": ts, "entries": [asdict(e) for e in es]} for k, (es, ts) in recent}}
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".listings-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
        self._saved_gen = gen
        return True
