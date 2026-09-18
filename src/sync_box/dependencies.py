"""Detection and verified per-user installation of the official Box CLI."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import BinaryIO, Callable
from urllib.request import Request, urlopen


BOX_RELEASE_API = "https://api.github.com/repos/box/boxcli/releases/latest"
OFFICIAL_REPOSITORY = "https://github.com/box/boxcli"
MINIMUM_VERSION = (4, 6, 0)


class DependencyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BoxCliStatus:
    installed: bool
    usable: bool
    executable: Path | None = None
    version: tuple[int, int, int] | None = None
    detail: str = "Box CLI needs to be installed."


def user_box_cli_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home.expanduser() / "sync-box" / "box-cli" / "current" / "bin" / "box"


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:@box/cli|box-cli)/(\d+)\.(\d+)\.(\d+)", text)
    return tuple(map(int, match.groups())) if match else None  # type: ignore[return-value]


class BoxCliManager:
    """Small replaceable boundary around the external Box CLI dependency."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        opener: Callable[..., BinaryIO] = urlopen,
        install_path: Path | None = None,
    ) -> None:
        self._runner = runner
        self._opener = opener
        self.install_path = install_path or user_box_cli_path()

    def status(self) -> BoxCliStatus:
        found = shutil.which("box")
        executable = (
            self.install_path if self.install_path.is_file()
            else Path(found).resolve(strict=False) if found else self.install_path
        )
        return self._status_for(executable)

    def _status_for(self, executable: Path) -> BoxCliStatus:
        if not executable.is_file():
            return BoxCliStatus(False, False)
        try:
            result = self._runner(
                [str(executable), "--version"], check=False, capture_output=True,
                text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return BoxCliStatus(True, False, executable, detail="Box CLI could not be started.")
        version = _parse_version(result.stdout)
        if result.returncode or version is None:
            return BoxCliStatus(True, False, executable, detail="Box CLI version could not be verified.")
        if version < MINIMUM_VERSION:
            shown = ".".join(map(str, version))
            return BoxCliStatus(True, False, executable, version, f"Box CLI {shown} is too old.")
        return BoxCliStatus(True, True, executable, version, f"Box CLI {'.'.join(map(str, version))} installed.")

    def install(self) -> BoxCliStatus:
        """Install a digest-verified Linux binary from Box's official release."""
        machine = platform.machine().lower()
        architecture = {
            "x86_64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64", "armv7l": "arm",
        }.get(machine)
        if architecture is None or platform.system() != "Linux":
            raise DependencyError(f"No official Box CLI Linux build is available for {machine}.")

        release = self._json(BOX_RELEASE_API)
        tag = str(release.get("tag_name", ""))
        tag_match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
        if tag_match is None:
            raise DependencyError("The official Box release had an invalid version tag.")
        release_version = tuple(map(int, tag_match.groups()))
        asset_pattern = re.compile(rf"box-v[0-9.]+-linux-{re.escape(architecture)}\.tar\.gz$")
        assets = [a for a in release.get("assets", []) if asset_pattern.fullmatch(str(a.get("name", "")))]
        if len(assets) != 1:
            raise DependencyError("The official Box release did not contain the expected Linux build.")
        asset = assets[0]
        url = str(asset.get("browser_download_url", ""))
        digest_value = str(asset.get("digest", ""))
        if not url.startswith("https://github.com/box/boxcli/releases/download/"):
            raise DependencyError("The Box CLI download URL was not from the official Box repository.")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest_value):
            raise DependencyError("The official Box release did not publish a usable SHA-256 digest.")
        expected = digest_value.split(":", 1)[1].lower()

        if self.install_path.parent.name != "bin" or self.install_path.parent.parent.name != "current":
            raise DependencyError("The Box CLI installation target has an unsafe layout.")
        install_root = self.install_path.parent.parent.parent
        install_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sync-box-cli-") as temporary:
            archive = Path(temporary) / str(asset["name"])
            self._download(url, archive)
            actual = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
            if actual != expected:
                raise DependencyError("The downloaded Box CLI failed SHA-256 verification.")
            extracted = Path(temporary) / "extracted"
            extracted.mkdir()
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                if not members or any(
                    Path(member.name).is_absolute()
                    or ".." in Path(member.name).parts
                    or not Path(member.name).parts
                    or Path(member.name).parts[0] != "box"
                    for member in members
                ):
                    raise DependencyError("The verified Box CLI archive had an unsafe layout.")
                try:
                    bundle.extractall(extracted, filter="data")
                except (OSError, tarfile.TarError, ValueError) as exc:
                    raise DependencyError("The verified Box CLI archive could not be safely extracted.") from exc
            candidate = extracted / "box"
            candidate_executable = candidate / "bin" / "box"
            if not candidate_executable.is_file():
                raise DependencyError("The verified Box CLI archive had an unexpected layout.")
            candidate_executable.chmod(
                candidate_executable.stat().st_mode | stat.S_IXUSR
            )
            release_directory = install_root / "releases" / tag.removeprefix("v")
            release_directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not release_directory.exists():
                candidate.replace(release_directory)
            link = install_root / "current"
            temporary_link = install_root / ".current.new"
            temporary_link.unlink(missing_ok=True)
            temporary_link.symlink_to(release_directory.relative_to(install_root), target_is_directory=True)
            temporary_link.replace(link)
            directory_fd = os.open(install_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        status = self._status_for(self.install_path)
        if not status.usable or status.version != release_version:
            link.unlink(missing_ok=True)
            raise DependencyError(f"Box CLI {tag or 'installation'} could not be verified: {status.detail}")
        return status

    def _json(self, url: str) -> dict[str, object]:
        request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Sync_Box/1.0"})
        try:
            with self._opener(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise DependencyError("Could not obtain the current official Box CLI release.") from exc

    def _download(self, url: str, target: Path) -> None:
        request = Request(url, headers={"User-Agent": "Sync_Box/1.0"})
        try:
            with self._opener(request, timeout=60) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise DependencyError("The official Box CLI download failed.") from exc
