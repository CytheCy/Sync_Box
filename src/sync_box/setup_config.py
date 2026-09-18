"""Conservative first-run configuration creation for the desktop UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from sync_box.config import default_config_path


class SetupError(ValueError):
    pass


def box_folder_id_from_url(value: str) -> str:
    text = value.strip()
    if not text:
        raise SetupError("Enter the URL of the Box folder to synchronize.")
    parsed = urlparse(text)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "box.com" or hostname.endswith(".box.com")
    ):
        raise SetupError("Enter a valid box.com folder URL.")
    match = re.search(r"/(?:folder|folders)/(\d+)(?:/|$)", parsed.path)
    if match is None:
        raise SetupError("The Box folder URL does not contain a folder number.")
    return match.group(1)


def default_state_directory() -> Path:
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ).expanduser()
    return state_home / "sync-box"


def create_initial_config(
    local_root: Path,
    box_folder_url: str,
    *,
    config_path: Path | None = None,
    state_directory: Path | None = None,
) -> Path:
    """Create only non-secret configuration; never initialize state or a baseline."""
    target = (config_path or default_config_path()).expanduser()
    if target.exists():
        raise SetupError(f"Configuration already exists: {target}")
    expanded_root = local_root.expanduser()
    if not expanded_root.is_absolute():
        raise SetupError("Choose an absolute path to the local folder.")
    if expanded_root.is_symlink():
        raise SetupError("Choose the real local folder rather than a symbolic link.")
    root = expanded_root.resolve(strict=False)
    if not root.is_dir():
        raise SetupError("The local folder must already exist.")
    folder_id = box_folder_id_from_url(box_folder_url)
    state = (state_directory or default_state_directory()).expanduser().resolve(strict=False)
    if state.is_relative_to(root):
        raise SetupError("Application state must be outside the synchronized folder.")

    contents = (
        "# Created by Sync_Box. This file contains no credentials.\n\n"
        "[local]\n"
        f"root = {json.dumps(str(root))}\n\n"
        "[box]\n"
        f"folder_id = {json.dumps(folder_id)}\n\n"
        "[sync]\n"
        "exclude = []\n"
        "exclude_names = []\n\n"
        "[storage]\n"
        f"state_database = {json.dumps(str(state / 'state.sqlite3'))}\n"
        f"log_file = {json.dumps(str(state / 'sync-box.log'))}\n"
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config.toml.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise SetupError(f"Configuration already exists: {target}")
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
