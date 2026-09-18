"""Installed paths and package resources used by desktop integrations."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import shutil
import sys


INSTALLED_CLI = Path("/usr/bin/sync-box")
INSTALLED_GUI = Path("/usr/bin/sync-box-gui")
PACKAGED_SERVICE = Path("/usr/lib/systemd/user/sync-box.service")
PACKAGED_TIMER = Path("/usr/lib/systemd/user/sync-box.timer")


def icon_path() -> Path:
    """Return the filesystem path for the package's application icon."""
    return Path(str(files("sync_box").joinpath("resources/icons/sync-box.svg")))


def cli_command() -> list[str]:
    """Choose the installed CLI, with a source-tree fallback for development."""
    installed = shutil.which("sync-box")
    if installed:
        return [str(Path(installed).resolve(strict=False))]
    return [sys.executable, "-m", "sync_box"]
