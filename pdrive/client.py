"""Client for the official proton-drive CLI. The only module that runs the binary."""
from __future__ import annotations

import atexit
import json
import os
import re
import select
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config

DEVNULL = subprocess.DEVNULL
_URL_RE = re.compile(r"https://drive\.proton\.me/[^\s\"'<>\\]+")
_LOGGED_OUT_RE = re.compile(
    r"not (logged|signed) in|log ?in|sign ?in|session|unauthori[sz]ed|expired|"
    r"authenticat|credentials|\b401\b",
    re.I,
)
_UPLOAD_FILE = {"rename": "rename", "replace": "replace", "skip": "skip",
                "revision": "create-new-revision"}
_DOWNLOAD_FILE = {"rename": "rename", "replace": "remove", "remove": "remove",
                  "skip": "skip", "revision": "remove"}
_CHUNK = 100
TRASH = "/trash"
_SAFE_ARG = re.compile(r"[A-Za-z0-9._/~@+=,-]+")  # arguments allowed in a session
_PROMPT = "proton-drive> "
_READ_CMDS = {("fs", "list"), ("fs", "info"), ("fs", "size"), ("sharing", "status")}
_SESSION_START_TIMEOUT = 25.0
_SESSION_CMD_TIMEOUT = 60.0
_SESSION_MAX_AGE = 900.0   # recycled (the session may hold cached CLI data)
_SESSION_RETRY = 10.0      # after a startup failure, do not retry before this


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    sha1: str | None
    shared_by_url: bool


class DriveError(Exception):
    """Failure running the CLI (message = stderr)."""


class NotLoggedIn(DriveError):
    """No session, or it has expired."""


class NotFound(DriveError):
    """The remote node does not exist."""


def _default_runner(argv: list[str], stdin=DEVNULL) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, stdin=stdin, capture_output=True, text=True,
                           errors="replace")
    except OSError as e:
        raise DriveError(f"could not run {argv[0]}: {e}") from e
    return p.returncode, p.stdout, p.stderr


class Session:
    """Interactive `proton-drive` process, reusable for reads (fast: ~0.1 s/command).

    Lazy, background startup: while it is not ready, callers use the normal
    subprocess. Any anomaly (death, timeout, odd output) kills the process
    and returns None so the caller falls back to the subprocess."""

    def __init__(self, binary: str):
        self.binary = binary
        self.proc: subprocess.Popen | None = None
        self.state = "new"          # new | starting | ready | dead
        self.busy = False
        self.recycle = False
        self.closed = False
        self.started = 0.0
        self.failed_at = 0.0
        self._err = bytearray()
        self._err_lock = threading.Lock()
        self._live.add(self)

    _live: "weakref.WeakSet[Session]" = weakref.WeakSet()

    # -- lifecycle --------------------------------------------------------
    def _spawn(self) -> bool:
        """Launch the process and wait for the first prompt (startup thread)."""
        try:
            self.proc = subprocess.Popen([self.binary], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         bufsize=0)
        except OSError:
            return False
        proc = self.proc
        threading.Thread(target=self._drain_err, args=(proc,), daemon=True).start()
        try:
            return self._read_to_prompt(_SESSION_START_TIMEOUT) is not None
        except OSError:
            return False

    def _drain_err(self, proc: subprocess.Popen) -> None:
        try:
            while True:
                d = os.read(proc.stderr.fileno(), 65536)
                if not d:
                    return
                if proc is self.proc:
                    with self._err_lock:
                        self._err += d
        except (OSError, ValueError):
            return

    def _read_to_prompt(self, timeout: float) -> str | None:
        """Read stdout up to the final prompt; None on timeout/EOF."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return None
        fd, buf, end = proc.stdout.fileno(), bytearray(), time.monotonic() + timeout
        pb = _PROMPT.encode()
        while True:
            if buf.endswith(pb) and (len(buf) == len(pb) or buf[-len(pb) - 1] == 10):
                return buf[:-len(pb)].decode("utf-8", "replace")
            left = end - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                return None
            d = os.read(fd, 1 << 20)
            if not d:
                return None
            buf += d

    def start_async(self, on_done=None) -> None:
        """Start in the background if appropriate (new, or dead once the retry delay has passed)."""
        if self.state == "starting" or self.state == "ready":
            return
        if self.state == "dead" and time.monotonic() - self.failed_at < _SESSION_RETRY:
            return
        self.state = "starting"

        def go() -> None:
            ok = self._spawn() and not self.closed
            if ok:
                self.started = time.monotonic()
                self.state = "ready"
            else:
                self.kill()
                self.failed_at = time.monotonic()
                self.state = "dead"
            if on_done:
                on_done()

        threading.Thread(target=go, daemon=True).start()

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if self.state != "starting":
            self.state = "dead"
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def close(self, final: bool = False) -> None:
        """Clean shutdown: EOF on stdin (the CLI exits by itself) and, failing that, kill."""
        self.closed = self.closed or final
        proc = self.proc
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(1.5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.kill()
        self.state = "new"

    # -- usage ------------------------------------------------------------------
    def request(self, args: list[str]) -> tuple[str, str] | None:
        """Run a command: (stdout, stderr), or None on anomaly (dead session)."""
        proc = self.proc
        if proc is None or proc.stdin is None:
            return None
        with self._err_lock:
            self._err.clear()
        try:
            proc.stdin.write((" ".join(args) + "\n").encode())
            out = self._read_to_prompt(_SESSION_CMD_TIMEOUT)
        except (OSError, ValueError):
            out = None
        if out is None or proc.poll() is not None:
            self.kill()
            self.failed_at = time.monotonic()
            return None
        with self._err_lock:
            err = bytes(self._err).decode("utf-8", "replace")
        return out, err


@atexit.register
def _close_sessions() -> None:
    for s in list(Session._live):
        try:
            s.close()
        except Exception:  # noqa: BLE001 - process shutdown
            pass


def _esc(name: str) -> str:
    return name.replace("/", "\\/")


def _split(remote: str) -> tuple[str, str]:
    """('/a/b', 'c') for '/a/b/c' (ignores '/' escaped with a backslash)."""
    remote = remote.rstrip("/")
    m = list(re.finditer(r"(?<!\\)/", remote))
    i = m[-1].start() if m else -1
    return (remote[:i] or "/"), remote[i + 1:].replace("\\/", "/")


def _ts(s: object) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _name(raw: object) -> str | None:
    """Decrypted name, or None if unusable."""
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, dict):
        if raw.get("ok") is False:
            return None
        v = raw.get("value")
        return v if isinstance(v, str) and v else None
    return None


def _section(d: object) -> Entry | None:
    """Section of `fs list /` ({"path": "/trash"}) as a folder."""
    path = d.get("path") if isinstance(d, dict) else d
    if not isinstance(path, str) or not path.startswith("/") or not path.strip("/"):
        return None
    return Entry(path.strip("/"), path.rstrip("/"), True, 0, 0.0, None, False)


def _entry(d: object, parent: str | None = None, path: str | None = None) -> Entry | None:
    """Convert a JSON node into an Entry; None if it must be ignored."""
    if not isinstance(d, dict):
        return None
    name = _name(d.get("name"))
    kind = d.get("type")
    if name is None or kind == "album":
        return None
    is_dir = kind == "folder" or "rootFolderUid" in d  # devices: root folder of another computer
    rev = d.get("activeRevision")
    rev = rev if isinstance(rev, dict) else {}
    digests = rev.get("claimedDigests")
    sha1 = digests.get("sha1") if isinstance(digests, dict) else None
    size = 0
    if not is_dir:
        for v in (rev.get("claimedSize"), d.get("totalStorageSize")):
            if isinstance(v, (int, float)):
                size = int(v)
                break
    mtime = _ts(rev.get("claimedModificationTime") or d.get("modificationTime"))
    if path is None:
        path = (parent or "").rstrip("/") + "/" + _esc(name)
    return Entry(name, path, is_dir, size, mtime,
                 sha1 if isinstance(sha1, str) and sha1 else None,
                 bool(d.get("isSharedByUrl")))


def _find_url(text: str) -> str | None:
    """First public Proton URL in text or JSON (recursive)."""
    def walk(o: object) -> str | None:
        if isinstance(o, str):
            m = _URL_RE.search(o.replace("\\/", "/"))
            return m.group(0) if m else None
        if isinstance(o, dict):
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None

    try:
        r = walk(json.loads(text))
        if r:
            return r
    except ValueError:
        pass
    return walk(text)


_EMAIL_KEYS = ("email", "inviteeEmail", "invitee", "user", "address", "memberEmail")
_ROLE_KEYS = ("role", "inviteeRole", "directRole", "memberRole")


def _person(x: object) -> dict | None:
    """{'email','role'} from a member/invitation of unconfirmed format."""
    if isinstance(x, str):
        return {"email": x, "role": None} if x else None
    if not isinstance(x, dict):
        return None

    def first(keys: tuple[str, ...], d: dict) -> str | None:
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):  # e.g. {"user": {"email": ...}}
                r = first(keys, v)
                if r:
                    return r
        return None

    email = first(_EMAIL_KEYS, x)
    return {"email": email, "role": first(_ROLE_KEYS, x)} if email else None


def _people(*lists: object) -> list[dict]:
    out: list[dict] = []
    for lst in lists:
        for x in lst if isinstance(lst, list) else []:
            p = _person(x)
            if p:
                out.append(p)
    return out


def _parse_sharing(data: object) -> dict:
    """Normalize `sharing status --json` (real format: protonInvitations,
    nonProtonInvitations, members, urlAccess; tolerant of missing keys)."""
    d = data if isinstance(data, dict) else {}
    ua = d.get("urlAccess") or d.get("publicLink")
    ua = ua if isinstance(ua, dict) else {}
    url = ua.get("url") if isinstance(ua.get("url"), str) and ua.get("url") else None
    exp = ua.get("expirationTime") or ua.get("expiration")
    role = ua.get("role")
    return {
        "members": _people(d.get("members")),
        "invitations": _people(d.get("protonInvitations"), d.get("nonProtonInvitations"),
                               d.get("invitations")),
        "url": url,
        "has_password": bool(ua.get("customPassword") or ua.get("hasPassword")),
        "expiration": str(exp) if exp else None,
        "role": role if isinstance(role, str) else None,
        "raw": data,
    }


class Client:
    def __init__(self, binary: str | None = None,
                 runner: Callable[..., tuple[int, str, str]] | None = None,
                 sessions: int | None = None):
        """`sessions`: persistent sessions for reads (default 2; 0 disables them).
        They are never used when a `runner` is injected."""
        self.binary = binary or config.BINARY
        self.runner = runner or _default_runner
        n = (2 if sessions is None else sessions) if runner is None else 0
        self._sessions = [Session(self.binary) for _ in range(max(0, n))]
        self._cv = threading.Condition()

    # -- persistent sessions (reads only) ---------------------------------
    def close(self) -> None:
        """Close the persistent sessions and stop using them (idempotent)."""
        sessions, self._sessions = self._sessions, []
        for s in sessions:
            s.close(final=True)

    @property
    def fast_reads(self) -> bool:
        """True if a session is ready (reads of ~0.1 s instead of ~0.7 s)."""
        return any(s.state == "ready" for s in self._sessions)

    def warm(self, wait: bool = False, timeout: float = 30.0) -> bool:
        """Start the sessions in the background; with `wait`, wait until they are ready
        (True if any is)."""
        self._start_next()
        if not wait:
            return bool(self._sessions)
        end = time.monotonic() + timeout
        with self._cv:
            while time.monotonic() < end:
                if any(s.state == "ready" for s in self._sessions) and \
                        not any(s.state == "starting" for s in self._sessions):
                    break
                if all(s.state == "dead" for s in self._sessions):
                    break
                self._cv.wait(0.2)
        return any(s.state == "ready" for s in self._sessions)

    def recycle_sessions(self) -> None:
        """Discard the sessions (after writing or on forced F5: the CLI may hold cached
        nodes); they are relaunched in the background and meanwhile reads use a subprocess."""
        for s in list(self._sessions):
            with self._cv:
                if s.busy:
                    s.recycle = True
                elif s.state == "ready":
                    s.busy = True
                    threading.Thread(target=self._recycle_one, args=(s,), daemon=True).start()
                elif s.state == "dead":
                    s.failed_at = 0.0   # e.g. after signing in: retry immediately

    def _recycle_one(self, s: Session) -> None:
        try:
            s.close()
        finally:
            with self._cv:
                s.busy = s.recycle = False
                self._cv.notify_all()
            self._start_next()

    def _notify(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def _start_next(self) -> None:
        """Start pending sessions one at a time (two processes starting at once
        compete for the CPU and delay what the user is waiting for)."""
        with self._cv:
            if any(s.state == "starting" for s in self._sessions):
                return
            for s in self._sessions:
                if s.state == "new" or (s.state == "dead" and
                                        time.monotonic() - s.failed_at >= _SESSION_RETRY):
                    s.start_async(self._started)
                    return

    def _started(self) -> None:
        self._notify()
        self._start_next()

    def _acquire(self) -> Session | None:
        """Ready, free session (waits a little if all are busy); None -> subprocess."""
        deadline = time.monotonic() + 5.0
        with self._cv:
            while True:
                waiting, now = False, time.monotonic()
                for s in self._sessions:
                    if s.state == "ready":
                        if s.recycle or now - s.started > _SESSION_MAX_AGE:
                            if s.busy:
                                s.recycle = True
                            else:
                                s.busy = True
                                threading.Thread(target=self._recycle_one, args=(s,),
                                                 daemon=True).start()
                        elif not s.busy:
                            s.busy = True
                            return s
                        else:
                            waiting = True
                    else:
                        self._start_next()   # new/dead: start (respects the retry delay)
                left = deadline - time.monotonic()
                if not waiting or left <= 0:
                    return None
                self._cv.wait(min(left, 0.5))

    def _release(self, s: Session) -> None:
        with self._cv:
            if s.recycle and s.state == "ready":
                threading.Thread(target=self._recycle_one, args=(s,), daemon=True).start()
            else:
                s.busy = s.recycle = False
            self._cv.notify_all()

    def _session_run(self, args: tuple[str, ...]) -> str | None:
        """Read through a session: stdout (str), raises NotFound if the CLI clearly says so,
        or None if the subprocess must be used."""
        if not self._sessions or tuple(args[:2]) not in _READ_CMDS or \
                not all(_SAFE_ARG.fullmatch(a) for a in args):
            return None
        s = self._acquire()
        if s is None:
            return None
        try:
            res = s.request(list(args))
        finally:
            self._release(s)
        if res is None:
            return None
        out, err = res
        err = err.strip()
        if not err and out.strip():
            return out
        if err.startswith("Node not found") and not out.strip():
            raise NotFound(err)
        return None  # error or anything else: let the subprocess report it (exact exception)

    # -- internals -------------------------------------------------------------
    def _run(self, *args: str) -> str:
        if self._sessions:
            if tuple(args[:2]) in _READ_CMDS:
                out = self._session_run(args)
                if out is not None:
                    return out
            elif tuple(args[:2]) != ("fs", "download"):
                try:
                    return self._run_proc(*args)
                finally:
                    self.recycle_sessions()  # the CLI may cache nodes: after writing, start fresh
        return self._run_proc(*args)

    def _run_proc(self, *args: str) -> str:
        rc, out, err = self.runner([self.binary, *args], stdin=DEVNULL)
        if rc == 0:
            return out
        msg = (err or out or f"exit {rc}").strip()
        low = msg.lower()
        if "not found" in low:
            raise NotFound(msg)
        if _LOGGED_OUT_RE.search(low):
            raise NotLoggedIn(msg)
        raise DriveError(msg)

    def _json(self, *args: str) -> object:
        out = self._run(*args, "--json")
        try:
            return json.loads(out)
        except ValueError as e:
            raise DriveError(f"invalid JSON output: {out[:200]!r}") from e

    # -- session ---------------------------------------------------------------
    def is_logged_in(self) -> bool:
        try:
            self._run_proc("fs", "list", config.REMOTE_ROOT, "--json")  # authoritative: without a session
            return True
        except NotLoggedIn:
            return False
        except NotFound:
            return True
        except DriveError as e:
            if str(e).startswith("could not run"):
                raise
            return False

    def logout(self) -> None:
        self._run("auth", "logout")

    def login(self) -> None:
        """Interactive: inherits the terminal; the CLI opens the browser."""
        try:
            rc = subprocess.run([self.binary, "auth", "login"]).returncode
        except OSError as e:
            raise DriveError(f"could not run {self.binary}: {e}") from e
        self.recycle_sessions()  # sessions started without an account are useless
        if rc != 0:
            raise DriveError(f"auth login failed (exit {rc})")

    # -- reading --------------------------------------------------------------
    def list(self, remote: str) -> list[Entry]:
        data = self._json("fs", "list", remote)
        if isinstance(data, dict):  # tolerance: {"items": [...]} or similar
            data = next((v for v in data.values() if isinstance(v, list)), [])
        if not isinstance(data, list):
            raise DriveError("unexpected list response")
        return [e for e in (_entry(d, remote) for d in data) if e]

    def info(self, remote: str) -> Entry:
        data = self._json("fs", "info", remote)
        if isinstance(data, list) and data:
            data = data[0]
        e = _entry(data, path=remote.rstrip("/") or "/")
        if e is None:
            raise DriveError(f"info for {remote}: unexpected response")
        return e

    def exists(self, remote: str) -> bool:
        try:
            self.info(remote)
            return True
        except NotFound:
            return False

    def sections(self) -> list[Entry]:
        """Top-level sections (`fs list /`): /my-files, /trash, ..."""
        data = self._json("fs", "list", "/")
        if not isinstance(data, list):
            raise DriveError("unexpected list / response")
        return [e for e in map(_section, data) if e]

    def size(self, remote: str) -> int:
        """Bytes (folders included, with descendants)."""
        data = self._json("fs", "size", remote)
        if isinstance(data, dict):
            data = data.get("size")
        if isinstance(data, (int, float)):
            return int(data)
        raise DriveError(f"size for {remote}: unexpected response")

    def list_trash(self) -> list[Entry]:
        """Contents of /trash (empty if the CLI has no such section)."""
        try:
            return self.list(TRASH)
        except NotFound:
            return []

    def sharing_status(self, remote: str) -> dict:
        out = self._run("sharing", "status", remote, "--json")
        if out.strip() in ("", "undefined"):  # the real CLI prints "undefined" when nothing is shared
            return _parse_sharing({})
        try:
            return _parse_sharing(json.loads(out))
        except ValueError as e:
            raise DriveError(f"invalid JSON output: {out[:200]!r}") from e

    # -- writing ------------------------------------------------------------
    def mkdir(self, parent: str, name: str) -> None:
        self._run("fs", "create-folder", parent, name)

    def mkdir_p(self, remote: str) -> None:
        remote = remote.rstrip("/")
        if not remote or remote == config.REMOTE_ROOT or self.exists(remote):
            return
        parent, name = _split(remote)
        if parent != "/":
            self.mkdir_p(parent)
        self.mkdir(parent, name)

    def upload(self, locals: list[Path], remote_dir: str, on_conflict: str = "rename") -> None:
        if on_conflict not in _UPLOAD_FILE:
            raise ValueError(f"invalid on_conflict: {on_conflict}")
        paths = [str(Path(p).absolute()) for p in locals]
        if not paths:
            return
        self.mkdir_p(remote_dir)
        d = "skip" if on_conflict == "skip" else "merge"
        for i in range(0, len(paths), _CHUNK):
            self._run("fs", "upload", "-f", _UPLOAD_FILE[on_conflict], "-d", d,
                      *paths[i:i + _CHUNK], remote_dir)

    def download(self, remote: str, local_dir: Path, on_conflict: str = "rename") -> None:
        if on_conflict not in _DOWNLOAD_FILE:
            raise ValueError(f"invalid on_conflict: {on_conflict}")
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        d = "skip" if on_conflict == "skip" else "merge"
        self._run("fs", "download", "-f", _DOWNLOAD_FILE[on_conflict], "-d", d,
                  remote, str(local_dir.absolute()))

    def trash(self, remote: str) -> None:
        self._run("fs", "trash", remote)

    def rename(self, remote: str, new_name: str) -> None:
        self._run("fs", "rename", remote, new_name)

    def move(self, remotes: list[str], target_parent: str) -> None:
        if remotes:
            self._run("fs", "move", *remotes, target_parent)

    def copy(self, remotes: list[str], target_parent: str) -> None:
        if remotes:
            self._run("fs", "copy", *remotes, target_parent)

    def restore(self, remote: str) -> None:
        self._run("fs", "restore", remote)

    def empty_trash(self) -> None:
        self._run("fs", "empty-trash")

    def delete(self, remote: str) -> None:
        """Permanent deletion (the CLI only deletes items that are already in the trash)."""
        self._run("fs", "delete", remote)

    # -- sharing ------------------------------------------------------------
    def invite(self, remote: str, emails: list[str], role: str = "viewer") -> None:
        if not emails:
            raise ValueError("emails is empty")
        args: list[str] = []
        for e in emails:
            args += ["-u", e]
        self._run("sharing", "invite", *args, "-r", role, remote)

    def link(self, remote: str, password: str | None = None,
             expiration: str | None = None, role: str = "viewer") -> str:
        args = ["sharing", "set-url", "--role", role]
        if password:
            if password.startswith("-"):
                raise DriveError("The link password cannot start with '-'")
            args += ["--password", password]
        if expiration:
            args += ["--expiration", expiration]
        out = self._run(*args, remote)
        url = _find_url(out)
        if not url:  # unconfirmed format: ask for the link status
            url = _find_url(self._run("sharing", "status", remote, "--json"))
        if not url:
            raise DriveError("could not obtain the public URL")
        return url

    def remove_access(self, remote: str, emails: list[str] | None = None) -> None:
        """Remove access/invitations for `emails`, or for everyone if None."""
        if emails is None:
            self._run("sharing", "remove", "-a", remote)
        elif emails:
            args: list[str] = []
            for e in emails:
                args += ["-e", e]
            self._run("sharing", "remove", *args, remote)

    def unlink(self, remote: str) -> None:
        try:
            self._run("sharing", "remove-url", remote)
        except DriveError as e:
            if "not implemented" in str(e).lower():
                raise DriveError("This Proton CLI (0.9.0) cannot remove public links yet. "
                                 "Remove it from the web app: Proton Drive → Share → Stop sharing.") from e
            raise
