"""Recursive Linux notification observer; never hashes or polls file contents."""
from pathlib import Path
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from PySide6.QtCore import QObject, Signal


class FolderWatcher(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.observer = None
        self.root = None

    def start(self, root: Path) -> None:
        if self.root == root and self.observer and self.observer.is_alive():
            return
        self.stop()
        owner = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                # Reads performed by inventories must not trigger more syncs.
                if event.event_type in {'created', 'modified', 'deleted', 'moved', 'closed'}:
                    owner.changed.emit()

        observer = Observer()
        observer.schedule(Handler(), str(root), recursive=True)
        try:
            observer.start()
        except BaseException:
            observer.stop()
            raise
        self.observer = observer
        self.root = root

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
        self.observer = None
        self.root = None
