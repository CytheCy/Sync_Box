"""Disposable local SHA-1 cache, committed only after a completed inventory."""

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
from time import time_ns

from sync_box.database import initialize_database


HASH_MAX_AGE_NS = 24 * 60 * 60 * 1_000_000_000


def file_fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode)


@dataclass(frozen=True)
class CachedHash:
    fingerprint: tuple[int, ...]
    sha1: str
    verified_at_ns: int


class LocalHashCache:
    def __init__(self, database: Path, root: Path, *, force_hashes: bool = False):
        self.database = database
        self.root = str(root)
        self.force_hashes = force_hashes
        self.pending: dict[str, CachedHash] = {}
        self.hits = 0
        self.hashed = 0
        initialize_database(database)
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                "SELECT relative_path, device, inode, size, mtime_ns, ctime_ns, mode, "
                "sha1, verified_at_ns FROM local_hash_cache WHERE local_root=?",
                (self.root,),
            ).fetchall()
        self.entries = {
            row[0]: CachedHash(tuple(row[1:7]), row[7], row[8]) for row in rows
        }

    def lookup(self, path: str, info: os.stat_result) -> CachedHash | None:
        entry = self.entries.get(path)
        if self.force_hashes or entry is None:
            return None
        if (entry.fingerprint != file_fingerprint(info)
                or not 0 <= time_ns() - entry.verified_at_ns < HASH_MAX_AGE_NS
                or re.fullmatch(r"[0-9a-f]{40}", entry.sha1) is None):
            return None
        return entry

    def record(self, path: str, info: os.stat_result, sha1: str,
               cached: CachedHash | None) -> None:
        if cached is None:
            self.hashed += 1
            self.pending[path] = CachedHash(file_fingerprint(info), sha1, time_ns())
        else:
            self.hits += 1
            # A cache hit must never extend the full-verification deadline.
            self.pending[path] = cached

    def save(self) -> None:
        """Atomically replace this root's entries, pruning absent/excluded files."""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM local_hash_cache WHERE local_root=?", (self.root,))
            connection.executemany(
                "INSERT INTO local_hash_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((self.root, path, *entry.fingerprint, entry.sha1, entry.verified_at_ns)
                 for path, entry in self.pending.items()),
            )
