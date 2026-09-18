"""Resumable first-run orchestration over the existing safe sync machinery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path

from sync_box.app_status import AuthenticationState, SystemdController, read_database_state
from sync_box.autostart import set_autostart
from sync_box.box_auth import authorize, build_authenticated_client, test_authentication
from sync_box.box_inventory import scan_box
from sync_box.config import AppConfig, default_config_path, load_config
from sync_box.database import load_baseline, replace_baseline
from sync_box.dependencies import BoxCliManager, BoxCliStatus
from sync_box.initial_download import DownloadResult, execute_initial_download
from sync_box.inventory import InventoryItem, summarize
from sync_box.local_inventory import scan_local
from sync_box.planner import build_comparison_plan, build_initial_download_plan, summarize_plan
from sync_box.requirements import FolderKind, RequirementChecker, inspect_folder
from sync_box.setup_config import SetupError, create_initial_config
from sync_box.two_way import validate_baseline_match


class SetupStep(str, Enum):
    WELCOME = "welcome"
    REQUIREMENTS = "requirements"
    BOX_CLI = "box_cli"
    AUTH = "auth"
    LOCAL_FOLDER = "local_folder"
    INITIAL_SYNC = "initial_sync"
    ATTENTION = "attention"
    AUTOMATIC_SYNC = "automatic_sync"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class InventoryAnalysis:
    local_items: tuple[InventoryItem, ...]
    box_items: tuple[InventoryItem, ...]
    local_empty: bool
    identical: bool
    summary: dict[str, int]
    differences: dict[str, int]

    @property
    def box_files(self) -> int:
        return sum(item.item_type == "file" for item in self.box_items)

    @property
    def box_folders(self) -> int:
        return max(0, sum(item.item_type == "folder" for item in self.box_items) - 1)


class SetupController:
    """No wizard-state file: every decision is derived from durable real state."""

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
        self.requirements = RequirementChecker(
            self.config_path, dependencies=self.dependencies, systemd=self.systemd
        )

    def box_cli_status(self) -> BoxCliStatus:
        return self.dependencies.status()

    def install_box_cli(self) -> BoxCliStatus:
        return self.dependencies.install()

    def authenticate(self, *, reauthorize: bool = False) -> tuple[str, str]:
        authorize(reauthorize=reauthorize)
        return self.test_authentication()

    def test_authentication(self) -> tuple[str, str]:
        config = self._config_or_placeholder()
        return test_authentication(config)

    def prepare_local_folder(self, path: Path, *, create: bool = False) -> FolderKind:
        kind, detail = inspect_folder(path)
        if kind is FolderKind.MISSING and create:
            path.expanduser().mkdir(mode=0o700, parents=True)
            kind, detail = inspect_folder(path)
        if kind in {FolderKind.MISSING, FolderKind.INACCESSIBLE, FolderKind.UNSAFE}:
            raise SetupError(detail)
        if not self.config_path.exists():
            create_initial_config(path, config_path=self.config_path, box_folder_id="0")
        else:
            configured = load_config(self.config_path)
            if configured.local_root != path.expanduser().resolve(strict=False):
                raise SetupError("Sync_Box is already configured for a different local folder.")
        return kind

    def analyze(self) -> InventoryAnalysis:
        config = load_config(self.config_path)
        local, box = self._fresh_inventories(config)
        comparison = build_comparison_plan(local, box)
        differences = summarize_plan(comparison)
        identical = differences["review"] == 0
        return InventoryAnalysis(
            tuple(local), tuple(box), len(local) == 1, identical,
            summarize(box), differences,
        )

    def bootstrap_from_box(self) -> tuple[DownloadResult, int]:
        """Safely execute/resume Box→local, then freshly verify and baseline."""
        config = load_config(self.config_path)
        client = build_authenticated_client(config)
        local, box = self._scan(config, client)
        plan = build_initial_download_plan(local, box)
        result = execute_initial_download(
            client, config.local_root, plan,
            refresh_client=lambda: build_authenticated_client(config),
        )
        generation = self._verify_and_baseline(config)
        return result, generation

    def establish_identical_baseline(self) -> int:
        return self._verify_and_baseline(load_config(self.config_path))

    def enable_automatic_sync(self) -> tuple[bool, str]:
        config = load_config(self.config_path)
        if load_baseline(config.state_database) is None:
            return False, "A verified baseline is required before enabling automatic sync."
        self.test_authentication()
        state = self.systemd.snapshot()
        if not state.timer.installed:
            return False, "The packaged Sync_Box timer is not installed."
        ok, message = self.systemd.set_timer_enabled(True)
        if not ok:
            return ok, message
        verified = self.systemd.snapshot()
        if not verified.timer_enabled or not verified.timer.running:
            return False, "The timer did not become enabled and active."
        return True, message

    def set_gui_autostart(self, enabled: bool) -> None:
        set_autostart(enabled)

    def setup_step(self, auth_state: AuthenticationState) -> SetupStep:
        status = self.requirements.check()
        if not all((status.supported_platform, status.package_resources, status.python_runtime, status.qt_runtime)):
            return SetupStep.REQUIREMENTS
        if not status.box_cli.usable:
            return SetupStep.BOX_CLI
        if auth_state is not AuthenticationState.CONNECTED:
            return SetupStep.AUTH
        if status.config is None or status.folder_kind not in {FolderKind.EMPTY, FolderKind.NONEMPTY}:
            return SetupStep.LOCAL_FOLDER
        if not status.database.has_baseline:
            return SetupStep.INITIAL_SYNC
        if not status.systemd.timer_enabled or not status.systemd.timer.running:
            return SetupStep.AUTOMATIC_SYNC
        return SetupStep.READY

    def _config_or_placeholder(self) -> AppConfig:
        if self.config_path.exists():
            return load_config(self.config_path)
        placeholder = Path(os.devnull)
        return AppConfig(Path.home(), "0", placeholder, placeholder, (), ())

    def _fresh_inventories(self, config: AppConfig) -> tuple[list[InventoryItem], list[InventoryItem]]:
        return self._scan(config, build_authenticated_client(config))

    @staticmethod
    def _scan(config: AppConfig, client: object) -> tuple[list[InventoryItem], list[InventoryItem]]:
        local = scan_local(
            config.local_root, hash_files=True,
            excluded_paths=config.excluded_paths, excluded_names=config.excluded_names,
        )
        box = scan_box(
            client, config.box_folder_id,
            excluded_paths=config.excluded_paths, excluded_names=config.excluded_names,
        )
        return local, box

    def _verify_and_baseline(self, config: AppConfig) -> int:
        local, box = self._fresh_inventories(config)
        validate_baseline_match(local, box)
        return replace_baseline(
            config.state_database, local_root=str(config.local_root),
            box_root_id=config.box_folder_id, local_items=local, box_items=box,
        )
