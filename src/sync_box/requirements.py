"""Read-only requirement and setup-state checks for the desktop application."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import platform
import sys

from sync_box.app_status import DatabaseState, SystemdController, SystemdState, read_database_state
from sync_box.config import AppConfig, ConfigError, default_config_path, load_config
from sync_box.dependencies import BoxCliManager, BoxCliStatus
from sync_box.resources import PACKAGED_SERVICE, PACKAGED_TIMER, icon_path


class FolderKind(str, Enum):
    MISSING = "missing"
    EMPTY = "empty"
    NONEMPTY = "nonempty"
    INACCESSIBLE = "inaccessible"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class RequirementStatus:
    supported_platform: bool
    package_resources: bool
    python_runtime: bool
    qt_runtime: bool
    box_cli: BoxCliStatus
    config: AppConfig | None
    config_error: str | None
    folder_kind: FolderKind | None
    folder_detail: str | None
    database: DatabaseState
    systemd: SystemdState


def inspect_folder(path: Path) -> tuple[FolderKind, str]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        return FolderKind.UNSAFE, "Choose the real folder rather than a symbolic link."
    if not expanded.exists():
        parent = next((p for p in expanded.parents if p.exists()), None)
        if parent is None or not os.access(parent, os.W_OK | os.X_OK):
            return FolderKind.INACCESSIBLE, "The folder cannot be created at this location."
        return FolderKind.MISSING, "The folder does not exist yet."
    if not expanded.is_dir():
        return FolderKind.UNSAFE, "The selected path is not a directory."
    try:
        entries = list(os.scandir(expanded))
    except OSError:
        return FolderKind.INACCESSIBLE, "The folder cannot be read."
    if not os.access(expanded, os.R_OK | os.W_OK | os.X_OK):
        return FolderKind.INACCESSIBLE, "The folder is not readable and writable."
    try:
        owner = expanded.stat(follow_symlinks=False).st_uid
    except OSError:
        return FolderKind.INACCESSIBLE, "The folder cannot be inspected."
    if owner != os.geteuid():
        return FolderKind.UNSAFE, "The folder must be owned by the logged-in user."
    try:
        for directory, names, files in os.walk(expanded, followlinks=False):
            for name in (*names, *files):
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    return FolderKind.UNSAFE, "The folder contains a symbolic link."
                if candidate.stat(follow_symlinks=False).st_uid != os.geteuid():
                    return FolderKind.UNSAFE, "The folder contains files not owned by the logged-in user."
    except OSError:
        return FolderKind.INACCESSIBLE, "The folder contains an inaccessible path."
    return (FolderKind.NONEMPTY if entries else FolderKind.EMPTY), (
        "The folder contains files." if entries else "The folder is empty."
    )


class RequirementChecker:
    def __init__(
        self,
        config_path: Path | None = None,
        *,
        dependencies: BoxCliManager | None = None,
        systemd: SystemdController | None = None,
    ) -> None:
        self.config_path = config_path or default_config_path()
        self.dependencies = dependencies or BoxCliManager()
        self.systemd = systemd or SystemdController()

    def check(self) -> RequirementStatus:
        config = None
        config_error = None
        folder_kind = None
        folder_detail = None
        database = DatabaseState()
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            config_error = str(exc)
        if config is not None:
            folder_kind, folder_detail = inspect_folder(config.local_root)
            database = read_database_state(config.state_database)
        return RequirementStatus(
            supported_platform=_supported_fedora_linux(),
            package_resources=(
                icon_path().is_file()
                and (_development_tree() or (PACKAGED_SERVICE.is_file() and PACKAGED_TIMER.is_file()))
            ),
            python_runtime=sys.version_info >= (3, 11),
            qt_runtime=_qt_available(),
            box_cli=self.dependencies.status(),
            config=config,
            config_error=config_error,
            folder_kind=folder_kind,
            folder_detail=folder_detail,
            database=database,
            systemd=self.systemd.snapshot(),
        )


def _supported_fedora_linux() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        values = dict(
            line.rstrip().split("=", 1)
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError:
        return False
    identifiers = f"{values.get('ID', '')} {values.get('ID_LIKE', '')}".replace('"', "").split()
    return "fedora" in identifiers


def _qt_available() -> bool:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def _development_tree() -> bool:
    return "site-packages" not in str(Path(__file__).resolve())
