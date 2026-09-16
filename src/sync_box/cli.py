"""Command-line entry point for safe local/Box operations."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from sync_box.box_auth import (
    AuthenticationError,
    authorize,
    test_authentication,
)
from sync_box.box_inventory import scan_box
from sync_box.config import AppConfig, ConfigError, default_config_path, load_config
from sync_box.database import initialize_database, save_inventory
from sync_box.inventory import ScanError, render_inventory, summarize
from sync_box.initial_download import InitialDownloadError, execute_initial_download
from sync_box.local_inventory import scan_local
from sync_box.logging_setup import configure_logging
from sync_box.planner import (
    build_comparison_plan,
    build_initial_download_plan,
    render_initial_download_plan,
    render_plan,
    summarize_initial_download,
    summarize_plan,
)


LOGGER = logging.getLogger("sync_box")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync-box",
        description="Conservative local/Box folder synchronization",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="external TOML config path (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-config", help="validate configuration without writing")
    subparsers.add_parser("init", help="create or migrate the state database and log")

    auth_parser = subparsers.add_parser("auth", help="log in with the official Box CLI")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    login_parser = auth_commands.add_parser(
        "login", help="authorize the official Box CLI application"
    )
    login_parser.add_argument(
        "--reauthorize",
        action="store_true",
        help="reauthorize the existing sync-box CLI environment",
    )
    login_parser.add_argument(
        "--code",
        action="store_true",
        help="manually enter the authorization code for a headless machine",
    )
    auth_commands.add_parser("test", help="make a read-only current-user API call")

    inventory_parser = subparsers.add_parser(
        "inventory", help="scan metadata without changing local or Box content"
    )
    inventory_commands = inventory_parser.add_subparsers(
        dest="inventory_source", required=True
    )
    for source in ("local", "box"):
        source_parser = inventory_commands.add_parser(source)
        source_parser.add_argument(
            "--json", action="store_true", help="display full inventory as JSON"
        )
        source_parser.add_argument(
            "--summary-only", action="store_true", help="display counts only"
        )
        source_parser.add_argument(
            "--save",
            action="store_true",
            help="save the metadata snapshot to the external SQLite database",
        )

    run_parser = subparsers.add_parser(
        "run", help="compare trees or perform the initial Box-to-local download"
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect without changing local files, Box, state, or log files",
    )
    run_parser.add_argument(
        "--json", action="store_true", help="display review items as JSON"
    )
    run_parser.add_argument(
        "--summary-only", action="store_true", help="display comparison counts only"
    )
    run_parser.add_argument(
        "--initial-download-from-box",
        action="store_true",
        help="plan, execute, or safely resume initial Box-to-local population",
    )
    run_parser.add_argument(
        "--limit",
        type=_positive_int,
        help="display only the first N plan items while retaining full counts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"sync-box: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "check-config":
            configure_logging()
            LOGGER.info("Configuration is valid; no files were changed")
            return 0

        if args.command == "init":
            configure_logging(config.log_file)
            initialize_database(config.state_database)
            LOGGER.info("State database is ready at %s", config.state_database)
            return 0

        if args.command == "auth":
            configure_logging()
            return _handle_auth(args, config)

        if args.command == "inventory":
            configure_logging(config.log_file if args.save else None)
            return _handle_inventory(args, config)

        configure_logging()
        if not args.dry_run and not args.initial_download_from_box:
            print(
                "sync-box: general synchronization is not implemented; use run --dry-run",
                file=sys.stderr,
            )
            return 2
        return _handle_run(args, config)
    except (
        AuthenticationError,
        InitialDownloadError,
        ScanError,
        OSError,
        RuntimeError,
    ) as exc:
        if (
            args.command == "run"
            and args.initial_download_from_box
            and not args.dry_run
        ):
            result = getattr(exc, "result", None)
            print(
                "Initial download stopped: "
                f"downloaded={getattr(result, 'downloaded', 0)}, "
                "already_verified_skipped="
                f"{getattr(result, 'already_verified', 0)}, "
                "failed_conflicting="
                f"{getattr(result, 'failed_conflicting', 1)}",
                file=sys.stderr,
            )
        print(f"sync-box: {exc}", file=sys.stderr)
        return 1


def _handle_auth(args: argparse.Namespace, config: AppConfig) -> int:
    if args.auth_command == "login":
        authorize(reauthorize=args.reauthorize, code=args.code)
        print("Box authorization completed; credentials are managed by the Box CLI")
    else:
        user_id, user_name = test_authentication(config)
        print(f"Authenticated to Box as {user_name} (user ID {user_id})")
    return 0


def _handle_inventory(args: argparse.Namespace, config: AppConfig) -> int:
    if args.inventory_source == "local":
        items = scan_local(
            config.local_root,
            excluded_paths=config.excluded_paths,
            excluded_names=config.excluded_names,
        )
        root_identifier = str(config.local_root)
    else:
        from sync_box.box_auth import build_authenticated_client

        items = scan_box(
            build_authenticated_client(config),
            config.box_folder_id,
            excluded_paths=config.excluded_paths,
            excluded_names=config.excluded_names,
        )
        root_identifier = config.box_folder_id

    counts = summarize(items)
    print("Summary: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    if not args.summary_only:
        print(render_inventory(items, as_json=args.json))
    if args.save:
        run_id = save_inventory(
            config.state_database,
            source=args.inventory_source,
            root_identifier=root_identifier,
            items=items,
        )
        LOGGER.info("Saved inventory snapshot %s", run_id)
    return 0


def _handle_run(args: argparse.Namespace, config: AppConfig) -> int:
    from sync_box.box_auth import build_authenticated_client

    local_items = scan_local(
        config.local_root,
        hash_files=True,
        excluded_paths=config.excluded_paths,
        excluded_names=config.excluded_names,
    )
    client = build_authenticated_client(config)
    box_items = scan_box(
        client,
        config.box_folder_id,
        excluded_paths=config.excluded_paths,
        excluded_names=config.excluded_names,
    )
    if args.initial_download_from_box:
        plan = build_initial_download_plan(local_items, box_items)
        counts = summarize_initial_download(plan)
        print(
            "Initial download plan: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
        if not args.summary_only:
            if args.limit is not None:
                print(f"Showing first {min(args.limit, len(plan))} of {len(plan)} items")
            print(
                render_initial_download_plan(
                    plan, as_json=args.json, limit=args.limit
                )
            )
        if not args.dry_run:
            result = execute_initial_download(
                client,
                config.local_root,
                plan,
                refresh_client=lambda: build_authenticated_client(config),
            )
            print(
                "Initial download complete: "
                f"downloaded={result.downloaded}, "
                f"already_verified_skipped={result.already_verified}, "
                f"failed_conflicting={result.failed_conflicting}, "
                f"folders_created={result.folders_created}, "
                f"folders_reused={result.folders_reused}, bytes={result.bytes}, "
                f"sha1_verified={result.verified_sha1}"
            )
        return 0

    plan = build_comparison_plan(local_items, box_items)
    counts = summarize_plan(plan)
    print(
        "Comparison summary: "
        + ", ".join(f"{key}={value}" for key, value in counts.items())
    )
    if not args.summary_only:
        print(render_plan(plan, as_json=args.json, limit=args.limit))
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
