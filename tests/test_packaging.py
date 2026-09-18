from pathlib import Path
import unittest

from sync_box.resources import INSTALLED_CLI, PACKAGED_SERVICE, icon_path
from sync_box.systemd_units import render_user_units


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_package_resource_lookup_finds_icon(self) -> None:
        self.assertTrue(icon_path().is_file())
        self.assertEqual(icon_path().suffix, ".svg")

    def test_installed_paths_and_packaged_unit_are_fixed(self) -> None:
        self.assertEqual(INSTALLED_CLI, Path("/usr/bin/sync-box"))
        self.assertEqual(PACKAGED_SERVICE, Path("/usr/lib/systemd/user/sync-box.service"))
        service = (ROOT / "packaging/sync-box.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=/usr/bin/sync-box run --summary-only", service)
        self.assertIn("Restart=no", service)

    def test_packaged_timer_retains_safe_schedule(self) -> None:
        timer = (ROOT / "packaging/sync-box.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:00,30:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=2m", timer)
        self.assertIn("FixedRandomDelay=true", timer)

    def test_resources_contain_no_development_tree_paths(self) -> None:
        files = [
            *sorted((ROOT / "packaging").glob("*")),
            ROOT / "src/sync_box/resources.py",
        ]
        forbidden = ("/home/cport", "/Git/Sync_Box", "/.venv/")
        for path in files:
            if path.is_dir() or path.suffix == ".sh":
                continue
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, text, str(path))

    def test_generated_installed_unit_uses_installed_executable(self) -> None:
        service = render_user_units(INSTALLED_CLI)["sync-box.service"]
        self.assertIn('ExecStart="/usr/bin/sync-box" run --summary-only', service)

    def test_spec_does_not_enable_or_start_user_units(self) -> None:
        spec = (ROOT / "packaging/sync-box.spec").read_text(encoding="utf-8")
        self.assertNotIn("%systemd_user_post", spec)
        self.assertNotIn("systemctl --user enable", spec)
        self.assertIn("BuildArch:      noarch", spec)


if __name__ == "__main__":
    unittest.main()
