"""Load and validate non-secret sync configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import Any

from sync_box.inventory import ScanError, normalize_relative_path


class ConfigError(ValueError):
    """Raised when configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    local_root: Path
    box_folder_id: str
    state_database: Path
    log_file: Path
    excluded_paths: tuple[str, ...]
    excluded_names: tuple[str, ...]


def default_config_path() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser()
    return config_home / "sync-box" / "config.toml"


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().resolve(strict=False)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {config_path}")

    try:
        with config_path.open("rb") as file_handle:
            data = tomllib.load(file_handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from exc

    config = AppConfig(
        local_root=_absolute_path(data, "local", "root"),
        box_folder_id=_string(data, "box", "folder_id"),
        state_database=_absolute_path(data, "storage", "state_database"),
        log_file=_absolute_path(data, "storage", "log_file"),
        excluded_paths=_excluded_paths(data),
        excluded_names=_excluded_names(data),
    )
    _validate(config, config_path)
    return config


def _string(data: dict[str, Any], section: str, key: str) -> str:
    try:
        value = data[section][key]
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Missing configuration value [{section}] {key}") from exc
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section}] {key} must be a non-empty string")
    return value.strip()


def _absolute_path(data: dict[str, Any], section: str, key: str) -> Path:
    value = Path(_string(data, section, key)).expanduser()
    if not value.is_absolute():
        raise ConfigError(f"[{section}] {key} must be an absolute path")
    return value.resolve(strict=False)


def _excluded_paths(data: dict[str, Any]) -> tuple[str, ...]:
    section = data.get("sync", {})
    if not isinstance(section, dict):
        raise ConfigError("[sync] must be a table")
    values = section.get("exclude", [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ConfigError("[sync] exclude must be an array of relative path strings")

    normalized: list[str] = []
    for value in values:
        try:
            path = normalize_relative_path(tuple(value.split("/")))
        except ScanError as exc:
            raise ConfigError(f"Invalid [sync] exclusion {value!r}: {exc}") from exc
        if path in normalized:
            raise ConfigError(f"Duplicate [sync] exclusion: {path!r}")
        normalized.append(path)
    return tuple(normalized)


def _excluded_names(data: dict[str, Any]) -> tuple[str, ...]:
    section = data.get("sync", {})
    if not isinstance(section, dict):
        raise ConfigError("[sync] must be a table")
    values = section.get("exclude_names", [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ConfigError(
            "[sync] exclude_names must be an array of filename strings"
        )

    normalized: list[str] = []
    for value in values:
        try:
            name = normalize_relative_path((value,))
        except ScanError as exc:
            raise ConfigError(
                f"Invalid [sync] filename exclusion {value!r}: {exc}"
            ) from exc
        if name in normalized:
            raise ConfigError(f"Duplicate [sync] filename exclusion: {name!r}")
        normalized.append(name)
    return tuple(normalized)


def _validate(config: AppConfig, config_path: Path) -> None:
    if config.state_database == config.log_file:
        raise ConfigError("The state database and log file must be different paths")
    if not config.box_folder_id.isdigit():
        raise ConfigError("[box] folder_id must contain only digits")

    config_git_root = _find_git_root(config_path.parent)
    if config_git_root is not None:
        raise ConfigError(
            f"Configuration must be stored outside a Git checkout: {config_git_root}"
        )

    for label, path in (
        ("state database", config.state_database),
        ("log file", config.log_file),
    ):
        if path.is_relative_to(config.local_root):
            raise ConfigError(f"The {label} must be outside the synchronized folder")
        containing_repo = _find_git_root(path.parent)
        if containing_repo is not None:
            raise ConfigError(f"The {label} must be outside Git: {containing_repo}")


def _find_git_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None
