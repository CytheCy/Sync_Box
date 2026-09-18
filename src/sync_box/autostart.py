"""Freedesktop autostart preference independent from the sync timer."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


AUTOSTART_NAME = "sync-box.desktop"


def user_autostart_path() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser()
    return config_home / "autostart" / AUTOSTART_NAME


def autostart_enabled(path: Path | None = None) -> bool:
    override = path or user_autostart_path()
    if not override.exists():
        return True  # The RPM installs a system-wide XDG autostart entry.
    try:
        return "Hidden=true" not in override.read_text(encoding="utf-8")
    except OSError:
        return True


def set_autostart(enabled: bool, path: Path | None = None) -> Path:
    target = path or user_autostart_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    hidden = "false" if enabled else "true"
    contents = f"""[Desktop Entry]
Type=Application
Name=Sync_Box
Exec=/usr/bin/sync-box-gui
Icon=sync-box
Terminal=false
X-GNOME-Autostart-enabled={str(enabled).lower()}
Hidden={hidden}
"""
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
