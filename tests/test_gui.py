import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    raise unittest.SkipTest("PySide6 is not installed in this interpreter")

from sync_box.app_status import (
    AuthenticationState, DatabaseState, StatusKind, StatusSnapshot,
    SystemdState, UnitState,
)
from sync_box.gui import MainWindow, SettingsDialog


class FakeProvider:
    def __init__(self, root: Path) -> None:
        self.config_path = root / "missing.toml"
        self.controller = Mock()
        self.snapshot = StatusSnapshot(
            StatusKind.NOT_CONFIGURED,
            "Not configured",
            "Choose a local folder and Box folder in Settings.",
            None,
            SystemdState(UnitState(), UnitState()),
            DatabaseState(),
            auth_state=AuthenticationState.UNKNOWN,
        )

    def read(self, **_kwargs):
        return self.snapshot


class GuiBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.temporary = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def make_window(self):
        provider = FakeProvider(Path(self.temporary.name))
        with patch.object(MainWindow, "check_authentication"):
            window = MainWindow(provider)
        return window, provider

    def test_close_hides_window_for_tray_operation(self) -> None:
        window, _provider = self.make_window()
        window.show()
        self.app.processEvents()
        window.close()
        self.app.processEvents()
        self.assertFalse(window.isVisible())
        self.assertFalse(window._quitting)
        window.tray.hide()
        window.deleteLater()

    def test_quit_does_not_disable_timer(self) -> None:
        window, provider = self.make_window()
        with patch.object(QApplication, "quit") as quit_method:
            window.quit_gui()
        quit_method.assert_called_once()
        provider.controller.set_timer_enabled.assert_not_called()
        window.deleteLater()

    def test_timer_cannot_be_enabled_before_safe_setup(self) -> None:
        window, _provider = self.make_window()
        window.snapshot = StatusSnapshot(
            window.snapshot.kind,
            window.snapshot.title,
            window.snapshot.detail,
            window.snapshot.config,
            SystemdState(UnitState(), UnitState(load_state="loaded")),
            window.snapshot.database,
            auth_state=window.snapshot.auth_state,
        )
        dialog = SettingsDialog(window)
        self.assertFalse(dialog.timer.isEnabled())
        dialog.deleteLater()
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
