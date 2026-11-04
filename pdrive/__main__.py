"""User CLI: python -m pdrive <subcommand> (wrapper ~/.local/bin/pdrive).

Exit codes: 0 ok, 1 error, 2 not signed in, 130 interrupted.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config, notify as nt
from .client import Client, DriveError, NotFound, NotLoggedIn

WEB_URL = "https://drive.proton.me"
LOGIN_HINT = "Not signed in: run `pdrive login` in a terminal."


class UserError(Exception):
    """Usage error (missing path, cancelled...) with a user-facing message."""


class Cancelled(Exception):
    """The user closed a dialog; exit silently."""


# ------------------------------------------------------------------ utilities

def _local(arg: str) -> Path:
    """Absolute local path (accepts file:// in case the file manager passes URLs)."""
    if arg.startswith("file://"):
        arg = unquote(urlparse(arg).path)
    return Path(os.path.abspath(os.path.expanduser(arg)))


def _is_local(arg: str) -> bool:
    return os.path.lexists(_local(arg))


def _names(paths: list[Path]) -> str:
    first = paths[0].name or str(paths[0])
    return first if len(paths) == 1 else f"{first} and {len(paths) - 1} more"


def _join(remote: str, name: str) -> str:
    return remote.rstrip("/") + "/" + name


def _cfg() -> dict:
    return config.load()


def _existing(args: list[str]) -> list[Path]:
    paths = [_local(a) for a in args]
    missing = [p for p in paths if not os.path.lexists(p)]
    if missing:
        raise UserError(f"Does not exist: {', '.join(str(p) for p in missing)}")
    return paths


def _upload(client: Client, paths: list[Path], remote_dir: str, on_conflict: str = "rename") -> None:
    """Upload with a start notification (replaced by the end one)."""
    what = _names(paths)
    nid = nt.notify("Uploading to Proton Drive", f"{what} → {remote_dir}")
    client.upload(paths, remote_dir, on_conflict=on_conflict)
    nt.notify("Upload complete", f"{what} → {remote_dir}", replace_id=nid)


def _resolve(client: Client, args: list[str], upload_dir: str) -> list[str]:
    """Convert local/remote paths into remote paths.

    Local ones are uploaded (new revision if changed) to upload_dir and that copy is used.
    """
    locals_: list[Path] = []
    remotes: list[str] = []
    for a in args:
        if _is_local(a):
            locals_.append(_local(a))
        elif a.startswith("/"):
            remotes.append(a)
        else:
            raise UserError(f"Neither a local nor a remote path exists: {a}")
    for r in remotes:
        if not client.exists(r):
            raise UserError(f"Does not exist in Proton Drive: {r}")
    out: list[str] = []
    if locals_:
        _upload(client, locals_, upload_dir, on_conflict="revision")
        out += [_join(upload_dir, p.name) for p in locals_]
    return out + remotes


# ------------------------------------------------------------- subcommands

def cmd_login(a: argparse.Namespace, c: Client) -> int:
    c.login()
    if c.is_logged_in():
        nt.notify("Signed in", "Proton Drive is ready.")
        return 0
    raise DriveError("Could not verify the session after signing in.")


def cmd_status(a: argparse.Namespace, c: Client) -> int:
    cfg = _cfg()
    print(f"binary:      {c.binary if hasattr(c, 'binary') else config.BINARY}")
    print(f"uploads to:  {cfg['upload_dir']}")
    if c.is_logged_in():
        print("session:     signed in")
        return 0
    print("session:     NOT signed in (run `pdrive login`)")
    return 2


def cmd_ls(a: argparse.Namespace, c: Client) -> int:
    for e in sorted(c.list(a.remote), key=lambda e: (not e.is_dir, e.name.lower())):
        print(f"{'d' if e.is_dir else '-'} {e.size:>12}  {e.name}{'/' if e.is_dir else ''}")
    return 0


def cmd_upload(a: argparse.Namespace, c: Client) -> int:
    _upload(c, _existing(a.files), a.to or _cfg()["upload_dir"], a.conflict)
    return 0


def cmd_uploadto(a: argparse.Namespace, c: Client) -> int:
    paths = _existing(a.files)
    dest = nt.pick_remote_folder(c)
    if not dest:
        raise Cancelled
    _upload(c, paths, dest, a.conflict)
    return 0


def cmd_download(a: argparse.Namespace, c: Client) -> int:
    dest = _local(a.to) if a.to else Path.home() / "Downloads"
    dest.mkdir(parents=True, exist_ok=True)
    nid = nt.notify("Downloading from Proton Drive", f"{a.remote} → {dest}")
    c.download(a.remote, dest, on_conflict=a.conflict)
    nt.notify("Download complete", f"{a.remote} → {dest}", replace_id=nid)
    return 0


def cmd_share(a: argparse.Namespace, c: Client) -> int:
    for x in a.paths:  # fail early, before asking anything
        if not _is_local(x) and not x.startswith("/"):
            raise UserError(f"Neither a local nor a remote path exists: {x}")
    emails = a.email or nt.ask_email()
    if not emails:
        raise Cancelled
    remotes = _resolve(c, a.paths, _cfg()["upload_dir"])
    for r in remotes:
        c.invite(r, emails, role=a.role)
    nt.notify("Shared", f"{_names([Path(r) for r in remotes])} with {', '.join(emails)} "
                            f"({'can edit' if a.role == 'editor' else 'view only'})")
    return 0


def cmd_link(a: argparse.Namespace, c: Client) -> int:
    remotes = _resolve(c, a.paths, _cfg()["upload_dir"])
    urls = [c.link(r, password=a.password, expiration=a.expiration, role=a.role) for r in remotes]
    text = "\n".join(urls)
    print(text)
    copied = nt.copy_to_clipboard(text)
    head = "Public link copied to clipboard" if copied else "Public link (could not copy)"
    nt.notify(head, text)
    return 0


def cmd_unlink(a: argparse.Namespace, c: Client) -> int:
    for r in a.paths:
        c.unlink(r)
    nt.notify("Public link removed", ", ".join(a.paths))
    return 0


def cmd_gui(a: argparse.Namespace, c: Client) -> int:
    try:
        from .gui.app import run
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] == "PySide6":
            msg = "PySide6 is missing (Fedora: sudo dnf install python3-pyside6)."
        elif e.name and e.name.startswith("pdrive.gui"):
            msg = "The graphical interface is not available in this installation yet."
        else:
            raise
        print(msg, file=sys.stderr)
        nt.notify("Proton Drive", msg, error=True)
        return 1
    return run(c, _cfg())


def cmd_open(a: argparse.Namespace, c: Client | None) -> int:
    print(WEB_URL)
    if not nt._no_gui():
        subprocess.Popen(["xdg-open", WEB_URL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdrive", description="Proton Drive for KDE.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name: str, fn, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(fn=fn)
        return sp

    add("login", cmd_login, "sign in (opens the browser)")
    add("status", cmd_status, "show configuration and session status")
    add("ls", cmd_ls, "list a remote folder").add_argument("remote", nargs="?", default=config.REMOTE_ROOT)

    for name, fn, help in (("upload", cmd_upload, "upload files/folders (to upload_dir by default)"),
                           ("uploadto", cmd_uploadto, "upload, choosing the remote folder in a dialog")):
        sp = add(name, fn, help)
        sp.add_argument("files", nargs="+")
        sp.add_argument("--conflict", choices=["rename", "replace", "skip", "revision"], default="rename",
                        help="if it already exists (default: rename)")
        if name == "upload":
            sp.add_argument("--to", metavar="REMOTE", help="destination remote folder")

    sp = add("download", cmd_download, "download a remote path")
    sp.add_argument("remote")
    sp.add_argument("--to", metavar="DIR", help="local folder (default: ~/Downloads)")
    sp.add_argument("--conflict", choices=["rename", "replace", "skip"], default="rename")

    sp = add("share", cmd_share, "share with people by email (uploads first if local)")
    sp.add_argument("paths", nargs="+", help="local or remote path")
    sp.add_argument("--email", "-e", action="append", help="email (repeatable); asked for if missing")
    sp.add_argument("--role", choices=["viewer", "editor"], default="viewer")

    sp = add("link", cmd_link, "create a public link and copy it to the clipboard")
    sp.add_argument("paths", nargs="+", help="local or remote path")
    sp.add_argument("--password")
    sp.add_argument("--expiration", metavar="YYYY-MM-DD")
    sp.add_argument("--role", choices=["viewer", "editor"], default="viewer")

    add("unlink", cmd_unlink, "remove the public link from remote paths").add_argument("paths", nargs="+")

    add("gui", cmd_gui, "open the Proton Drive graphical explorer")
    add("open", cmd_open, "open Proton Drive in the browser")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = None if args.fn is cmd_open else Client(sessions=None if args.fn is cmd_gui else 0)
        return args.fn(args, client)
    except Cancelled:
        return 1
    except NotLoggedIn:
        nt.notify("Proton Drive: not signed in", LOGIN_HINT, error=True)
        return 2
    except UserError as e:
        nt.notify("Proton Drive", str(e), error=True)
        return 1
    except NotFound as e:
        nt.notify("Proton Drive: not found", str(e)[:300], error=True)
        return 1
    except DriveError as e:
        nt.notify("Proton Drive: error", str(e).strip()[:300] or "unknown error", error=True)
        return 1
    except (FileNotFoundError, PermissionError) as e:
        nt.notify("Proton Drive: error", f"{e.filename or e}: {e.strerror or e}", error=True)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
