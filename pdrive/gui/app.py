"""GUI entry point: ``run(client, cfg) -> int``."""
from __future__ import annotations

import sys

from PySide6.QtCore import QLocale, QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..client import Client
from . import theme
from .cache import ListingCache, default_path
from .window import MainWindow


def run(client: Client, cfg: dict) -> int:
    """Create the QApplication, apply the custom theme (light/dark) and enter the event loop."""
    QLocale.setDefault(QLocale(QLocale.Language.English))   # English dates, numbers and standard buttons whatever the system locale
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Proton Drive")
    app.setApplicationDisplayName("Proton Drive")
    app.setDesktopFileName("pdrive")
    app.setOrganizationName("pdrive")
    paths = QIcon.themeSearchPaths()  # only for the application icon ("pdrive") installed in hicolor
    for d in QStandardPaths.standardLocations(QStandardPaths.StandardLocation.GenericDataLocation):
        if d + "/icons" not in paths:
            paths.append(d + "/icons")
    QIcon.setThemeSearchPaths(paths)
    theme.install(app)   # Fusion + palette + QSS; follows the system color scheme live
    cache = None
    if cfg.get("disk_cache"):
        cache = ListingCache(default_path())
    else:  # no disk cache: leave no leftovers from earlier runs
        try:
            default_path().unlink(missing_ok=True)
        except OSError:
            pass
    win = MainWindow(client, cfg, cache=cache)
    win.show()
    return app.exec()
