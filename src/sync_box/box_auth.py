"""Authentication through the official Box CLI with read-only token downscoping."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Sequence

from sync_box.box_errors import format_box_api_error, safe_error_detail
from sync_box.config import AppConfig
from sync_box.dependencies import user_box_cli_path


BOX_CLI_ENVIRONMENT = "sync-box"
MINIMUM_BOX_CLI_VERSION = (4, 6, 0)
READ_ONLY_SCOPES = "root_readonly,item_download"
READ_WRITE_SCOPES = "root_readwrite,item_download"


class AuthenticationError(RuntimeError):
    """Raised for missing, invalid, or failed Box authentication."""


def authorize(*, reauthorize: bool = False, code: bool = False) -> None:
    """Delegate interactive OAuth login to the official Box CLI application."""
    executable = _box_cli_executable()
    arguments = [
        executable,
        "login",
        "--default-box-app",
        "--name",
        BOX_CLI_ENVIRONMENT,
    ]
    if reauthorize:
        arguments.append("--reauthorize")
    if code:
        arguments.append("--code")

    try:
        result = subprocess.run(arguments, check=False)
    except OSError as exc:
        raise AuthenticationError("Could not start the Box CLI login flow") from exc
    if result.returncode != 0:
        raise AuthenticationError(
            "Box CLI login failed; review the message above and try again"
        )


def build_authenticated_client(config: AppConfig) -> Any:
    """Build an SDK client from a short-lived, read-only token held in memory."""
    del config  # Authentication state belongs exclusively to the Box CLI.
    token = _read_only_access_token()
    BoxClient, BoxDeveloperTokenAuth = _sdk_components()
    return BoxClient(auth=BoxDeveloperTokenAuth(token))


def build_write_authenticated_client(config: AppConfig) -> Any:
    """Build a short-lived content read/write client for an executing sync."""
    del config
    token = _access_token(READ_WRITE_SCOPES, "read/write")
    BoxClient, BoxDeveloperTokenAuth = _sdk_components()
    return BoxClient(auth=BoxDeveloperTokenAuth(token))


def test_authentication(config: AppConfig) -> tuple[str, str]:
    client = build_authenticated_client(config)
    try:
        user = client.users.get_user_me(fields=["id", "name"])
    except Exception as exc:
        raise AuthenticationError(
            f"Box authentication test failed: {format_box_api_error(exc)}"
        ) from exc
    return str(user.id), str(user.name)


def _read_only_access_token() -> str:
    """Exchange the CLI's credential for a non-refreshable read-only token."""
    return _access_token(READ_ONLY_SCOPES, "read-only")


def _access_token(scopes: str, label: str) -> str:
    executable = _box_cli_executable()
    result = _run_captured(
        [
            executable,
            "tokens:exchange",
            scopes,
            "--no-color",
        ]
    )
    if result.returncode != 0:
        detail = safe_error_detail(result.stderr)
        message = f"Box CLI could not issue a {label} token"
        if detail:
            message += f": {detail}"
        message += (
            f"; run 'sync-box auth login --reauthorize' for environment "
            f"'{BOX_CLI_ENVIRONMENT}'"
        )
        raise AuthenticationError(message)

    token = result.stdout.strip()
    if not token:
        raise AuthenticationError(
            f"Box CLI returned no {label} token despite reporting success"
        )
    if any(character.isspace() for character in token):
        raise AuthenticationError(f"Box CLI returned an invalid {label} token")
    return token


def _box_cli_executable() -> str:
    executable = (
        str(user_box_cli_path())
        if user_box_cli_path().is_file()
        else shutil.which("box")
    )
    if executable is None:
        raise AuthenticationError(
            "The official Box CLI is not installed; install @box/cli 4.6 or newer"
        )

    result = _run_captured([executable, "--version"])
    if result.returncode != 0:
        raise AuthenticationError("Could not determine the Box CLI version")
    version = _parse_version(result.stdout)
    if version is None:
        raise AuthenticationError(
            f"Unrecognized Box CLI version output: {safe_error_detail(result.stdout)}"
        )
    if version < MINIMUM_BOX_CLI_VERSION:
        installed = ".".join(str(part) for part in version)
        raise AuthenticationError(
            f"Box CLI {installed} is too old; version 4.6 or newer is required"
        )
    return str(Path(executable).resolve(strict=False))


def _parse_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:@box/cli|box-cli)/(\d+)\.(\d+)\.(\d+)", output)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _run_captured(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthenticationError("Could not run the official Box CLI") from exc


def _sdk_components() -> tuple[Any, Any]:
    try:
        from box_sdk_gen import BoxClient, BoxDeveloperTokenAuth
    except ImportError as exc:
        raise AuthenticationError(
            "The Box SDK is not installed; install the Python boxsdk dependency "
            "for this application."
        ) from exc
    return BoxClient, BoxDeveloperTokenAuth
