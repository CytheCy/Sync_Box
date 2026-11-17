"""Compact PySide6 tray interface for the existing systemd sync service."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path, PurePosixPath
import stat
import sqlite3
import sys

from PySide6.QtCore import QObject, QProcess, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal, QEvent
from PySide6.QtGui import QAction, QColor, QCloseEvent, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPlainTextEdit, QTabWidget,
    QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from sync_box.desktop_sync import (
    INTERVALS, Preferences, load_preferences, save_preferences, set_sync_interval, transfer_history,
)
from sync_box.folder_watch import FolderWatcher
from sync_box.systemd_units import SystemdUnitError

from sync_box.app_status import (
    AuthenticationState, ConflictDetail, StatusKind, StatusProvider, StatusSnapshot,
    validated_local_folder,
)
from sync_box.autostart import autostart_enabled, set_autostart
from sync_box.box_errors import safe_error_detail
from sync_box.config import ConfigError, default_config_path, load_config, folder_mode, save_folder_mode
from sync_box.inventory import is_excluded_path
from sync_box.logging_setup import configure_logging, log_failure
from sync_box.resources import cli_command, icon_path
from sync_box.setup_config import SetupError, create_initial_config, default_state_directory
from sync_box.requirements import FolderKind, inspect_folder
from sync_box.setup_controller import InventoryAnalysis, SetupController
from sync_box.sync_engine import _fsync_directory, _rename_noreplace


COLORS = {
    StatusKind.UP_TO_DATE: "#4aa3df",
    StatusKind.SYNCING: "#4aa3df",
    StatusKind.ATTENTION: "#d6a84b",
    StatusKind.CONFLICT: "#d68045",
    StatusKind.ERROR: "#d65c5c",
    StatusKind.NOT_CONFIGURED: "#8b929a",
    StatusKind.AUTH_REQUIRED: "#d6a84b",
}
LOGGER = logging.getLogger("sync_box.setup")
LOCAL_CASE_CONFLICT_REASON = (
    "local name differs only by case from an existing Box item"
)


class _TaskSignals(QObject):
    finished = Signal(object, object)


class _Task(QRunnable):
    def __init__(self, operation, callback) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _TaskSignals()
        self.signals.finished.connect(lambda result, error: callback(result, error))

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.operation(), None)
        except BaseException as exc:
            self.signals.finished.emit(None, exc)


class SetupWizard(QDialog):
    """Compact first-run flow driven by actual configuration and baseline state."""

    def __init__(self, window: "MainWindow", controller: SetupController | None = None) -> None:
        super().__init__(window)
        self.window = window
        self.controller = controller or SetupController(
            window.provider.config_path, systemd=window.provider.controller
        )
        self.analysis: InventoryAnalysis | None = None
        self._tasks: list[_Task] = []
        self.setWindowTitle("Set up Sync_Box")
        self.setModal(True)
        self.setMinimumSize(470, 330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        self.kicker = _section("WELCOME")
        self.title = QLabel("Box Cloud Sync")
        self.title.setObjectName("appTitle")
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.detail = QLabel()
        self.detail.setObjectName("secondary")
        self.detail.setWordWrap(True)
        self.path_row = QWidget()
        path_layout = QHBoxLayout(self.path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit(str(Path.home() / "Box"))
        self.choose = QPushButton("Choose…")
        self.choose.clicked.connect(self._choose_folder)
        path_layout.addWidget(self.path, 1)
        path_layout.addWidget(self.choose)
        self.autostart = QCheckBox("Show Sync_Box in the system tray when I log in")
        self.autostart.setChecked(autostart_enabled())
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.kicker)
        layout.addSpacing(5)
        layout.addWidget(self.title)
        layout.addSpacing(8)
        layout.addWidget(self.body)
        layout.addWidget(self.detail)
        layout.addWidget(self.path_row)
        layout.addWidget(self.autostart)
        layout.addWidget(self.progress)
        layout.addStretch()
        actions = QHBoxLayout()
        self.logs = QPushButton("Open Logs")
        self.logs.clicked.connect(window.open_logs)
        self.logs.hide()
        self.secondary = QPushButton("Retry")
        self.secondary.hide()
        self.primary = QPushButton("Get Started")
        self.primary.setProperty("primary", True)
        self.primary.clicked.connect(self._primary_clicked)
        actions.addWidget(self.logs)
        actions.addWidget(self.secondary)
        actions.addStretch()
        actions.addWidget(self.primary)
        layout.addLayout(actions)
        self.stage = "welcome"
        self._show_welcome()

    def _reset(self) -> None:
        self.path_row.hide()
        self.autostart.hide()
        self.logs.hide()
        self.secondary.hide()
        self.detail.clear()
        self.progress.hide()
        self.primary.setEnabled(True)

    def _page(self, stage: str, kicker: str, title: str, body: str, button: str) -> None:
        self.stage = stage
        self._reset()
        self.kicker.setText(kicker)
        self.title.setText(title)
        self.body.setText(body)
        self.primary.setText(button)

    def _show_welcome(self) -> None:
        self._page(
            "welcome", "WELCOME", "Sync_Box",
            "Keep a complete local copy of your Box files\nsynchronized automatically.",
            "Get Started",
        )

    def _primary_clicked(self) -> None:
        if self.stage == "welcome":
            self._resume()
        elif self.stage == "box":
            self._busy("Installing and verifying the official Box CLI…")
            self._run(self.controller.install_box_cli, self._installed)
        elif self.stage == "auth":
            self._busy("Waiting for Box authorization…")
            self._run(lambda: self.controller.authenticate(reauthorize=False), self._authenticated)
        elif self.stage == "folder":
            self._save_folder()
        elif self.stage == "initial":
            if self.analysis and self.analysis.identical:
                self._busy("Verifying both sides and establishing the baseline…")
                self._run(self.controller.establish_identical_baseline, self._baseline_finished)
            else:
                self._busy("Downloading and verifying your Box files…")
                self._run(self.controller.bootstrap_from_box, self._bootstrap_finished)
        elif self.stage == "automatic":
            self._busy("Enabling the packaged user timer…")
            self._run(self.controller.enable_automatic_sync, self._timer_finished)
        elif self.stage == "ready":
            try:
                self.controller.set_gui_autostart(self.autostart.isChecked())
            except OSError as exc:
                self._error("Could not save the tray preference", exc)
                return
            self.accept()
            self.window.refresh_status()
            self.window.show_window()
            QTimer.singleShot(0, self.window.check_authentication)

    def _resume(self) -> None:
        report = self.controller.requirements.check()
        missing = []
        if not report.supported_platform:
            missing.append("This system is not a supported Fedora/Linux environment.")
        if not report.package_resources:
            missing.append("The installed Sync_Box resources are incomplete.")
        if not report.python_runtime or not report.qt_runtime:
            missing.append("The packaged Python/Qt runtime is incomplete.")
        if missing:
            self._page("requirements", "REQUIREMENTS", "Setup cannot continue", "\n".join(missing), "Retry")
            self.logs.show()
            self.stage = "welcome"
            return
        if not report.box_cli.usable:
            self._show_box(report.box_cli.detail)
            return
        self._busy("Checking your Box connection…")
        self._run(self.controller.test_authentication, self._authentication_checked)

    def _show_box(self, detail: str = "Box CLI needs to be installed.") -> None:
        self._page(
            "box", "BOX", "BOX ACCOUNT",
            "Box CLI is required to connect Sync_Box to Box.", "Install Box CLI",
        )
        self.detail.setText(detail + "\n\nSource: github.com/box/boxcli (SHA-256 verified, installed for this user).")

    def _show_auth(self, detail: str = "Not connected") -> None:
        self._page(
            "auth", "CONNECT", "BOX ACCOUNT",
            "Connect Sync_Box to your Box account.", "Connect to Box",
        )
        self.detail.setText(detail)

    def _show_folder(self) -> None:
        self._page(
            "folder", "LOCAL FOLDER", "LOCAL FOLDER",
            "Choose where your complete offline copy of Box will be stored on this computer.",
            "Continue",
        )
        self.path_row.show()
        try:
            config = self.controller.requirements.check().config
            if config:
                self.path.setText(str(config.local_root))
        except Exception:
            pass

    def _choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Choose local Box folder", self.path.text(), QFileDialog.Option.ShowDirsOnly
        )
        if selected:
            self.path.setText(selected)

    def _save_folder(self) -> None:
        selected = Path(self.path.text()).expanduser()
        kind, detail = inspect_folder(selected)
        create = False
        if kind is FolderKind.MISSING:
            create = QMessageBox.question(
                self, "Create local folder?", f"{selected} does not exist. Create it?"
            ) == QMessageBox.StandardButton.Yes
            if not create:
                return
        try:
            self.controller.prepare_local_folder(selected, create=create)
        except (OSError, SetupError) as exc:
            self._error("Local folder is not ready", exc)
            return
        self._busy("Checking local files and Box…")
        self._run(self.controller.analyze, self._analyzed)

    def _analyzed(self, result, error) -> None:
        if error:
            self._error("Could not compare the local folder and Box", error, retry=self._analyze_again)
            return
        self.analysis = result
        if not result.identical and not result.local_empty:
            counts = result.differences
            self._page(
                "attention", "SETUP NEEDS ATTENTION", "Files differ",
                "This folder and your Box account contain differences that must be resolved before automatic synchronization can begin.",
                "Check Again",
            )
            self.detail.setText(
                f"{counts.get('review', 0):,} item(s) need review: "
                f"{counts.get('local_only', 0):,} local-only, {counts.get('box_only', 0):,} Box-only, "
                f"{counts.get('content_mismatch', 0):,} content mismatch. No files were changed."
            )
            self.primary.clicked.disconnect()
            self.primary.clicked.connect(self._analyze_again)
            self.logs.show()
            return
        if result.identical:
            message = (
                f"Box contains {result.box_files:,} files and {result.box_folders:,} folders.\n\n"
                "The local folder is identical. Sync_Box will verify both sides again before establishing the baseline."
            )
            button = "Establish Baseline"
        else:
            message = (
                f"Box contains {result.box_files:,} files and {result.box_folders:,} folders.\n\n"
                "The local folder is empty. Sync_Box will download and verify a complete local copy before automatic synchronization begins."
            )
            button = "Start Initial Sync"
        self._page("initial", "INITIAL SETUP", "INITIAL SYNC", message, button)

    def _analyze_again(self) -> None:
        try:
            self.primary.clicked.disconnect()
        except RuntimeError:
            pass
        self.primary.clicked.connect(self._primary_clicked)
        self._busy("Checking local files and Box…")
        self._run(self.controller.analyze, self._analyzed)

    def _show_automatic(self) -> None:
        self._page(
            "automatic", "AUTOMATIC SYNC", "AUTOMATIC SYNC",
            "Keep these files synchronized automatically every 30 minutes?",
            "Enable Automatic Sync",
        )

    def _show_ready(self) -> None:
        self._page(
            "ready", "READY", "READY",
            "✓ Box connected\n✓ Local files verified\n✓ Baseline established\n✓ Automatic sync enabled\n\nSync_Box is ready.",
            "Finish",
        )
        self.autostart.show()

    def _busy(self, text: str) -> None:
        self.detail.setText(text)
        self.primary.setEnabled(False)
        self.progress.show()

    def _run(self, operation, callback) -> None:
        task = _Task(operation, lambda result, error: self._task_done(task, callback, result, error))
        self._tasks.append(task)
        QThreadPool.globalInstance().start(task)

    def _task_done(self, task, callback, result, error) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        self.progress.hide()
        self.primary.setEnabled(True)
        callback(result, error)

    def _installed(self, result, error) -> None:
        if error:
            self._error("Box CLI installation failed", error, retry=lambda: self._show_box(str(error)))
            return
        self._show_auth("Box CLI installed and verified.")

    def _authentication_checked(self, result, error) -> None:
        if error:
            self.window.auth_state = AuthenticationState.REQUIRED
            self._show_auth("Not connected")
            return
        self._authenticated(result, None)

    def _authenticated(self, result, error) -> None:
        if error:
            self.window.auth_state = AuthenticationState.REQUIRED
            self._error("Authentication failed", error, retry=lambda: self._show_auth("Authentication failed. Try again."))
            return
        _user_id, name = result
        self.window.auth_state = AuthenticationState.CONNECTED
        self.window.account = name
        report = self.controller.requirements.check()
        if report.config is None or report.folder_kind not in {FolderKind.EMPTY, FolderKind.NONEMPTY}:
            self._show_folder()
        elif report.database.has_baseline:
            if report.systemd.timer_enabled and report.systemd.timer.running:
                self._show_ready()
            else:
                self._show_automatic()
        else:
            self._busy("Checking local files and Box…")
            self._run(self.controller.analyze, self._analyzed)

    def _baseline_finished(self, _result, error) -> None:
        if error:
            self._error("The baseline was not established", error, retry=self._analyze_again)
            return
        self._show_automatic()

    def _bootstrap_finished(self, _result, error) -> None:
        if error:
            self._error("Initial download was interrupted", error, retry=self._analyze_again)
            return
        self._show_automatic()

    def _timer_finished(self, result, error) -> None:
        if error or not result[0]:
            self._error("Automatic sync could not be enabled", error or RuntimeError(result[1]), retry=self._show_automatic)
            return
        self._show_ready()

    def _error(self, title: str, error: BaseException, retry=None) -> None:
        detail = safe_error_detail(error)
        log_failure(LOGGER, title, error)
        self.progress.hide()
        self.primary.setEnabled(True)
        self.detail.setText(f"{title}.\n{detail}")
        self.logs.show()
        if retry:
            self.secondary.show()
            try:
                self.secondary.clicked.disconnect()
            except RuntimeError:
                pass
            self.secondary.clicked.connect(retry)


class SettingsDialog(QDialog):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Sync_Box Settings")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.local = QLineEdit()
        self.local.setPlaceholderText("Choose an existing local folder")
        browse = QPushButton("…")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._browse)
        local_row = QHBoxLayout()
        local_row.addWidget(self.local, 1)
        local_row.addWidget(browse)
        form.addRow("Local Box folder", local_row)

        self.box_url = QLineEdit()
        self.box_url.setPlaceholderText("https://app.box.com/folder/…")
        form.addRow("Box folder URL", self.box_url)
        self.schedule = QComboBox()
        for minutes in INTERVALS:
            label = f"Every {minutes} minutes" if minutes < 60 else f"Every {minutes // 60} hour(s)"
            self.schedule.addItem(label, minutes)
        self.schedule.setCurrentIndex(INTERVALS.index(window.preferences.interval))
        form.addRow("Schedule", self.schedule)
        self.account = QLabel("Checking…")
        form.addRow("Box connection", self.account)
        layout.addLayout(form)

        self.timer = QCheckBox("Automatic synchronization enabled")
        self.timer.setChecked(window.snapshot.systemd.timer_enabled)
        ready_for_automatic_sync = (
            window.snapshot.database.has_baseline
            and window.snapshot.auth_state is AuthenticationState.CONNECTED
        )
        self.timer.setEnabled(
            window.snapshot.systemd.timer.installed
            and (window.snapshot.systemd.timer_enabled or ready_for_automatic_sync)
        )
        if window.snapshot.systemd.timer.installed and not self.timer.isEnabled():
            self.timer.setToolTip(
                "Authenticate Box and establish a verified baseline before enabling automatic synchronization."
            )
        layout.addWidget(self.timer)
        self.watch = QCheckBox("Sync automatically when the local Box folder changes")
        self.watch.setChecked(window.preferences.watch_folder)
        layout.addWidget(self.watch)
        self.login_sync = QCheckBox("Automatically sync at login")
        self.login_sync.setChecked(window.preferences.sync_at_login)
        layout.addWidget(self.login_sync)
        self.autostart = QCheckBox("Launch at login")
        self.autostart.setChecked(autostart_enabled())
        layout.addWidget(self.autostart)

        buttons = QHBoxLayout()
        self.logs = QPushButton("Open Logs")
        self.auth = QPushButton("Reconnect Box")
        self.save = QPushButton("Save")
        self.save.setProperty("primary", True)
        buttons.addWidget(self.logs)
        buttons.addStretch()
        buttons.addWidget(self.auth)
        buttons.addWidget(self.save)
        layout.addLayout(buttons)
        self.logs.clicked.connect(window.open_logs)
        self.auth.clicked.connect(window.authenticate)
        self.save.clicked.connect(self._save)

        config = window.snapshot.config
        if config:
            self.local.setText(str(config.local_root))
            self.local.setReadOnly(True)
            browse.setEnabled(False)
            self.box_url.hide()
            label = form.labelForField(self.box_url)
            if label:
                label.hide()
        self._update_account()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose local Box folder")
        if folder:
            self.local.setText(folder)

    def _update_account(self) -> None:
        if self.window.snapshot.auth_state is AuthenticationState.CONNECTED:
            self.account.setText(self.window.snapshot.account or "Connected")
        elif self.window.snapshot.auth_state is AuthenticationState.REQUIRED:
            self.account.setText("Authentication required")
        else:
            self.account.setText("Checking…")

    def _save(self) -> None:
        created_config = self.window.snapshot.config is None
        try:
            if created_config:
                create_initial_config(
                    Path(self.local.text()),
                    self.box_url.text(),
                    config_path=self.window.provider.config_path,
                )
            if self.timer.isEnabled() and self.timer.isChecked() != self.window.snapshot.systemd.timer_enabled:
                ok, message = self.window.provider.controller.set_timer_enabled(self.timer.isChecked())
                if not ok:
                    raise SetupError(message)
            preferences = Preferences(self.schedule.currentData(), self.watch.isChecked(), self.login_sync.isChecked())
            if preferences.interval != self.window.preferences.interval:
                set_sync_interval(preferences.interval, enabled=self.timer.isChecked())
            set_autostart(self.autostart.isChecked())
            save_preferences(preferences)
            self.window.preferences = preferences
            self.window.update_watcher()
        except (OSError, SetupError, SystemdUnitError) as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))
            return
        self.window.refresh_status()
        self.accept()
        if created_config:
            QTimer.singleShot(0, self.window.check_authentication)


class FolderModeDialog(QDialog):
    def __init__(self, settings: "FolderSettingsDialog", folder: str) -> None:
        super().__init__(settings)
        self.settings = settings
        self.folder = folder
        self.setWindowTitle("Edit folder direction")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        name = QLabel("All folders (default)" if folder == "." else folder)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setWordWrap(True)
        layout.addWidget(name)
        self.direction = QComboBox()
        self.direction.addItem("Two way (local ↔ Box)", "two-way")
        self.direction.addItem("One way (local → Box)", "one-way")
        self.direction.setCurrentIndex(self.direction.findData(folder_mode(folder, settings.config.folder_modes)))
        self.direction.setAccessibleName("Sync direction")
        layout.addWidget(self.direction)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        self.direction.currentIndexChanged.connect(self._describe)
        self._describe()
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _describe(self) -> None:
        self.explanation.setText(
            "Local files replace matching Box files, including files changed on Box. "
            "New local files are uploaded. Box-only files stay on Box; nothing is downloaded "
            "or deleted by this mode."
            if self.direction.currentData() == "one-way" else
            "Changes and deletions sync in both directions. Conflicting changes require review."
        )
        self.explanation.setText(self.explanation.text() +
            " Applies to subfolders unless they have their own setting. Takes effect on the next sync.")

    def _save(self) -> None:
        try:
            save_folder_mode(self.settings.window.provider.config_path,
                             self.folder, self.direction.currentData())
        except (OSError, ConfigError) as exc:
            QMessageBox.warning(self, "Could not save folder setting", str(exc))
            return
        self.settings.reload()
        self.settings.window.refresh_status()
        self.accept()


class FolderSettingsDialog(QDialog):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Folder Setting")
        self.resize(720, 460)
        layout = QVBoxLayout(self)
        explanation = QLabel("Choose a sync direction for each folder. Subfolders inherit their parent’s direction unless edited.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Folder", "Direction", "Edit"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 250)
        self.table.setColumnWidth(2, 50)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.reload()

    def reload(self) -> None:
        self.config = load_config(self.window.provider.config_path)
        folders = {".", *(path for path, _ in self.config.folder_modes)}
        # Include cloud folders from the verified inventory without a network request.
        if self.config.state_database.is_file():
            try:
                from contextlib import closing
                with closing(sqlite3.connect(f"file:{self.config.state_database}?mode=ro", uri=True)) as db:
                    folders.update(row[0] for row in db.execute(
                        "SELECT relative_path FROM baseline_items WHERE item_type='folder' "
                        "AND generation_id=(SELECT generation_id FROM current_baseline WHERE singleton=1)"))
            except sqlite3.Error:
                pass
        for parent, dirs, _files in os.walk(self.config.local_root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()
                and not is_excluded_path((Path(parent) / name).relative_to(self.config.local_root).as_posix(),
                                         self.config.excluded_paths, self.config.excluded_names)]
            folders.update((Path(parent) / name).relative_to(self.config.local_root).as_posix() for name in dirs)
        self.folders = sorted(folders)
        explicit = dict(self.config.folder_modes)
        self.table.setRowCount(len(self.folders))
        for row, folder in enumerate(self.folders):
            self.table.setItem(row, 0, QTableWidgetItem("All folders (default)" if folder == "." else folder))
            mode = folder_mode(folder, self.config.folder_modes)
            direction = "One way · local → Box" if mode == "one-way" else "Two way · local ↔ Box"
            if folder not in explicit and folder != ".":
                direction += " (inherited)"
            direction_item = QTableWidgetItem(direction)
            direction_item.setToolTip(direction)
            self.table.setItem(row, 1, direction_item)
            edit = QPushButton("✎")
            icon = QIcon.fromTheme("document-edit")
            if not icon.isNull():
                edit.setIcon(icon)
                edit.setText("")
            edit.setToolTip(f"Edit {folder}")
            edit.setAccessibleName(f"Edit direction for {folder}")
            edit.clicked.connect(lambda _checked=False, path=folder: self.edit_folder(path))
            self.table.setCellWidget(row, 2, edit)

    def edit_folder(self, folder: str) -> None:
        FolderModeDialog(self, folder).exec()


class ConflictDialog(QDialog):
    """Show detected conflicts and apply safe local-name corrections."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.conflicts = window.snapshot.database.conflicts
        self.setWindowTitle("Resolve sync conflict")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        layout.addWidget(_section("SYNC CONFLICT"))
        title = QLabel("Review and resolve a conflict")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        self.paths = QComboBox()
        for conflict in self.conflicts:
            self.paths.addItem(conflict.relative_path)
        self.paths.currentIndexChanged.connect(self._selection_changed)
        layout.addWidget(self.paths)
        self.reason = QLabel()
        self.reason.setWordWrap(True)
        self.reason.setObjectName("secondary")
        layout.addWidget(self.reason)
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)

        self._preview = None
        self._busy = False
        self._tasks = []
        self.preview_tabs = QTabWidget()
        self.preview_tabs.setMinimumHeight(240)
        self.preview_fields = {}
        for key, title in (("local_preview", "Local"), ("box_preview", "Box"), ("diff", "Difference")):
            field = QPlainTextEdit()
            field.setReadOnly(True)
            self.preview_tabs.addTab(field, title)
            self.preview_fields[key] = field
        layout.addWidget(self.preview_tabs)
        self.backups_button = QPushButton("Open Backups")
        self.backups_button.clicked.connect(self._open_backups)
        preview_actions = QHBoxLayout()
        self.refresh_preview_button = QPushButton("Refresh Preview")
        self.refresh_preview_button.clicked.connect(self._refresh_preview)
        preview_actions.addWidget(self.backups_button)
        preview_actions.addWidget(self.refresh_preview_button)
        layout.addLayout(preview_actions)
        actions = QHBoxLayout()
        self.keep_local_button = QPushButton("Keep Local")
        self.keep_box_button = QPushButton("Keep Box")
        self.keep_local_button.clicked.connect(lambda: self._apply_resolution("keep_local"))
        self.keep_box_button.clicked.connect(lambda: self._apply_resolution("keep_box"))
        self.open_button = QPushButton("Open Folder")
        self.custom_button = QPushButton("Choose Name…")
        self.suggested_button = QPushButton("Keep Both")
        self.suggested_button.setProperty("primary", True)
        close_button = QPushButton("Cancel")
        self.open_button.clicked.connect(self._open_parent)
        self.custom_button.clicked.connect(self._choose_name)
        self.suggested_button.clicked.connect(self._use_suggested_name)
        close_button.clicked.connect(self.reject)
        actions.addWidget(self.open_button)
        actions.addStretch()
        actions.addWidget(close_button)
        actions.addWidget(self.custom_button)
        actions.addWidget(self.keep_local_button)
        actions.addWidget(self.keep_box_button)
        actions.addWidget(self.suggested_button)
        layout.addLayout(actions)
        self._selection_changed()

    def _selected(self) -> ConflictDetail | None:
        index = self.paths.currentIndex()
        return self.conflicts[index] if 0 <= index < len(self.conflicts) else None

    def _selection_changed(self, _index: int = -1) -> None:
        conflict = self._selected()
        if conflict is None:
            self.reason.setText("No conflict details are available.")
            self.explanation.clear()
            self.custom_button.setEnabled(False)
            self.suggested_button.setEnabled(False)
            return
        self._preview = None
        self.keep_local_button.setEnabled(False)
        self.keep_box_button.setEnabled(False)
        self.backups_button.setEnabled(False)
        self.refresh_preview_button.setEnabled(False)
        self.reason.setText(conflict.reason)
        supported = conflict.reason == LOCAL_CASE_CONFLICT_REASON
        if supported:
            suggestion = _suggested_conflict_name(
                self.window.snapshot.local_folder, conflict.relative_path
            )
            self.explanation.setText(
                "Box ignores letter case in file names. Keep Both renames the "
                f"local file to {suggestion!r}; Choose Name lets you enter another name."
            )
        else:
            self.explanation.setText("Loading current versions and verified backups…")
        self.preview_tabs.setVisible(not supported)
        self.custom_button.setVisible(supported)
        self.keep_local_button.setVisible(not supported)
        self.keep_box_button.setVisible(not supported)
        self.backups_button.setVisible(not supported)
        self.refresh_preview_button.setVisible(not supported)
        self.custom_button.setEnabled(supported)
        self.suggested_button.setEnabled(supported)
        if not supported:
            self._load_preview(conflict.relative_path)

    def reject(self) -> None:
        if not self._busy:
            super().reject()

    def _run_task(self, operation, callback) -> None:
        self._busy = True
        self.paths.setEnabled(False)
        self.refresh_preview_button.setEnabled(False)
        self.window._resolution_busy = True
        task = _Task(operation, callback)
        self._tasks.append(task)
        QThreadPool.globalInstance().start(task)

    def _load_preview(self, path) -> None:
        from sync_box.file_resolution import preview_file_conflict
        self._run_task(lambda: preview_file_conflict(load_config(self.window.provider.config_path), path), self._preview_ready)

    def _preview_ready(self, result, error) -> None:
        self._busy = False
        self.window._resolution_busy = False
        self.paths.setEnabled(True)
        self.refresh_preview_button.setEnabled(True)
        if error:
            self.explanation.setText(safe_error_detail(error))
            for field in self.preview_fields.values():
                field.clear()
            return
        selected = self.paths.findText(result["path"])
        if selected >= 0 and selected != self.paths.currentIndex():
            self.paths.blockSignals(True)
            self.paths.setCurrentIndex(selected)
            self.paths.blockSignals(False)
            self.reason.setText("Resume the interrupted resolution for this file")
        self._preview = result
        for key, field in self.preview_fields.items():
            field.setPlainText(result[key])
        sizes = []
        for side in ("local", "box"):
            source = result["sources"].get(side)
            sizes.append(f"{side.title()}: {source['size']:,} bytes · SHA-1 {source['sha1']}" if source else f"{side.title()}: missing")
        description = "Keep Local uses the local version (or its deletion). Keep Box uses the Box version (or its deletion). Keep Both preserves every existing version."
        if result.get("copy_path"):
            description += f" Keep Both saves the local copy as: {result['copy_path']}."
        if result["box_path"] != result["path"]:
            description += f" Box moved this file to: {result['box_path']}."
        if result["resume"]:
            description = f"An interrupted resolution must resume its saved choice: {result['policy']}."
        self.explanation.setText("\n".join(sizes) + "\n" + description + "\nVerified backups: " + result["backup_directory"])
        for button, policy in ((self.keep_local_button, "keep_local"), (self.keep_box_button, "keep_box"),
                               (self.suggested_button, "keep_both")):
            button.setEnabled((not result["resume"] or result["policy"] == policy) and
                              (policy != "keep_both" or result["keep_both_available"]))
        self.backups_button.setEnabled(True)

    def _refresh_preview(self) -> None:
        conflict = self._selected()
        if conflict is None:
            return
        if self._preview and self._preview["resume"]:
            answer = QMessageBox.question(self, "Review the current versions?",
                "Stop the pending resolution and create a fresh preview? Completed file changes remain in place and every backup is retained.")
            if answer != QMessageBox.StandardButton.Yes:
                return
            from sync_box.file_resolution import cancel_file_resolution
            token = self._preview["token"]
            self._run_task(lambda: cancel_file_resolution(load_config(self.window.provider.config_path), token),
                           lambda result, error: self._cancel_finished(conflict.relative_path, error))
        else:
            self._selection_changed()

    def _cancel_finished(self, path, error) -> None:
        self._busy = False
        self.window._resolution_busy = False
        if error:
            self._preview_ready(None, error)
            return
        self._selection_changed()

    def _open_backups(self) -> None:
        if self._preview:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._preview["backup_directory"]))

    def _apply_resolution(self, policy: str) -> None:
        if self._preview is None:
            return
        label = {"keep_local": "Keep Local", "keep_box": "Keep Box", "keep_both": "Keep Both"}[policy]
        effect = ""
        if policy == "keep_local" and "local" not in self._preview["sources"]:
            effect = "This will delete the Box copy to keep the local deletion.\n\n"
        elif policy == "keep_box" and "box" not in self._preview["sources"]:
            effect = "This will delete the local copy to keep the Box deletion.\n\n"
        answer = QMessageBox.question(self, f"Apply {label}?",
            f"Apply {label} to {self._preview['path']}?\n\n" + effect +
            "The reviewed versions have verified backups. If either file changed since the preview, the action will stop.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        from sync_box.file_resolution import execute_file_resolution
        token = self._preview["token"]
        for button in (self.keep_local_button, self.keep_box_button, self.suggested_button):
            button.setEnabled(False)
        self.explanation.setText("Applying the saved choice and verifying both copies…")
        self._run_task(lambda: execute_file_resolution(load_config(self.window.provider.config_path), token, policy), self._resolution_finished)

    def _resolution_finished(self, result, error) -> None:
        self._busy = False
        self.window._resolution_busy = False
        if error:
            self.refresh_preview_button.setEnabled(True)
            self.explanation.setText(safe_error_detail(error) + "\nBackups are retained. Reopen Resolve Conflict to refresh or resume.")
            self.paths.setEnabled(True)
            return
        QMessageBox.information(self, "Conflict resolved", "The selected conflict is resolved. Backups are saved in:\n" + result["backup_directory"])
        self.accept()
        self.window.refresh_status()
        self.window.sync_now()

    def _open_parent(self) -> None:
        conflict = self._selected()
        root = self.window.snapshot.local_folder
        if conflict is None or root is None:
            return
        parent = root.joinpath(*PurePosixPath(conflict.relative_path).parts).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(parent)))

    def _use_suggested_name(self) -> None:
        if self._preview is not None:
            self._apply_resolution("keep_both")
            return
        conflict = self._selected()
        root = self.window.snapshot.local_folder
        if conflict is None or root is None:
            return
        name = _suggested_conflict_name(root, conflict.relative_path)
        answer = QMessageBox.question(
            self,
            "Keep both files?",
            f"Rename the local file to {name!r}?\n\nBoth files will then sync to Box.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply_name(conflict, name)

    def _choose_name(self) -> None:
        conflict = self._selected()
        root = self.window.snapshot.local_folder
        if conflict is None or root is None:
            return
        suggested = _suggested_conflict_name(root, conflict.relative_path)
        name, accepted = QInputDialog.getText(
            self, "Choose a local name", "New file name:", text=suggested
        )
        if accepted:
            self._apply_name(conflict, name)

    def _apply_name(self, conflict: ConflictDetail, name: str) -> None:
        root = self.window.snapshot.local_folder
        if root is None:
            return
        try:
            destination = _rename_conflict_file(root, conflict.relative_path, name)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "File was not renamed", str(exc))
            return
        QMessageBox.information(
            self,
            "Conflict corrected",
            f"Renamed the local file to {destination.name!r}. Synchronization will run now.",
        )
        self.accept()
        self.window.refresh_status()
        self.window.sync_now()


class MainWindow(QMainWindow):
    def __init__(self, provider: StatusProvider | None = None, *, tray_only: bool = False) -> None:
        super().__init__()
        self.provider = provider or StatusProvider()
        self.preferences = load_preferences()
        self._login_pending = "--login" in sys.argv and self.preferences.sync_at_login
        self._dirty = False
        self._resolution_busy = False
        self._history_rows = None
        self._history_task = None
        self._watch_error = None
        self.watcher = FolderWatcher(self)
        self.watcher.changed.connect(self._folder_changed)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self._automatic_sync)
        self.auth_state = AuthenticationState.UNKNOWN
        self.account: str | None = None
        self.box_space_used: int | None = None
        self.box_space_amount: int | None = None
        self.sync_requested = False
        self._request_refreshes = 0
        self._quitting = False
        self.auth_process: QProcess | None = None
        self.setup_wizard: SetupWizard | None = None
        self.snapshot = self.provider.read()
        self.setWindowTitle("Box Cloud Sync")
        self.setWindowIcon(QIcon(str(icon_path())))
        self.resize(850, 820)
        self.setMinimumSize(650, 650)
        self._build_window()
        self._build_tray()
        self.refresh_status()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_status)
        self.poll_timer.start(5000)
        self.auth_timer = QTimer(self)
        self.auth_timer.timeout.connect(self.check_authentication)
        self.auth_timer.start(60000)
        self.history_timer = QTimer(self)
        self.history_timer.timeout.connect(self.refresh_history)
        self.history_timer.start(10000)
        self.update_watcher()
        self.refresh_history()
        QApplication.instance().aboutToQuit.connect(self._shutdown)
        QTimer.singleShot(100, self.check_authentication if tray_only else self._start_application)

    def _start_application(self) -> None:
        if self.snapshot.config is not None and self.snapshot.database.has_baseline:
            self.check_authentication()
            return
        self.hide()
        self.setup_wizard = SetupWizard(self)
        self.setup_wizard.finished.connect(self._setup_closed)
        self.setup_wizard.show()

    def _setup_closed(self) -> None:
        if self.setup_wizard and self.setup_wizard.result() == QDialog.DialogCode.Accepted:
            self.update_watcher()
            self.refresh_history()
            self.show_window()

    def _build_window(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(9)

        header = QHBoxLayout()
        title = QLabel("Box Cloud Sync")
        title.setObjectName("appTitle")
        self.header_status = QLabel()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.header_status)
        layout.addLayout(header)
        layout.addSpacing(5)

        layout.addWidget(_section("SYNC STATUS"))
        self.main_status = QLabel()
        self.main_status.setObjectName("mainStatus")
        self.main_status.setTextFormat(Qt.TextFormat.PlainText)
        self.main_status.setWordWrap(True)
        self.detail = QLabel()
        self.detail.setObjectName("secondary")
        self.last_sync = QLabel()
        self.last_sync.setObjectName("secondary")
        self.next_sync = QLabel()
        self.next_sync.setObjectName("secondary")
        layout.addWidget(self.main_status)
        layout.addWidget(self.detail)
        layout.addWidget(self.last_sync)
        layout.addWidget(self.next_sync)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(5)
        layout.addWidget(self.progress)
        layout.addSpacing(8)

        self.conflict_panel = QWidget()
        conflict_layout = QVBoxLayout(self.conflict_panel)
        conflict_layout.setContentsMargins(0, 0, 0, 0)
        conflict_layout.setSpacing(6)
        conflict_layout.addWidget(_section("CONFLICT"))
        self.conflict_path = QLabel()
        self.conflict_path.setWordWrap(True)
        self.conflict_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.conflict_reason = QLabel()
        self.conflict_reason.setObjectName("secondary")
        self.conflict_reason.setWordWrap(True)
        conflict_actions = QHBoxLayout()
        self.resolve_button = QPushButton("Resolve Conflict…")
        self.resolve_button.setProperty("primary", True)
        self.resolve_button.clicked.connect(self.open_conflicts)
        conflict_actions.addWidget(self.resolve_button)
        conflict_actions.addStretch()
        conflict_layout.addWidget(self.conflict_path)
        conflict_layout.addWidget(self.conflict_reason)
        conflict_layout.addLayout(conflict_actions)
        layout.addWidget(self.conflict_panel)
        layout.addSpacing(8)

        layout.addWidget(_section("LOCAL FOLDER"))
        local_row = QHBoxLayout()
        self.folder = QLabel("Not selected")
        self.folder.setTextFormat(Qt.TextFormat.PlainText)
        self.folder.setWordWrap(True)
        self.folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_folder_button = QPushButton("…")
        self.open_folder_button.setFixedWidth(36)
        self.open_folder_button.clicked.connect(self.open_folder)
        local_row.addWidget(self.folder, 1)
        local_row.addWidget(self.open_folder_button)
        layout.addLayout(local_row)
        self.items = QLabel("No baseline")
        self.items.setObjectName("secondary")
        layout.addWidget(self.items)
        self.folder_settings_button = QPushButton("Folder Setting")
        self.folder_settings_button.clicked.connect(self.open_folder_settings)
        layout.addWidget(self.folder_settings_button)
        layout.addSpacing(8)

        layout.addWidget(_section("BOX"))
        self.connection = QLabel("Checking connection…")
        self.storage = QLabel("Checking storage usage…")
        self.storage.setObjectName("secondary")
        layout.addWidget(self.connection)
        layout.addWidget(self.storage)
        self.storage_bar = QProgressBar()
        self.storage_bar.setObjectName("storageBar")
        self.storage_bar.setAccessibleName("Box storage usage")
        self.storage_bar.setRange(0, 1000)
        self.storage_bar.setMinimumHeight(24)
        self.storage_bar.setTextVisible(True)
        layout.addWidget(self.storage_bar)
        self.watch_status = QLabel("Watching local changes in the background")
        self.watch_status.setObjectName("secondary")
        layout.addWidget(self.watch_status)
        layout.addSpacing(12)
        layout.addWidget(_section("TRANSFER ACTIVITY · LAST 48 HOURS"))
        self.history_summary = QLabel("Loading transfer history…")
        self.history_summary.setObjectName("secondary")
        layout.addWidget(self.history_summary)
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(["Time", "Direction", "File", "Status"])
        self.history.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history.setColumnWidth(0, 175)
        self.history.setColumnWidth(1, 100)
        self.history.verticalHeader().hide()
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history.setAlternatingRowColors(True)
        self.history.setMinimumHeight(180)
        self.history.setWordWrap(False)
        layout.addWidget(self.history, 1)

        actions = QHBoxLayout()
        self.sync_button = QPushButton("Sync Now")
        self.sync_button.setProperty("primary", True)
        self.settings_button = QPushButton("Settings")
        self.sync_button.clicked.connect(self.sync_now)
        self.settings_button.clicked.connect(self.open_settings)
        actions.addWidget(self.sync_button)
        actions.addStretch()
        background = QPushButton("Minimize to tray")
        background.clicked.connect(self.hide)
        actions.addWidget(background)
        actions.addWidget(self.settings_button)
        layout.addLayout(actions)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray_menu = QMenu()
        self.tray_state = QAction("Sync_Box")
        self.tray_state.setEnabled(False)
        self.tray_last = QAction("Last sync: Never")
        self.tray_last.setEnabled(False)
        self.open_action = self.tray_menu.addAction("Open Sync_Box")
        self.sync_action = self.tray_menu.addAction("Sync Now")
        self.folder_action = self.tray_menu.addAction("Open Box Folder")
        self.tray_menu.insertAction(self.open_action, self.tray_last)
        self.tray_menu.insertAction(self.tray_last, self.tray_state)
        self.tray_menu.addSeparator()
        self.quit_action = self.tray_menu.addAction("Quit")
        self.open_action.triggered.connect(self.show_window)
        self.sync_action.triggered.connect(self.sync_now)
        self.folder_action.triggered.connect(self.open_folder)
        self.quit_action.triggered.connect(self.quit_gui)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.setIcon(self.windowIcon())
        self.tray.show()

    def refresh_status(self) -> None:
        snapshot = self.provider.read(
            auth_state=self.auth_state,
            account=self.account,
            requested=self.sync_requested,
        )
        if self.sync_requested:
            self._request_refreshes += 1
            if snapshot.systemd.service.running:
                self._request_refreshes = 0
            elif self._request_refreshes >= 2:
                self.sync_requested = False
                self._request_refreshes = 0
                snapshot = self.provider.read(auth_state=self.auth_state, account=self.account)
        self.snapshot = snapshot
        if (snapshot.database.latest_summary or "").startswith("retry:") and not self._resolution_busy and snapshot.kind is not StatusKind.CONFLICT:
            self._dirty = True
            if not self.debounce.isActive():
                self.debounce.start(5000)
        color = COLORS[snapshot.kind]
        self.header_status.setText(f"<span style='color:{color}'>●</span> {snapshot.title}")
        symbol = "✓" if snapshot.kind is StatusKind.UP_TO_DATE else "●"
        self.main_status.setText(f"{symbol}  {snapshot.detail}")
        self.detail.setText(_secondary_detail(snapshot))
        self.last_sync.setText(f"Last sync: {_format_time(snapshot.database.last_completed)}")
        self.next_sync.setText(_next_sync_text(snapshot))
        self.progress.setVisible(snapshot.kind is StatusKind.SYNCING)
        database = snapshot.database
        scanning = (
            database.progress_phase
            and database.progress_phase != "Applying changes"
            and database.progress_total
        )
        if snapshot.kind is StatusKind.SYNCING and scanning:
            self.progress.setRange(0, database.progress_total)
            self.progress.setValue(database.progress_completed)
        elif snapshot.kind is StatusKind.SYNCING and database.operation_total:
            self.progress.setRange(0, database.operation_total)
            self.progress.setValue(database.operation_completed)
        else:
            self.progress.setRange(0, 0 if snapshot.kind is StatusKind.SYNCING else 100)
        conflicts = snapshot.database.conflicts
        self.conflict_panel.setVisible(snapshot.kind is StatusKind.CONFLICT)
        if conflicts:
            first = conflicts[0]
            suffix = f" (+{len(conflicts) - 1} more)" if len(conflicts) > 1 else ""
            self.conflict_path.setText(first.relative_path + suffix)
            self.conflict_reason.setText(first.reason)
            self.resolve_button.setEnabled(True)
        else:
            self.conflict_path.setText("Conflict details are unavailable.")
            self.conflict_reason.clear()
            self.resolve_button.setEnabled(False)
        self.folder.setText(str(snapshot.local_folder) if snapshot.local_folder else "Not selected")
        count = snapshot.database.item_count
        self.items.setText(f"{count:,} items" if snapshot.database.has_baseline else "No verified baseline")
        self.connection.setText(_connection_text(snapshot))
        self.storage.setText(
            _storage_text(snapshot.auth_state, self.box_space_used, self.box_space_amount)
        )
        used, total = self.box_space_used, self.box_space_amount
        storage_available = (
            snapshot.auth_state is AuthenticationState.CONNECTED
            and used is not None and total is not None and total > 0
        )
        self.storage_bar.setEnabled(storage_available)
        if storage_available:
            fraction = max(0.0, min(1.0, used / total))
            self.storage_bar.setValue(round(fraction * 1000))
            self.storage_bar.setFormat(f"{fraction:.1%} used · {1 - fraction:.1%} free")
            self.storage_bar.setToolTip(
                f"{_format_bytes(used)} used of {_format_bytes(total)} total · "
                f"{_format_bytes(max(0, total - used))} free"
            )
        else:
            self.storage_bar.setValue(0)
            self.storage_bar.setFormat("Storage usage unavailable")
            self.storage_bar.setToolTip("Connect to Box to check your storage quota.")
        usable = snapshot.config is not None and snapshot.database.has_baseline
        self.folder_settings_button.setEnabled(snapshot.config is not None)
        self.sync_button.setEnabled(usable and snapshot.kind is not StatusKind.SYNCING)
        self.sync_action.setEnabled(self.sync_button.isEnabled())
        self.open_folder_button.setEnabled(validated_local_folder(snapshot.config) is not None)
        self.folder_action.setEnabled(self.open_folder_button.isEnabled())
        self.tray_state.setText(f"Sync_Box — {snapshot.title}")
        self.tray_last.setText(f"Last sync: {_format_time(snapshot.database.last_completed)}")
        icon = _state_icon(color)
        self.tray.setIcon(icon)
        self.tray.setToolTip(f"Sync_Box — {snapshot.title}")

    def update_watcher(self) -> None:
        root = validated_local_folder(self.snapshot.config)
        try:
            if root and self.preferences.watch_folder:
                self.watcher.start(root)
            else:
                self.watcher.stop()
                self._dirty = False
            self._watch_error = None
        except OSError as exc:
            self._watch_error = str(exc)
            log_failure(LOGGER, "Could not watch Box folder", exc)
        if self._watch_error:
            self.watch_status.setText("Folder monitoring unavailable — periodic sync still runs. See logs.")
        else:
            self.watch_status.setText("Watching local changes • sync after files settle" if self.watcher.root else "Folder change sync is off")

    def _folder_changed(self) -> None:
        self._dirty = True
        self.debounce.start(5000)

    def _automatic_sync(self) -> None:
        if not (self._dirty or self._login_pending):
            return
        if not self.snapshot.config or not self.snapshot.database.has_baseline:
            self.debounce.start(30000)
            return
        if self.auth_state is not AuthenticationState.CONNECTED:
            self.debounce.start(30000)
            return
        if self._resolution_busy or self.sync_requested or self.snapshot.systemd.service.running:
            self.debounce.start(5000)
            return
        result = self.provider.controller.start_sync(self.snapshot.systemd)
        if result.accepted:
            self._dirty = False
            self._login_pending = False
            self.sync_requested = True
            self._request_refreshes = 0
        else:
            self.debounce.start(30000)
        self.refresh_status()

    def refresh_history(self) -> None:
        if self._history_task or not self.snapshot.config:
            return
        path = self.snapshot.config.state_database
        self._history_task = _Task(lambda: transfer_history(path), self._history_finished)
        QThreadPool.globalInstance().start(self._history_task)

    def _history_finished(self, rows, error) -> None:
        self._history_task = None
        if error:
            self.history_summary.setText("Transfer history temporarily unavailable")
            return
        completed = [row for row in rows if row[3] == "completed"]
        uploads = sum(row[1].startswith("upload") for row in completed)
        self.history_summary.setText(f"↑ {uploads:,} uploads   ↓ {len(completed) - uploads:,} downloads" if rows else "No uploads or downloads in the last 48 hours")
        if rows == self._history_rows:
            return
        self._history_rows = rows
        position = self.history.verticalScrollBar().value()
        self.history.setRowCount(len(rows))
        for index, (time, action, path, status) in enumerate(rows):
            parsed = datetime.fromisoformat(time.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                from datetime import timezone
                parsed = parsed.replace(tzinfo=timezone.utc)
            values = (parsed.astimezone().strftime("%b %d, %I:%M %p"),
                      "↑ Upload" if action.startswith("upload") else "↓ Download", path, status.capitalize())
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.history.setItem(index, column, item)
        self.history.verticalScrollBar().setValue(position)

    def sync_now(self) -> None:
        result = self.provider.controller.start_sync(self.snapshot.systemd)
        if result.already_running:
            self.tray.showMessage("Sync_Box", result.message, QSystemTrayIcon.MessageIcon.Information, 3000)
        elif not result.accepted:
            QMessageBox.warning(self, "Synchronization not started", result.message)
        else:
            self.sync_requested = True
            self._request_refreshes = 0
        self.refresh_status()

    def check_authentication(self) -> None:
        if self.snapshot.config is None or self.auth_process is not None:
            return
        self.auth_state = AuthenticationState.CHECKING
        command = cli_command()
        process = QProcess(self)
        self.auth_process = process
        process.finished.connect(self._authentication_finished)
        process.errorOccurred.connect(lambda _error: self._authentication_finished(1, QProcess.ExitStatus.CrashExit) if self.auth_process is process else None)
        QTimer.singleShot(45000, process, lambda: process.kill() if process.state() != QProcess.ProcessState.NotRunning else None)
        process.start(command[0], [
            *command[1:], "--config", str(self.provider.config_path),
            "auth", "test", "--json",
        ])

    def authenticate(self) -> None:
        if self.snapshot.config is None or self.auth_process is not None:
            return
        command = cli_command()
        process = QProcess(self)
        self.auth_process = process
        process.finished.connect(self._login_finished)
        process.start(command[0], [*command[1:], "--config", str(self.provider.config_path), "auth", "login", "--reauthorize"])

    def _login_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.auth_process = None
        if exit_code == 0:
            self.check_authentication()
            return
        self.auth_state = AuthenticationState.REQUIRED
        self.account = None
        self.box_space_used = None
        self.box_space_amount = None
        self.refresh_status()

    def _authentication_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        process = self.auth_process
        self.auth_process = None
        if process is None:
            return
        output = bytes(process.readAllStandardOutput()).decode(errors="replace").strip()
        process.deleteLater()
        if exit_code == 0:
            account = _parse_account_status(output)
            if account is None:
                self.auth_state = AuthenticationState.REQUIRED
                self.account = None
                self.box_space_used = None
                self.box_space_amount = None
            else:
                self.auth_state = AuthenticationState.CONNECTED
                self.account, self.box_space_used, self.box_space_amount = account
        else:
            self.auth_state = AuthenticationState.REQUIRED
            self.account = None
            self.box_space_used = None
            self.box_space_amount = None
        self.refresh_status()
        if self._login_pending or self._dirty:
            self.debounce.start(1000)

    def open_folder(self) -> None:
        folder = validated_local_folder(self.snapshot.config)
        if folder is None:
            QMessageBox.information(self, "Folder unavailable", "The local Box folder is unavailable.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_logs(self) -> None:
        config = self.snapshot.config
        log_file = config.log_file if config else default_state_directory() / "sync-box.log"
        path = log_file if log_file.exists() else log_file.parent
        if not path.exists():
            QMessageBox.information(self, "Logs unavailable", "No log has been created yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_folder_settings(self) -> None:
        try:
            FolderSettingsDialog(self).exec()
        except (OSError, ConfigError) as exc:
            QMessageBox.warning(self, "Folder settings unavailable", str(exc))

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def open_conflicts(self) -> None:
        if self.snapshot.database.conflicts:
            ConflictDialog(self).exec()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _shutdown(self) -> None:
        self.watcher.stop()
        if self.auth_process is not None:
            process = self.auth_process
            self.auth_process = None
            process.finished.disconnect()
            process.errorOccurred.disconnect()
            process.kill()
            process.waitForFinished(1000)

    def quit_gui(self) -> None:
        self._quitting = True
        self.tray.hide()
        QApplication.instance().quit()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
        else:
            event.ignore()
            self.hide()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self.show_window()


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    return label


def _suggested_conflict_name(root: Path | None, relative_path: str) -> str:
    original = PurePosixPath(relative_path).name
    path = PurePosixPath(original)
    suffix = path.suffix if not original.startswith(".") else ""
    stem = original[:-len(suffix)] if suffix else original
    parent = (
        root.joinpath(*PurePosixPath(relative_path).parts).parent
        if root is not None
        else None
    )
    occupied = {
        entry.name.casefold() for entry in os.scandir(parent)
    } if parent is not None and parent.is_dir() else set()
    for number in range(1, 10_000):
        marker = " (local copy)" if number == 1 else f" (local copy {number})"
        candidate = f"{stem}{marker}{suffix}"
        if candidate.casefold() not in occupied:
            return candidate
    raise ValueError("Could not find an available conflict-copy name")


def _rename_conflict_file(root: Path, relative_path: str, new_name: str) -> Path:
    if (
        not new_name
        or new_name in {".", ".."}
        or "/" in new_name
        or "\x00" in new_name
        or len(new_name.encode("utf-8")) > 255
    ):
        raise ValueError("Enter one valid file name of at most 255 bytes.")
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.name == "":
        raise ValueError("The conflict path is unsafe.")
    resolved_root = root.resolve(strict=True)
    source = resolved_root.joinpath(*relative.parts)
    parent = source.parent.resolve(strict=True)
    if parent != resolved_root and resolved_root not in parent.parents:
        raise ValueError("The conflict path leaves the synchronized folder.")
    value = source.lstat()
    if not stat.S_ISREG(value.st_mode) or source.is_symlink():
        raise ValueError("The conflicted item is not a regular local file.")
    occupied = {
        entry.name.casefold() for entry in os.scandir(parent)
        if entry.name != source.name
    }
    if new_name.casefold() in occupied or new_name == source.name:
        raise ValueError("That name is already in use in this folder.")
    destination = parent / new_name
    _rename_noreplace(source, destination)
    _fsync_directory(parent)
    return destination


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "Never"
    return value.astimezone().strftime("%-I:%M %p")


def _next_sync_text(snapshot: StatusSnapshot) -> str:
    if not snapshot.systemd.timer_enabled:
        return "Next sync: Automatic synchronization disabled"
    value = snapshot.systemd.timer.next_elapse
    if not value:
        return "Next sync: Scheduled by systemd"
    return f"Next sync: {_format_systemd_time(value)}"


def _format_systemd_time(value: str) -> str:
    parts = value.split()
    for index in range(len(parts) - 1):
        try:
            parsed = datetime.strptime(
                f"{parts[index]} {parts[index + 1]}", "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            continue
        return parsed.strftime("%B %-d, %Y, %-I:%M %p")
    return value


def _secondary_detail(snapshot: StatusSnapshot) -> str:
    if snapshot.kind is StatusKind.UP_TO_DATE and not snapshot.systemd.timer_enabled:
        return "Automatic synchronization is disabled"
    return "The systemd user timer runs independently of this window."


def _connection_text(snapshot: StatusSnapshot) -> str:
    if snapshot.auth_state is AuthenticationState.CONNECTED:
        return f"Connected as {snapshot.account or 'Box user'}"
    if snapshot.auth_state is AuthenticationState.REQUIRED:
        return "Box authentication required"
    if snapshot.auth_state is AuthenticationState.CHECKING:
        return "Checking Box connection…"
    return "Box connection has not been checked"


def _parse_account_status(output: str) -> tuple[str, int | None, int | None] | None:
    try:
        value = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return None
    used = _json_byte_count(value.get("space_used"))
    amount = _json_byte_count(value.get("space_amount"))
    return value["name"], used, amount


def _json_byte_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _storage_text(
    auth_state: AuthenticationState,
    used: int | None,
    amount: int | None,
) -> str:
    if auth_state is AuthenticationState.CHECKING:
        return "Checking storage usage…"
    if auth_state is not AuthenticationState.CONNECTED:
        return "Storage usage unavailable"
    if used is None or amount is None:
        return "Storage usage unavailable"
    free = max(amount - used, 0)
    return f"{_format_bytes(used)} used · {_format_bytes(free)} free"


def _format_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    unit = units[0]
    for unit in units:
        if size < 1000 or unit == units[-1]:
            break
        size /= 1000
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f}".rstrip("0").rstrip(".") + f" {unit}"


def _state_icon(color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#25292e"))
    painter.drawRoundedRect(2, 2, 28, 28, 7, 7)
    painter.setBrush(QColor(color))
    painter.drawEllipse(9, 9, 14, 14)
    painter.end()
    return QIcon(pixmap)


STYLESHEET = """
QWidget { background: #171e29; color: #e5e8eb; font-size: 13px; }
QMainWindow, QDialog { background: #171e29; }
QLabel#appTitle { font-size: 26px; font-weight: 700; }
QLabel#mainStatus { font-size: 14px; font-weight: 600; }
QLabel#secondary { color: #9299a1; }
QLabel#section { color: #858d96; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QPushButton, QLineEdit { background: #2a2e34; border: 1px solid #3b4149; border-radius: 8px; padding: 6px 11px; }
QPushButton:hover { border-color: #59616b; background: #30353c; }
QPushButton:disabled { color: #737981; background: #262a2f; }
QPushButton[primary="true"] { background: #327cad; border-color: #3e90c7; color: white; }
QProgressBar { background: #2a2e34; border: 0; border-radius: 2px; }
QProgressBar::chunk { background: #4aa3df; border-radius: 2px; }
QProgressBar#storageBar { background: #34465d; border: 1px solid #44556d; border-radius: 6px; text-align: center; color: white; }
QProgressBar#storageBar::chunk { background: #327cad; border-radius: 5px; }
QTableWidget { background: #1e2938; alternate-background-color: #222f40; border: 1px solid #34465d; border-radius: 10px; gridline-color: #2e3c50; selection-background-color: #285479; }
QHeaderView::section { background: #253349; color: #b6cce3; padding: 10px; border: none; }
QComboBox { padding: 7px; border: 1px solid #44556d; border-radius: 6px; }
QCheckBox { spacing: 10px; padding: 5px; }
QMenu { background: #292d33; border: 1px solid #414750; padding: 4px; }
QMenu::item { padding: 6px 24px 6px 10px; border-radius: 4px; }
QMenu::item:selected { background: #38414a; }
"""


def main() -> int:
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    tray_only = "--tray-only" in sys.argv[1:]
    application = QApplication([arg for arg in sys.argv if arg != "--tray-only"])
    server_name = f"sync-box-desktop-{os.getuid()}"
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(500):
        socket.write(b"tray" if tray_only else b"show")
        socket.waitForBytesWritten(500)
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer(application)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(server_name):
        return 1
    application.setApplicationName("Sync_Box")
    application.setDesktopFileName("sync-box")
    if os.geteuid() == 0:
        QMessageBox.critical(
            None, "Sync_Box cannot run as root",
            "Launch Sync_Box as your normal desktop user so its configuration and files remain user-owned.",
        )
        return 1
    log_file = default_state_directory() / "sync-box.log"
    config_error = None
    try:
        log_file = load_config(default_config_path()).log_file
    except ConfigError as exc:
        config_error = exc
    configure_logging(log_file)
    if config_error is not None:
        log_failure(LOGGER, "Could not load the configured log location", config_error)
    sys.excepthook = lambda _kind, error, _traceback: log_failure(
        LOGGER, "Unhandled GUI failure", error
    )
    application.setQuitOnLastWindowClosed(False)
    application.setStyleSheet(STYLESHEET)
    window = MainWindow(tray_only=tray_only)
    def received():
        connection = server.nextPendingConnection()
        def ready():
            if bytes(connection.readAll()) == b"show":
                window.show_window()
            connection.disconnectFromServer()
            connection.deleteLater()
        connection.readyRead.connect(ready)
        if connection.bytesAvailable():
            ready()
    server.newConnection.connect(received)
    if not tray_only:
        window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
