"""Read-only application status and narrow systemd controller for the GUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Callable, Mapping, Sequence

from sync_box.config import AppConfig, ConfigError, default_config_path, load_config


SERVICE_NAME = "sync-box.service"
TIMER_NAME = "sync-box.timer"


class StatusKind(str, Enum):
    UP_TO_DATE = "up_to_date"
    SYNCING = "syncing"
    ATTENTION = "attention"
    CONFLICT = "conflict"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    AUTH_REQUIRED = "auth_required"


class AuthenticationState(str, Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    CONNECTED = "connected"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class ConflictDetail:
    relative_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class UnitState:
    load_state: str = "not-found"
    active_state: str = "inactive"
    sub_state: str = "dead"
    result: str = "success"
    exec_status: int = 0
    unit_file_state: str = "disabled"
    next_elapse: str | None = None

    @property
    def running(self) -> bool:
        return self.active_state in {"active", "activating", "reloading"}

    @property
    def installed(self) -> bool:
        return self.load_state not in {"not-found", "masked"}


@dataclass(frozen=True, slots=True)
class SystemdState:
    service: UnitState
    timer: UnitState

    @property
    def timer_enabled(self) -> bool:
        return self.timer.unit_file_state in {"enabled", "enabled-runtime"}


@dataclass(frozen=True, slots=True)
class DatabaseState:
    has_baseline: bool = False
    item_count: int = 0
    last_completed: datetime | None = None
    latest_outcome: str | None = None
    latest_summary: str | None = None
    conflict: bool = False
    conflicts: tuple[ConflictDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    kind: StatusKind
    title: str
    detail: str
    config: AppConfig | None
    systemd: SystemdState
    database: DatabaseState
    account: str | None = None
    auth_state: AuthenticationState = AuthenticationState.UNKNOWN
    config_error: str | None = None

    @property
    def local_folder(self) -> Path | None:
        return self.config.local_root if self.config else None


@dataclass(frozen=True, slots=True)
class StartResult:
    accepted: bool
    already_running: bool = False
    message: str = ""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class SystemdController:
    """Perform only the user-manager operations explicitly exposed by the GUI."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._runner = runner

    def snapshot(self) -> SystemdState:
        return SystemdState(
            service=self._show(SERVICE_NAME),
            timer=self._show(TIMER_NAME),
        )

    def start_sync(self, current: SystemdState | None = None) -> StartResult:
        state = current or self.snapshot()
        if state.service.running:
            return StartResult(False, True, "Synchronization is already in progress.")
        if not state.service.installed:
            return StartResult(False, False, "The Sync_Box systemd service is not installed.")
        result = self._run(
            ["systemctl", "--user", "start", "--no-block", SERVICE_NAME]
        )
        if result.returncode != 0:
            detail = " ".join(result.stderr.split())
            return StartResult(False, False, detail or "Could not start synchronization.")
        return StartResult(True, False, "Synchronization requested.")

    def set_timer_enabled(self, enabled: bool) -> tuple[bool, str]:
        action = "enable" if enabled else "disable"
        arguments = ["systemctl", "--user", action, "--now", TIMER_NAME]
        result = self._run(arguments)
        if result.returncode == 0:
            verb = "enabled" if enabled else "disabled"
            return True, f"Automatic synchronization {verb}."
        return False, " ".join(result.stderr.split()) or "Could not update the timer."

    def _show(self, unit: str) -> UnitState:
        properties = (
            "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
            "UnitFileState", "NextElapseUSecRealtime",
        )
        result = self._run(
            ["systemctl", "--user", "show", unit, *[f"--property={p}" for p in properties]]
        )
        values = _parse_properties(result.stdout)
        try:
            exec_status = int(values.get("ExecMainStatus", "0") or 0)
        except ValueError:
            exec_status = 1
        return UnitState(
            load_state=values.get("LoadState", "not-found"),
            active_state=values.get("ActiveState", "inactive"),
            sub_state=values.get("SubState", "dead"),
            result=values.get("Result", "success"),
            exec_status=exec_status,
            unit_file_state=values.get("UnitFileState", "disabled"),
            next_elapse=values.get("NextElapseUSecRealtime") or None,
        )

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                list(arguments), check=False, capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(list(arguments), 1, "", str(exc))


class StatusProvider:
    def __init__(
        self,
        config_path: Path | None = None,
        controller: SystemdController | None = None,
    ) -> None:
        self.config_path = config_path or default_config_path()
        self.controller = controller or SystemdController()

    def read(
        self,
        *,
        auth_state: AuthenticationState = AuthenticationState.UNKNOWN,
        account: str | None = None,
        requested: bool = False,
    ) -> StatusSnapshot:
        systemd = self.controller.snapshot()
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            return StatusSnapshot(
                StatusKind.NOT_CONFIGURED,
                "Not configured",
                "Choose a local folder and Box folder in Settings.",
                None,
                systemd,
                DatabaseState(),
                auth_state=auth_state,
                config_error=str(exc),
            )
        if not config.local_root.is_dir():
            return StatusSnapshot(
                StatusKind.NOT_CONFIGURED,
                "Not configured",
                "The configured local folder is unavailable.",
                config,
                systemd,
                DatabaseState(),
                account,
                auth_state,
            )

        database = read_database_state(config.state_database)
        if systemd.service.running or requested:
            return StatusSnapshot(
                StatusKind.SYNCING, "Syncing", "Synchronization is in progress.",
                config, systemd, database, account, auth_state,
            )
        if database.conflict:
            return StatusSnapshot(
                StatusKind.CONFLICT, "Conflict", "A conflict requires attention.",
                config, systemd, database, account, auth_state,
            )
        if auth_state is AuthenticationState.REQUIRED:
            return StatusSnapshot(
                StatusKind.AUTH_REQUIRED,
                "Box authentication required",
                "Reconnect Box in Settings.",
                config, systemd, database, account, auth_state,
            )
        if not database.has_baseline:
            return StatusSnapshot(
                StatusKind.ATTENTION,
                "Attention needed",
                "A verified baseline has not been established.",
                config, systemd, database, account, auth_state,
            )
        service_failed = (
            systemd.service.result not in {"", "success"}
            or systemd.service.exec_status != 0
        )
        if database.latest_outcome == "failed" or service_failed:
            kind = (
                StatusKind.CONFLICT
                if "conflict" in (database.latest_summary or "").lower()
                else StatusKind.ERROR
            )
            title = "Conflict" if kind is StatusKind.CONFLICT else "Error"
            return StatusSnapshot(
                kind, title, database.latest_summary or "The last synchronization failed.",
                config, systemd, database, account, auth_state,
            )
        if not systemd.timer.installed:
            return StatusSnapshot(
                StatusKind.ATTENTION,
                "Attention needed",
                "The automatic synchronization timer is not installed.",
                config, systemd, database, account, auth_state,
            )
        return StatusSnapshot(
            StatusKind.UP_TO_DATE,
            "Up to date",
            "Everything is synchronized",
            config,
            systemd,
            database,
            account,
            auth_state,
        )


def read_database_state(path: Path) -> DatabaseState:
    if not path.is_file():
        return DatabaseState()
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            baseline = connection.execute(
                "SELECT generation_id FROM current_baseline WHERE singleton=1"
            ).fetchone()
            count = 0
            if baseline:
                count = int(connection.execute(
                    "SELECT count(*) FROM baseline_items WHERE generation_id=?",
                    (baseline[0],),
                ).fetchone()[0])
            latest = connection.execute(
                "SELECT finished_at, outcome, summary, plan_json FROM sync_runs "
                "WHERE dry_run=0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            completed = connection.execute(
                "SELECT finished_at FROM sync_runs "
                "WHERE dry_run=0 AND outcome='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            resolution = connection.execute(
                "SELECT 1 FROM conflict_resolution_runs "
                "WHERE outcome='in_progress' LIMIT 1"
            ).fetchone()
            unresolved = connection.execute(
                "SELECT 1 FROM conflicts WHERE resolved_at IS NULL LIMIT 1"
            ).fetchone()
    except (sqlite3.Error, OSError):
        return DatabaseState()

    plan_conflicts: tuple[ConflictDetail, ...] = ()
    if latest and latest[3]:
        try:
            plan_conflicts = tuple(
                ConflictDetail(
                    relative_path=str(item.get("relative_path", "Unknown path")),
                    reason=str(item.get("reason", "Conflict requires attention")),
                )
                for item in json.loads(latest[3])
                if isinstance(item, dict) and item.get("action") == "conflict"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return DatabaseState(
        has_baseline=baseline is not None,
        item_count=count,
        last_completed=_parse_sqlite_datetime(completed[0] if completed else None),
        latest_outcome=str(latest[1]) if latest and latest[1] else None,
        latest_summary=str(latest[2]) if latest and latest[2] else None,
        conflict=bool(resolution or unresolved or plan_conflicts),
        conflicts=plan_conflicts,
    )


def validated_local_folder(config: AppConfig | None) -> Path | None:
    if config is None:
        return None
    folder = config.local_root.resolve(strict=False)
    return folder if folder.is_dir() else None


def _parse_properties(output: str) -> Mapping[str, str]:
    return {
        key: value
        for line in output.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _parse_sqlite_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
