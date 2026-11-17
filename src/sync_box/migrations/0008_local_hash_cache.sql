BEGIN IMMEDIATE;

CREATE TABLE local_hash_cache (
    local_root TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    mode INTEGER NOT NULL,
    sha1 TEXT NOT NULL,
    verified_at_ns INTEGER NOT NULL,
    PRIMARY KEY (local_root, relative_path)
) STRICT;

PRAGMA user_version = 8;
COMMIT;
