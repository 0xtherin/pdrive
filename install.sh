#!/bin/sh
# Installs pdrive for the current user (everything under $HOME). Idempotent: it can be
# re-run on top of a previous installation.
#   ./install.sh               install (and remove the old pdrive-* systemd units)
#   ./install.sh --uninstall   revert (does not touch ~/.config/pdrive or user data)
# Variables: PDRIVE_SKIP_SYSTEM=1 skips systemctl, kbuildsycoca6, update-desktop-database
#            and gtk-update-icon-cache (for testing with a temporary HOME).
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"
MENU_DIR="$HOME/.local/share/kio/servicemenus"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
UI_ICON_DIR="$HOME/.local/share/pdrive/icons"   # GUI's own icons (backup copy)
UNIT_DIR="$HOME/.config/systemd/user"
MARK="# managed-by: pdrive install.sh"
# units from earlier versions (sync and watcher), now removed
OLD_UNITS="pdrive-sync.timer pdrive-sync.service pdrive-watch.service"

for a in "$@"; do
  case $a in
    --uninstall) mode=uninstall ;;
    --no-enable) ;;  # obsolete: there is nothing to enable anymore
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a" >&2; exit 1 ;;
  esac
done
mode=${mode:-install}

say() { printf '%s\n' "$*"; }
have() { [ "${PDRIVE_SKIP_SYSTEM:-}" != 1 ] && command -v "$1" >/dev/null 2>&1; }
sd() { have systemctl && systemctl --user "$@" >/dev/null 2>&1 || true; }
refresh() {
  have kbuildsycoca6 && { kbuildsycoca6 >/dev/null 2>&1 || true; }
  have update-desktop-database && { update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true; }
  if have gtk-update-icon-cache && [ -f "$HOME/.local/share/icons/hicolor/index.theme" ]; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
  fi
  return 0
}
remove_old_units() {
  found=0
  for u in $OLD_UNITS; do
    if [ -e "$UNIT_DIR/$u" ]; then
      found=1
      sd disable --now "$u"
      rm -f "$UNIT_DIR/$u"
    fi
  done
  if [ "$found" = 1 ]; then sd daemon-reload; say "Removed the old pdrive systemd units."; fi
  return 0
}

if [ "$mode" = uninstall ]; then
  remove_old_units
  rm -f "$MENU_DIR/pdrive.desktop" "$APP_DIR/pdrive.desktop" "$ICON_DIR/pdrive.svg"
  rm -rf "$UI_ICON_DIR"; rmdir "$HOME/.local/share/pdrive" 2>/dev/null || true
  if [ -f "$BIN_DIR/pdrive" ] && grep -qF "$MARK" "$BIN_DIR/pdrive"; then rm -f "$BIN_DIR/pdrive"; fi
  # the binary is only removed if it is the identical copy this script installed
  if [ -f "$BIN_DIR/proton-drive" ] && [ -f "$ROOT/proton-drive" ] && cmp -s "$ROOT/proton-drive" "$BIN_DIR/proton-drive"; then
    rm -f "$BIN_DIR/proton-drive"
  fi
  refresh
  say "pdrive uninstalled."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is missing." >&2; exit 1; }
case $ROOT in *"'"*) echo "The project path cannot contain single quotes." >&2; exit 1 ;; esac
mkdir -p "$BIN_DIR" "$MENU_DIR"

# 1) official binary (if not already there)
if [ ! -e "$BIN_DIR/proton-drive" ]; then
  if [ -f "$ROOT/proton-drive" ]; then
    install -m 755 "$ROOT/proton-drive" "$BIN_DIR/proton-drive"
  else
    say "Warning: $ROOT/proton-drive not found; install the official CLI at $BIN_DIR/proton-drive." >&2
  fi
fi

# 2) wrapper pointing to the project code
tmp=$(mktemp)
cat > "$tmp" <<WRAP
#!/bin/sh
$MARK
PYTHONPATH='$ROOT'\${PYTHONPATH:+:\$PYTHONPATH} exec python3 -m pdrive "\$@"
WRAP
install -m 755 "$tmp" "$BIN_DIR/pdrive"; rm -f "$tmp"

# 3) Dolphin context menu (KF6); Exec with the absolute path of the wrapper
sed "s|@PDRIVE@|$BIN_DIR/pdrive|g" "$ROOT/dolphin/pdrive.desktop" > "$MENU_DIR/pdrive.desktop"
chmod 755 "$MENU_DIR/pdrive.desktop"

# 4) application launcher and icon (tolerant: they may be missing from the project)
if [ -f "$ROOT/desktop/pdrive.desktop" ]; then
  mkdir -p "$APP_DIR"
  sed "s|@PDRIVE@|$BIN_DIR/pdrive|g" "$ROOT/desktop/pdrive.desktop" > "$APP_DIR/pdrive.desktop"
  chmod 644 "$APP_DIR/pdrive.desktop"
else
  say "Warning: $ROOT/desktop/pdrive.desktop is missing; the launcher is not installed." >&2
fi
if [ -f "$ROOT/assets/pdrive.svg" ]; then
  mkdir -p "$ICON_DIR"
  install -m 644 "$ROOT/assets/pdrive.svg" "$ICON_DIR/pdrive.svg"
else
  say "Warning: $ROOT/assets/pdrive.svg is missing; the icon is not installed." >&2
fi

# 4b) GUI's own icons: the app reads them from the project; this copy only helps if the project is moved/deleted
if [ -d "$ROOT/assets/icons" ]; then
  mkdir -p "$UI_ICON_DIR"
  cp -f "$ROOT"/assets/icons/*.svg "$UI_ICON_DIR"/
fi

# 5) cleanup of earlier versions (systemd sync/watcher)
remove_old_units

refresh
say "pdrive installed at $BIN_DIR/pdrive"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "Warning: $BIN_DIR is not in your PATH." ;; esac
say "Next step: pdrive login   (then: \"Proton Drive\" in the application menu, or pdrive gui)"
