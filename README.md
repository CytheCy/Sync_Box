# Sync Box

`sync-box` is a conservative two-way synchronizer for a normal Fedora folder and
a Box folder. It uses the official Box CLI login, verified SHA-1 inventories,
an external SQLite baseline, and short-lived downscoped tokens. Inventories and
dry runs use read-only access. An executing sync obtains a separate content
read/write token only for that run.

## Install on Fedora

The Fedora RPM installs the CLI, Qt tray application, desktop launcher, optional
KDE autostart entry, icon, and disabled systemd user units into normal system
locations. It pulls the Python, PySide6, Box SDK, systemd, and libsecret runtime
packages from Fedora. It never packages or removes user configuration, Box
credentials, the SQLite baseline, logs, or synchronized files. Installing or
upgrading the RPM does not enable the timer and does not start a synchronization.

Open the local RPM in KDE Discover for the normal graphical installation flow,
or use the single terminal fallback:

Install a built package with Fedora's package manager:

```bash
sudo dnf install ./sync-box-1.0.1-1.fc44.noarch.rpm
```

Launch **Sync_Box** from the Plasma application menu. The first-run wizard checks
the installed resources and runtime, Box CLI, Box authentication, local folder,
database baseline, and packaged timer. Fedora does not ship the official Box CLI
as an RPM. When it is missing, the wizard offers to download the matching Linux
archive from Box's official `box/boxcli` GitHub release, requires the SHA-256
digest published with that release, and installs only the verified `box` binary
under `~/.local/share/sync-box/box-cli/`. This is a per-user install and needs no
root password. The dependency logic is isolated in `sync_box.dependencies`.

The Connect button runs Box CLI's Official Box CLI App login and then proves
access with Sync_Box's existing read-only authentication test. Box CLI remains
the durable credential owner; Sync_Box keeps only its short-lived downscoped
token in memory. The wizard then uses a native directory chooser for the local
mirror (suggesting `~/Box`) and configures Box account root `0` as the complete
offline mirror.

An empty folder is populated through the existing version-pinned, staged,
no-clobber, SHA-1-verified initial download. A restarted wizard inventories both
sides again, recognizes already verified files, and safely resumes. A nonempty
folder is compared read-only. Matching trees may be baselined after another
fresh verification; differing trees stay in “Setup needs attention” and no
direction is chosen. The SQLite baseline is created transactionally only after
fresh inventories match.

After those gates pass, an explicit button runs `systemctl --user enable --now
sync-box.timer` through the systemd controller and verifies the timer is enabled
and active. The separate tray-at-login checkbox writes only the user's XDG
autostart preference. Closing the tray does not stop the timer. Expected setup
failures remain in the GUI, with sanitized details in
`~/.local/state/sync-box/sync-box.log`.

Build an RPM on Fedora with:

```bash
sudo dnf install rpm-build dnf-plugins-core
sudo dnf builddep ./packaging/sync-box.spec
./packaging/build-rpm.sh
```

Artifacts are written below `build/rpmbuild/RPMS/` and `build/rpmbuild/SRPMS/`.
The spec deliberately has no systemd enable/start scriptlet.

## Development install

Python 3.11 or newer is required. From this repository:

```bash
python3 -m venv dev-env
dev-env/bin/pip install -e .
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
sync-box auth login
```

The command creates and selects a Box CLI environment named `sync-box`, opens the
browser, and lets the Box CLI manage the callback and durable credentials. On
Linux the CLI uses Secret Service when available and otherwise falls back to
files under `~/.box`. For a headless login, add `--code`.

If authorization later expires, reauthorize the same environment:

```bash
sync-box auth login --reauthorize
```

Before each authenticated operation, `sync-box` asks the CLI to exchange its
credential for a short-lived token with the `root_readonly` and `item_download`
scopes. The first permits metadata reads and the second permits file-content
downloads. Only that downscoped token is given to the Python SDK, it is kept in
memory, and it cannot be refreshed or used to write Box content.

Test the connection with a read-only current-user request:

```bash
sync-box auth test
```

## Read-only inventories

Scan the local tree without following symbolic links:

```bash
sync-box inventory local
sync-box inventory local --summary-only
sync-box inventory local --json
```

Scan the configured Box folder recursively with metadata-only API requests:

```bash
sync-box inventory box
sync-box inventory box --summary-only
sync-box inventory box --json
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

Initialize a new database or migrate an older database to schema v5:

```bash
sync-box init
```

The migrations preserve existing data and add paired baseline generations, an
operation journal, generation-bound sync runs, and a single-running-sync guard.
Database and log files use mode `0600`; logs rotate at 5 MiB and keep three
backups.

## Baseline and two-way synchronization

Before a baseline exists, compare live local file hashes with Box SHA-1 metadata:

```bash
sync-box run --dry-run --summary-only
```

The dry run does not save inventories, create logs, or change either tree.
Matching files and folders are counted but omitted from the table. One-sided
items, type mismatches, unsupported types, unavailable hashes, and differing
content are reported for review. Because no common baseline exists yet, it does
not choose upload, download, or delete directions. Omit `--summary-only` for the
full review table, or add `--json` for structured review output.

Once both trees are expected to match, create the baseline:

```bash
sync-box baseline create
sync-box baseline status
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
sync-box run
```

Execution rejects any plan containing a conflict before changing either tree.
It journals each operation, binds the run to its baseline generation, and holds
a nonblocking lock on both the local root and state database through mutation
and verification. The exact plan is rebuilt under those locks and must remain
unchanged before journaling. Files are
revalidated before upload. Folder actions are bound to inventoried device/inode
identity and subtree state. Local moves use an atomic no-clobber rename; Box
folder deletion is always non-recursive. Box mutations use ETag preconditions,
and downloads are verified in temporary files, fsynced, and published
atomically. It never follows symlinks or accepts paths outside the root.
Token-expiration retries are bounded. An interrupted run keeps the prior
baseline, so a fresh inventory can safely plan the remaining work. A new
baseline is committed while the execution lock is still held, only after all
actions complete and another pair of fresh inventories proves that the trees
match.

## Periodic systemd user timer

The RPM installs equivalent static units at
`/usr/lib/systemd/user/sync-box.service` and
`/usr/lib/systemd/user/sync-box.timer`. The service runs
`/usr/bin/sync-box run --summary-only`. Enable it only after authentication,
configuration, and a verified baseline are ready:

```bash
systemctl --user daemon-reload
systemctl --user enable --now sync-box.timer
```

Closing or quitting the tray application does not stop or disable this timer.
The tray application's login startup is controlled separately through the
Freedesktop autostart entry and its Settings checkbox.

Install user units for the current installed `sync-box` executable:

```bash
sync-box systemd install
```

This writes `sync-box.service` and `sync-box.timer` below
`~/.config/systemd/user` (or `$XDG_CONFIG_HOME/systemd/user`) and reloads the
user manager. It deliberately does not enable or start either unit. The timer
runs a one-shot synchronization at the half hour and hour, with a stable delay
of up to two minutes. `Persistent=true` causes one missed invocation to run
after the user manager resumes or next starts; it does not replay every missed
interval. The service itself has no automatic restart loop.

When ready to activate periodic synchronization, use:

```bash
systemctl --user enable --now sync-box.timer
systemctl --user status sync-box.timer sync-box.service
journalctl --user-unit sync-box.service
```

The timer is normally active while the user manager is running. Running it
without an interactive login across reboot requires user lingering to be
configured separately. The service sends stdout, stderr, and sanitized
application messages to the user journal. Executing runs also retain the
configured rotating application log. The engine's nonblocking local-root and
database locks remain the authority for preventing concurrent executors;
systemd additionally will not run two instances of the same one-shot service.

Before removing an enabled installation, disable the timer. Removal itself
does not stop or disable anything:

```bash
systemctl --user disable --now sync-box.timer
sync-box systemd uninstall
```

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
sync-box run --dry-run --initial-download-from-box --limit 10
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
sync-box run --initial-download-from-box
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
