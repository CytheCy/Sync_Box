from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from sync_box.systemd_units import (
    GENERATED_HEADER,
    SERVICE_NAME,
    TIMER_NAME,
    SystemdUnitError,
    install_user_units,
    render_user_units,
    uninstall_user_units,
)


class SystemdUnitTests(unittest.TestCase):
    def test_units_are_periodic_oneshot_without_embedded_secrets_or_retries(self) -> None:
        units = render_user_units(Path("/opt/Sync Box/bin/sync-box"))
        service = units[SERVICE_NAME]
        timer = units[TIMER_NAME]

        self.assertIn('ExecStart="/opt/Sync Box/bin/sync-box" run --summary-only', service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("Restart=no", service)
        self.assertIn("StandardOutput=journal", service)
        self.assertNotIn("SuccessExitStatus", service)
        self.assertNotIn("token", service.lower())
        self.assertNotIn("credential", service.lower())
        self.assertIn("OnCalendar=*-*-* *:00,30:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=2m", timer)
        self.assertNotIn("Restart", timer)

    @patch("sync_box.systemd_units._daemon_reload")
    def test_install_and_uninstall_manage_only_generated_units(self, reload_mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            install_user_units(Path(os.sys.executable), directory)

            for name in (SERVICE_NAME, TIMER_NAME):
                path = directory / name
                self.assertTrue(path.read_text(encoding="utf-8").startswith(GENERATED_HEADER))
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)

            uninstall_user_units(directory)
            self.assertFalse((directory / SERVICE_NAME).exists())
            self.assertFalse((directory / TIMER_NAME).exists())
            self.assertEqual(reload_mock.call_count, 2)

    @patch("sync_box.systemd_units._daemon_reload")
    def test_install_refuses_to_replace_an_unmanaged_unit(self, _reload_mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / SERVICE_NAME).write_text("[Service]\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemdUnitError, "unmanaged"):
                install_user_units(Path(os.sys.executable), directory)


if __name__ == "__main__":
    unittest.main()
