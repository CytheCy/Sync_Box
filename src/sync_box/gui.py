"""Compact PySide6 tray interface for the existing systemd sync service."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QColor, QCloseEvent, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar,
    QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from sync_box.app_status import (
    AuthenticationState, StatusKind, StatusProvider, StatusSnapshot,
    validated_local_folder,
)
from sync_box.autostart import autostart_enabled, set_autostart
from sync_box.resources import cli_command, icon_path
from sync_box.setup_config import SetupError, create_initial_config


COLORS = {
    StatusKind.UP_TO_DATE: "#4aa3df",
    StatusKind.SYNCING: "#4aa3df",
    StatusKind.ATTENTION: "#d6a84b",
    StatusKind.CONFLICT: "#d68045",
    StatusKind.ERROR: "#d65c5c",
    StatusKind.NOT_CONFIGURED: "#8b929a",
    StatusKind.AUTH_REQUIRED: "#d6a84b",
}


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
        self.schedule = QLabel("Every 30 minutes with a stable delay of up to 2 minutes")
        self.schedule.setWordWrap(True)
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
        self.autostart = QCheckBox("Start the tray application when I sign in")
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
            set_autostart(self.autostart.isChecked())
        except (OSError, SetupError) as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))
            return
        self.window.refresh_status()
        self.accept()
        if created_config:
            QTimer.singleShot(0, self.window.check_authentication)


class MainWindow(QMainWindow):
    def __init__(self, provider: StatusProvider | None = None) -> None:
        super().__init__()
        self.provider = provider or StatusProvider()
        self.auth_state = AuthenticationState.UNKNOWN
        self.account: str | None = None
        self.sync_requested = False
        self._request_refreshes = 0
        self._quitting = False
        self.auth_process: QProcess | None = None
        self.snapshot = self.provider.read()
        self.setWindowTitle("Sync_Box")
        self.setWindowIcon(QIcon(str(icon_path())))
        self.resize(500, 400)
        self.setMinimumSize(470, 370)
        self._build_window()
        self._build_tray()
        self.refresh_status()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_status)
        self.poll_timer.start(3000)
        QTimer.singleShot(100, self.check_authentication)

    def _build_window(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(9)

        header = QHBoxLayout()
        title = QLabel("Sync_Box")
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

        layout.addWidget(_section("LOCAL FOLDER"))
        local_row = QHBoxLayout()
        self.folder = QLabel("Not selected")
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
        layout.addSpacing(8)

        layout.addWidget(_section("BOX"))
        self.connection = QLabel("Checking connection…")
        layout.addWidget(self.connection)
        layout.addStretch()

        actions = QHBoxLayout()
        self.sync_button = QPushButton("Sync Now")
        self.sync_button.setProperty("primary", True)
        self.settings_button = QPushButton("Settings")
        self.sync_button.clicked.connect(self.sync_now)
        self.settings_button.clicked.connect(self.open_settings)
        actions.addWidget(self.sync_button)
        actions.addStretch()
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
        color = COLORS[snapshot.kind]
        self.header_status.setText(f"<span style='color:{color}'>●</span> {snapshot.title}")
        symbol = "✓" if snapshot.kind is StatusKind.UP_TO_DATE else "●"
        self.main_status.setText(f"{symbol}  {snapshot.detail}")
        self.detail.setText(_secondary_detail(snapshot))
        self.last_sync.setText(f"Last sync: {_format_time(snapshot.database.last_completed)}")
        self.next_sync.setText(_next_sync_text(snapshot))
        self.progress.setVisible(snapshot.kind is StatusKind.SYNCING)
        self.progress.setRange(0, 0 if snapshot.kind is StatusKind.SYNCING else 100)
        self.folder.setText(str(snapshot.local_folder) if snapshot.local_folder else "Not selected")
        count = snapshot.database.item_count
        self.items.setText(f"{count:,} items" if snapshot.database.has_baseline else "No verified baseline")
        self.connection.setText(_connection_text(snapshot))
        usable = snapshot.config is not None and snapshot.database.has_baseline
        self.sync_button.setEnabled(usable and snapshot.kind is not StatusKind.SYNCING)
        self.sync_action.setEnabled(self.sync_button.isEnabled())
        self.open_folder_button.setEnabled(validated_local_folder(snapshot.config) is not None)
        self.folder_action.setEnabled(self.open_folder_button.isEnabled())
        self.tray_state.setText(f"Sync_Box — {snapshot.title}")
        self.tray_last.setText(f"Last sync: {_format_time(snapshot.database.last_completed)}")
        icon = _state_icon(color)
        self.tray.setIcon(icon)
        self.tray.setToolTip(f"Sync_Box — {snapshot.title}")

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
        process.start(command[0], [*command[1:], "--config", str(self.provider.config_path), "auth", "test"])

    def authenticate(self) -> None:
        if self.snapshot.config is None or self.auth_process is not None:
            return
        command = cli_command()
        process = QProcess(self)
        self.auth_process = process
        process.finished.connect(self._authentication_finished)
        process.start(command[0], [*command[1:], "--config", str(self.provider.config_path), "auth", "login", "--reauthorize"])

    def _authentication_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        process = self.auth_process
        self.auth_process = None
        if process is None:
            return
        output = bytes(process.readAllStandardOutput()).decode(errors="replace").strip()
        if exit_code == 0:
            self.auth_state = AuthenticationState.CONNECTED
            marker = "Authenticated to Box as "
            self.account = output.split(marker, 1)[1].rsplit(" (user ID", 1)[0] if marker in output else "Connected"
        else:
            self.auth_state = AuthenticationState.REQUIRED
            self.account = None
        self.refresh_status()

    def open_folder(self) -> None:
        folder = validated_local_folder(self.snapshot.config)
        if folder is None:
            QMessageBox.information(self, "Folder unavailable", "The local Box folder is unavailable.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_logs(self) -> None:
        config = self.snapshot.config
        if config is None:
            QMessageBox.information(self, "Logs unavailable", "Sync_Box is not configured yet.")
            return
        path = config.log_file if config.log_file.exists() else config.log_file.parent
        if not path.exists():
            QMessageBox.information(self, "Logs unavailable", "No log has been created yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_gui(self) -> None:
        self._quitting = True
        self.tray.hide()
        QApplication.instance().quit()

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


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "Never"
    return value.astimezone().strftime("%-I:%M %p")


def _next_sync_text(snapshot: StatusSnapshot) -> str:
    if not snapshot.systemd.timer_enabled:
        return "Next sync: Automatic synchronization disabled"
    value = snapshot.systemd.timer.next_elapse
    return f"Next sync: {value}" if value else "Next sync: Scheduled by systemd"


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
QWidget { background: #202328; color: #e5e8eb; font-size: 13px; }
QMainWindow, QDialog { background: #202328; }
QLabel#appTitle { font-size: 16px; font-weight: 600; }
QLabel#mainStatus { font-size: 14px; font-weight: 600; }
QLabel#secondary { color: #9299a1; }
QLabel#section { color: #858d96; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QPushButton, QLineEdit { background: #2a2e34; border: 1px solid #3b4149; border-radius: 8px; padding: 6px 11px; }
QPushButton:hover { border-color: #59616b; background: #30353c; }
QPushButton:disabled { color: #737981; background: #262a2f; }
QPushButton[primary="true"] { background: #327cad; border-color: #3e90c7; color: white; }
QProgressBar { background: #2a2e34; border: 0; border-radius: 2px; }
QProgressBar::chunk { background: #4aa3df; border-radius: 2px; }
QMenu { background: #292d33; border: 1px solid #414750; padding: 4px; }
QMenu::item { padding: 6px 24px 6px 10px; border-radius: 4px; }
QMenu::item:selected { background: #38414a; }
"""


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("Sync_Box")
    application.setDesktopFileName("sync-box")
    application.setQuitOnLastWindowClosed(False)
    application.setStyle("Fusion")
    application.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
