# Sync Box

`sync-box` is a conservative two-way synchronizer for a normal Fedora folder and
a Box folder. It uses the official Box CLI login, verified SHA-1 inventories,
an external SQLite baseline, and short-lived downscoped tokens. Inventories and
dry runs use read-only access. An executing sync obtains a separate content
read/write token only for that run.

## Install on Fedora

Python 3.11 or newer is required. From this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

This installs the official Box Python SDK v10. The official Box CLI 4.6 or newer
is also required. On Fedora, install Node.js 22 or newer and the CLI:

```bash
npm install --global @box/cli
box --version
```

Run the tests with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## External files

Configuration, SQLite state, and logs must remain outside this Git checkout.
State and logs must also remain outside the synchronized folder. The program
validates these boundaries. Box CLI owns its credentials and tokens; `sync-box`
does not store them.

Recommended locations are:

```text
~/.config/sync-box/config.toml
~/.local/state/sync-box/state.sqlite3
~/.local/state/sync-box/sync-box.log
```

Create the external directories and copy the example safely:

```bash
install -d -m 700 ~/.config/sync-box ~/.local/state/sync-box
install -m 600 config.example.toml ~/.config/sync-box/config.toml
```

Open `~/.config/sync-box/config.toml` in your preferred editor and replace every
example path. Set `local.root` to the folder that will hold full offline copies.
Set `box.folder_id` to the number at the end of the Box folder URL, or `"0"` for
the account root. No secret belongs in `config.toml`.

Configure relative paths that must never enter an inventory or comparison:

```toml
[sync]
exclude = [".Trash-1000"]
exclude_names = ["Thumbs.db"]
```

Each `exclude` entry omits that path and everything beneath it on both the local
and Box sides. Excluded folders are not traversed. Each `exclude_names` entry
omits that filename wherever it appears in either tree.

## Authenticate from Fedora KDE

Authorize with Box's preconfigured official CLI application. No Developer Console
application, client secret, or custom redirect URI is needed:

```bash
.venv/bin/sync-box auth login
```

The command creates and selects a Box CLI environment named `sync-box`, opens the
browser, and lets the Box CLI manage the callback and durable credentials. On
Linux the CLI uses Secret Service when available and otherwise falls back to
files under `~/.box`. For a headless login, add `--code`.

If authorization later expires, reauthorize the same environment:

```bash
.venv/bin/sync-box auth login --reauthorize
```

Before each authenticated operation, `sync-box` asks the CLI to exchange its
credential for a short-lived token with the `root_readonly` and `item_download`
scopes. The first permits metadata reads and the second permits file-content
downloads. Only that downscoped token is given to the Python SDK, it is kept in
memory, and it cannot be refreshed or used to write Box content.

Test the connection with a read-only current-user request:

```bash
.venv/bin/sync-box auth test
```

## Read-only inventories

Scan the local tree without following symbolic links:

```bash
.venv/bin/sync-box inventory local
.venv/bin/sync-box inventory local --summary-only
.venv/bin/sync-box inventory local --json
```

Scan the configured Box folder recursively with metadata-only API requests:

```bash
.venv/bin/sync-box inventory box
.venv/bin/sync-box inventory box --summary-only
.venv/bin/sync-box inventory box --json
```

Add `--save` to either inventory command to store the metadata snapshot in the
external SQLite database. Without `--save`, inventories do not write state or log
files. A Box scan may cause the official CLI to refresh its credential in the
system credential store.

Paths use relative POSIX separators and Unicode NFC normalization. Absolute
paths, parent traversal, invalid segments, and normalization collisions cause the
scan to stop. Local metadata includes type, size, UTC modification time,
nanosecond timestamp, device, inode, and mode. Box metadata includes item ID,
type, size, modification time, ETag, SHA-1, sequence ID, and file-version ID when
Box provides them. Symlinks and unsupported local or Box item types are recorded
but never traversed.

## Database and logs

Initialize a new database or migrate an older database to schema v3:

```bash
.venv/bin/sync-box init
```

The migrations preserve existing data and add paired baseline generations and
an operation journal. Database and log files use mode `0600`; logs rotate at 5
MiB and keep three backups.

## Baseline and two-way synchronization

Before a baseline exists, compare live local file hashes with Box SHA-1 metadata:

```bash
.venv/bin/sync-box run --dry-run --summary-only
```

The dry run does not save inventories, create logs, or change either tree.
Matching files and folders are counted but omitted from the table. One-sided
items, type mismatches, unsupported types, unavailable hashes, and differing
content are reported for review. Because no common baseline exists yet, it does
not choose upload, download, or delete directions. Omit `--summary-only` for the
full review table, or add `--json` for structured review output.

Once both trees are expected to match, create the baseline:

```bash
.venv/bin/sync-box baseline create
.venv/bin/sync-box baseline status
```

Creation performs fresh local and Box inventories. It refuses the database
write unless every non-excluded path and type matches and every file has a
matching SHA-1. The atomic generation records local stat identity and Box IDs,
versions, ETags, hashes, sizes, and timestamps.

With a baseline, `run --dry-run` prints exact upload, download, delete, move,
and conflict actions. Box IDs track remote moves; local device/inode identity is
used only as a conservative move hint. Concurrent edits, occupied destinations,
replaced Box identities, unsupported items, and ambiguities become conflicts.
The dry run never obtains a write-capable token.

After reviewing a conflict-free plan, execution is explicit:

```bash
.venv/bin/sync-box run
```

Execution rejects any plan containing a conflict before changing either tree.
It journals each operation, revalidates files before upload, uses Box ETag
preconditions, verifies downloads in temporary files, fsyncs them, and publishes
them atomically. It never follows symlinks or accepts paths outside the root.
Token-expiration retries are bounded. An interrupted run keeps the prior
baseline, so a fresh inventory can safely plan the remaining work. A new
baseline is committed only after all actions complete and another pair of fresh
inventories proves that the trees match.

## Keep-both conflict resolution engine

The shared engine exposes structured keep-both planning and execution through
`sync_box.conflict_resolution`. The first policy keeps the Box version at the
original path and preserves the divergent local version under a deterministic
name containing its SHA-1 prefix. Plans bind the baseline generation, local
fingerprint, Box item/version/ETag, and both paths before any mutation.

Resolution execution uses a durable operation journal. It uploads and verifies
the conflict copy without ambiguous mutation retries, publishes the local copy
with atomic no-clobber filesystem operations, and downloads the exact Box
version through the existing verified download path. Ordinary synchronization
and direct baseline replacement are refused while a resolution is incomplete.
The verified replacement baseline and completed resolution record commit in one
SQLite transaction. These APIs contain no CLI presentation logic and can be
called by either a command-line workflow or a desktop GUI.

## Initial download

For an empty or partially downloaded local root, build an explicit initial
Box-to-local plan and display the first ten actions:

```bash
.venv/bin/sync-box run --dry-run --initial-download-from-box --limit 10
```

This mode counts planned file downloads, files already verified, local folder
creations and reuse, total bytes, unknown file sizes, and unsupported Box items.
Existing expected files must match Box size and SHA-1 metadata where available.
Expected directories must be real, safe directories. Unexpected paths,
symlinks, type mismatches, unverifiable existing files, and metadata mismatches
stop planning. Dry-run mode does not download content, create folders, save a
baseline, or write state and log files.

After reviewing the complete plan, execute that initial population with:

```bash
.venv/bin/sync-box run --initial-download-from-box
```

Execution performs a fresh inventory and independently revalidates every reused
file and directory before making a local change. Unsupported Box items and
missing file IDs fail preflight. Each missing file is downloaded by its
inventoried version ID into a unique temporary file, checked against Box size
and SHA-1 metadata when available, and then published with an atomic no-clobber
operation. Existing local files are never replaced. A failed download or
verification removes its temporary file and stops the run. Completed files and
folders remain in place and the same command safely resumes by verifying and
skipping them. Completion and failure output reports downloaded,
already-verified/skipped, and failed/conflicting counts. Box access uses only
metadata reads and file-content GETs through the read-only downscoped token.
If that short-lived content token expires during a download, execution obtains a
fresh token with the same `root_readonly,item_download` scopes through the
authenticated Box CLI, rebuilds the SDK client, and retries that file once.
Other errors still stop the run, and rerunning the command verifies and skips
files that were already completed.
