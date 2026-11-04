"""pdrive configuration: paths, binary and config.toml."""
from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

BINARY: str = (
    os.environ.get("PDRIVE_BIN")
    or shutil.which("proton-drive")
    or str(Path.home() / ".local" / "bin" / "proton-drive")
)
REMOTE_ROOT = "/my-files"
CONFIG_DIR: Path = Path(os.environ.get("PDRIVE_CONFIG_DIR") or Path.home() / ".config" / "pdrive")

DEFAULTS: dict = {
    "upload_dir": "/my-files/Uploads",
    "disk_cache": False,   # store seen folder/file names on disk (unencrypted)
}


def load() -> dict:
    """Read CONFIG_DIR/config.toml on top of the defaults."""
    cfg = dict(DEFAULTS)
    path = CONFIG_DIR / "config.toml"
    if path.is_file():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    for k in ("sync_pairs", "inbox_remote"):  # obsolete (sync and watch were removed)
        cfg.pop(k, None)
    return cfg
