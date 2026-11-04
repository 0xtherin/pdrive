"""Desktop notifications and dialogs (notify-send, kdialog, zenity, wl-copy).

With PDRIVE_NO_GUI=1 no windows are opened: notifications go to stderr and
dialogs read their answer from PDRIVE_PICK / PDRIVE_EMAIL / PDRIVE_TEXT (tests).
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys

APP = "Proton Drive"
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def _no_gui() -> bool:
    return os.environ.get("PDRIVE_NO_GUI", "") not in ("", "0")


def _run(argv: list[str], timeout: float | None = None) -> tuple[int, str]:
    """Run argv without inheriting stdin; returns (rc, stdout). rc=127 if it does not exist."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return p.returncode, p.stdout


def notify(title: str, body: str = "", icon: str = "folder-cloud",
           error: bool = False, replace_id: int | None = None) -> int | None:
    """Show a desktop notification. Returns its id (to replace it later)."""
    if _no_gui() or sys.stderr.isatty():
        print(f"[{'error' if error else 'pdrive'}] {title}" + (f": {body}" if body else ""),
              file=sys.stderr, flush=True)
        if _no_gui():
            return None
    if not shutil.which("notify-send"):
        return None
    argv = ["notify-send", "-a", "pdrive", "-i", "dialog-error" if error else icon, "-p"]
    argv += ["-u", "critical"] if error else ["-t", "6000"]
    if replace_id:
        argv += ["-r", str(replace_id)]
    argv += [html.escape(title, quote=False), html.escape(body, quote=False)]
    rc, out = _run(argv, timeout=10)
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else None


def _menu(title: str, text: str, items: list[tuple[str, str]]) -> str | None:
    """Single-choice menu; returns the chosen tag, or None if cancelled."""
    if shutil.which("kdialog"):
        argv = ["kdialog", "--title", title, "--menu", text]
        for tag, label in items:
            argv += [tag, label]
    elif shutil.which("zenity"):
        argv = ["zenity", "--list", "--title", title, "--text", text, "--hide-header",
                "--column", "tag", "--column", "Folder", "--hide-column", "1",
                "--print-column", "1", "--width", "480", "--height", "420"]
        for tag, label in items:
            argv += [tag, label]
    else:
        return None
    rc, out = _run(argv)
    return out.strip() if rc == 0 and out.strip() else None


def ask_text(title: str, label: str, default: str = "") -> str | None:
    """Ask for a line of text; None if cancelled or there is no dialog."""
    if _no_gui():
        return os.environ.get("PDRIVE_TEXT") or None
    if shutil.which("kdialog"):
        argv = ["kdialog", "--title", title, "--inputbox", label, default]
    elif shutil.which("zenity"):
        argv = ["zenity", "--entry", "--title", title, "--text", label, "--entry-text", default]
    else:
        return None
    rc, out = _run(argv)
    text = out.strip()
    return text if rc == 0 and text else None


def ask_email() -> list[str] | None:
    """Ask for one or more emails (comma/space separated). Asks again if any are invalid."""
    if _no_gui():
        raw = os.environ.get("PDRIVE_EMAIL", "")
        mails = [m for m in re.split(r"[\s,;]+", raw) if m]
        return mails or None
    label = "Email(s) of the people to share with (comma-separated):"
    while True:
        raw = ask_text("Share with…", label)
        if raw is None:
            return None
        mails = [m for m in re.split(r"[\s,;]+", raw) if m]
        bad = [m for m in mails if not _EMAIL_RE.match(m)]
        if mails and not bad:
            return mails
        label = f"Invalid email: {', '.join(bad) or raw}. Please try again:"


def pick_remote_folder(client, start: str | None = None) -> str | None:
    """Navigable menu of remote folders; returns the chosen path, or None."""
    from . import config  # late import: avoids cycles and allows tests without config

    if _no_gui():
        return os.environ.get("PDRIVE_PICK") or None
    root = config.REMOTE_ROOT
    cur = (start or config.load().get("upload_dir") or root).rstrip("/") or root
    while True:
        try:
            dirs = sorted((e for e in client.list(cur) if e.is_dir), key=lambda e: e.name.lower())
        except Exception as exc:  # missing folder: walk up to one that exists
            from .client import NotLoggedIn
            if isinstance(exc, NotLoggedIn) or cur == root:
                raise
            cur = cur.rsplit("/", 1)[0] or root
            continue
        items = [("@here", f"[ Choose this folder ]  {cur}")]
        items.append(("@new", "[ New folder here… ]"))
        if cur != root:
            items.append(("@up", "[ .. Up one level ]"))
        items += [(f"d{i}", e.name + "/") for i, e in enumerate(dirs)]
        tag = _menu("Upload to Proton Drive", f"Destination folder: {cur}", items)
        if tag is None:
            return None
        if tag == "@here":
            return cur
        if tag == "@up":
            cur = cur.rsplit("/", 1)[0] or root
        elif tag == "@new":
            name = ask_text("New folder", f"Name of the new folder in {cur}:")
            if name and "/" not in name:
                client.mkdir(cur, name)
                cur = f"{cur}/{name}"
        elif tag.startswith("d") and tag[1:].isdigit() and int(tag[1:]) < len(dirs):
            cur = f"{cur}/{dirs[int(tag[1:])].name}"


def copy_to_clipboard(text: str) -> bool:
    """Copy to the clipboard (wl-copy, or xclip/xsel). True on success."""
    for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        if not shutil.which(argv[0]):
            continue
        try:
            # wl-copy stays in the background serving the content: do not wait for its pipes.
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            assert p.stdin is not None
            p.stdin.write(text.encode())
            p.stdin.close()
            try:
                if p.wait(timeout=3) != 0:
                    continue
            except subprocess.TimeoutExpired:  # xclip/xsel stay alive: this is normal
                pass
            return True
        except OSError:
            continue
    return False
