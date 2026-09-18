from pathlib import Path
import tempfile
import unittest

from sync_box.config import load_config
from sync_box.setup_config import SetupError, box_folder_id_from_url, create_initial_config


class SetupConfigTests(unittest.TestCase):
    def test_extracts_box_folder_url_without_exposing_it_in_result(self) -> None:
        self.assertEqual(box_folder_id_from_url("https://app.box.com/folder/12345"), "12345")

    def test_rejects_lookalike_box_domain_and_relative_local_path(self) -> None:
        with self.assertRaises(SetupError):
            box_folder_id_from_url("https://evilbox.com/folder/12345")
        with self.assertRaises(SetupError):
            create_initial_config(Path("."), "https://app.box.com/folder/12345")
        with self.assertRaises(SetupError):
            box_folder_id_from_url("https://example.com/folder/12345")

    def test_first_run_writes_only_external_nonsecret_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local"
            local.mkdir()
            config_path = root / "config" / "config.toml"
            state = root / "state"
            create_initial_config(
                local,
                "https://app.box.com/folder/987",
                config_path=config_path,
                state_directory=state,
            )
            config = load_config(config_path)
            self.assertEqual(config.local_root, local)
            self.assertEqual(config.box_folder_id, "987")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(config.state_database.exists())
            contents = config_path.read_text(encoding="utf-8").lower()
            self.assertNotIn("token", contents)
            with self.assertRaises(SetupError):
                create_initial_config(local, "https://app.box.com/folder/987", config_path=config_path)


if __name__ == "__main__":
    unittest.main()
