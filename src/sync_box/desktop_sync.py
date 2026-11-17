"""Desktop preferences, systemd scheduling, and read-only sync history."""
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import sqlite3
import subprocess

from sync_box.config import default_config_path
from sync_box.systemd_units import _atomic_write, user_unit_directory, SystemdUnitError

INTERVALS = (5, 10, 15, 30, 60, 120, 240, 720, 1440)

@dataclass
class Preferences:
    interval: int = 30
    watch_folder: bool = True
    sync_at_login: bool = True


def preferences_path() -> Path:
    return default_config_path().parent / 'desktop.json'


def load_preferences() -> Preferences:
    try:
        data = json.loads(preferences_path().read_text())
        return Preferences(
            interval=data['interval'] if data.get('interval') in INTERVALS else 30,
            watch_folder=data.get('watch_folder', True) is True,
            sync_at_login=data.get('sync_at_login', True) is True,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return Preferences()


def save_preferences(preferences: Preferences) -> None:
    target = preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(target, json.dumps(asdict(preferences), indent=2) + '\n')


def calendar_for_interval(minutes: int) -> str:
    if minutes not in INTERVALS:
        raise ValueError('Unsupported sync interval')
    if minutes == 1440:
        return '*-*-* 00:00:00'
    if minutes < 60:
        return f'*-*-* *:0/{minutes}:00'
    return f'*-*-* 0/{minutes // 60}:00:00'


def set_sync_interval(minutes: int, *, enabled: bool) -> None:
    calendar = calendar_for_interval(minutes)
    target = user_unit_directory() / 'sync-box.timer.d' / 'schedule.conf'
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.read_text() if target.exists() else None
    _atomic_write(target, '[Timer]\nOnCalendar=\nOnCalendar=' + calendar +
                  '\nRandomizedDelaySec=0\nAccuracySec=1s\n')
    commands = [['systemctl', '--user', 'daemon-reload']]
    if enabled:
        commands.append(['systemctl', '--user', 'restart', 'sync-box.timer'])
    try:
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise SystemdUnitError(result.stderr.strip() or 'Could not update sync schedule')
    except (OSError, subprocess.TimeoutExpired, SystemdUnitError):
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, previous)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=15)
        raise


def transfer_history(path: Path) -> list[tuple[str, str, str, str]]:
    """Use journaled transfers and deletions, including partially failed runs.

    The existing journal dates runs rather than individual operations; use the
    run's finish time, or start time while it is in progress.
    """
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        return db.execute(
            "SELECT coalesce(r.finished_at,r.started_at), o.action, o.relative_path, o.status "
            "FROM sync_operations o JOIN sync_runs r ON r.id=o.run_id "
            "WHERE r.dry_run=0 AND (o.action LIKE 'upload_%' OR o.action LIKE 'download_%' "
            "OR o.action IN ('delete_box', 'delete_local')) "
            "AND julianday(coalesce(r.finished_at,r.started_at)) >= julianday('now','-48 hours') "
            "ORDER BY r.id DESC, o.id DESC"
        ).fetchall()
