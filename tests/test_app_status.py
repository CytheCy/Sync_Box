from pathlib import Path
from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from sync_box.app_status import (
    AuthenticationState, StatusKind, StatusProvider, SystemdController,
    SystemdState, UnitState, validated_local_folder,
)
from sync_box.database import initialize_database


def write_config(path: Path, root: Path, database: Path) -> None:
    path.write_text(
        f"""[local]
root = "{root}"
[box]
folder_id = "123"
[sync]
exclude = []
exclude_names = []
[storage]
state_database = "{database}"
log_file = "{database.parent / 'sync.log'}"
""",
        encoding="utf-8",
    )


def system_state(*, running: bool = False, failed: bool = False, enabled: bool = True) -> SystemdState:
    return SystemdState(
        UnitState(
            load_state="loaded",
            active_state="active" if running else "inactive",
            sub_state="running" if running else "dead",
            result="exit-code" if failed else "success",
            exec_status=1 if failed else 0,
        ),
        UnitState(
            load_state="loaded",
            active_state="active" if enabled else "inactive",
            sub_state="waiting" if enabled else "dead",
            unit_file_state="enabled" if enabled else "disabled",
        ),
    )


class StatusProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.local = self.root / "local"
        self.local.mkdir()
        self.config = self.root / "config.toml"
        self.database = self.root / "state" / "state.sqlite3"
        self.controller = Mock()
        self.controller.snapshot.return_value = system_state()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provider(self) -> StatusProvider:
        return StatusProvider(self.config, self.controller)

    def establish_baseline(self) -> None:
        initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            generation = connection.execute(
                "INSERT INTO baseline_generations(local_root, box_root_id) VALUES (?, '123')",
                (str(self.local),),
            ).lastrowid
            connection.execute(
                "INSERT INTO current_baseline(singleton, generation_id) VALUES (1, ?)",
                (generation,),
            )
            connection.execute(
                "INSERT INTO baseline_items(generation_id, relative_path, item_type) "
                "VALUES (?, '.', 'folder')",
                (generation,),
            )
            connection.execute(
                "INSERT INTO sync_runs(started_at, finished_at, dry_run, outcome, summary) "
                "VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 'completed', 'verified')"
            )

    def test_missing_configuration_is_not_configured(self) -> None:
        status = self.provider().read()
        self.assertEqual(status.kind, StatusKind.NOT_CONFIGURED)
        self.assertIsNone(status.config)

    def test_missing_baseline_never_claims_success(self) -> None:
        write_config(self.config, self.local, self.database)
        status = self.provider().read()
        self.assertEqual(status.kind, StatusKind.ATTENTION)
        self.assertIn("baseline", status.detail)

    def test_running_failed_and_authentication_states(self) -> None:
        write_config(self.config, self.local, self.database)
        self.establish_baseline()

        self.controller.snapshot.return_value = system_state(running=True)
        self.assertEqual(self.provider().read().kind, StatusKind.SYNCING)

        self.controller.snapshot.return_value = system_state(failed=True)
        self.assertEqual(self.provider().read().kind, StatusKind.ERROR)

        self.controller.snapshot.return_value = system_state()
        status = self.provider().read(auth_state=AuthenticationState.REQUIRED)
        self.assertEqual(status.kind, StatusKind.AUTH_REQUIRED)

    def test_success_requires_baseline_and_nonfailed_service(self) -> None:
        write_config(self.config, self.local, self.database)
        self.establish_baseline()
        status = self.provider().read(
            auth_state=AuthenticationState.CONNECTED, account="person@example.com"
        )
        self.assertEqual(status.kind, StatusKind.UP_TO_DATE)
        self.assertEqual(status.database.item_count, 1)
        self.assertEqual(status.account, "person@example.com")

    def test_unresolved_conflict_is_presented(self) -> None:
        write_config(self.config, self.local, self.database)
        self.establish_baseline()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO conflicts(relative_path, detected_at, reason) "
                "VALUES ('file.txt', CURRENT_TIMESTAMP, 'both changed')"
            )
        self.assertEqual(self.provider().read().kind, StatusKind.CONFLICT)

    def test_planned_conflict_exposes_path_and_reason(self) -> None:
        write_config(self.config, self.local, self.database)
        self.establish_baseline()
        plan = [{
            "action": "conflict",
            "relative_path": "photos/aaa.jpeg",
            "reason": "local name differs only by case from an existing Box item",
        }]
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO sync_runs(started_at, finished_at, dry_run, outcome, "
                "summary, plan_json) VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "0, 'failed', 'conflicts=1', ?)",
                (json.dumps(plan),),
            )

        status = self.provider().read()

        self.assertEqual(status.kind, StatusKind.CONFLICT)
        self.assertEqual(status.database.conflicts[0].relative_path, "photos/aaa.jpeg")
        self.assertIn("differs only by case", status.database.conflicts[0].reason)

    def test_local_folder_validation(self) -> None:
        write_config(self.config, self.local, self.database)
        snapshot = self.provider().read()
        self.assertEqual(validated_local_folder(snapshot.config), self.local)
        self.local.rmdir()
        self.assertIsNone(validated_local_folder(snapshot.config))


class SystemdControllerTests(unittest.TestCase):
    def test_reads_service_and_timer_properties(self) -> None:
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nUnitFileState=static\n", ""),
            subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\nNextElapseUSecRealtime=tomorrow\n", ""),
        ])
        state = SystemdController(runner).snapshot()
        self.assertTrue(state.service.running)
        self.assertTrue(state.timer_enabled)
        self.assertEqual(state.timer.next_elapse, "tomorrow")

    def test_sync_now_delegates_to_systemd(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        controller = SystemdController(runner)
        result = controller.start_sync(system_state())
        self.assertTrue(result.accepted)
        command = runner.call_args.args[0]
        self.assertEqual(command, ["systemctl", "--user", "start", "--no-block", "sync-box.service"])

    def test_overlapping_request_is_handled_without_starting(self) -> None:
        runner = Mock()
        result = SystemdController(runner).start_sync(system_state(running=True))
        self.assertTrue(result.already_running)
        self.assertIn("already", result.message)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
