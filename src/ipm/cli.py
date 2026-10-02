"""Entry point: parse a handful of optional flags, then hand over to the TUI.

There are deliberately no sub-commands. ``ipm`` starts one persistent application;
importing and deleting are buttons inside it, not things you type.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ipm import __version__
from ipm.config import Config, clamp_concurrency, load_config, save_config
from ipm.logging_setup import configure_logging, default_log_path

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Create the (intentionally tiny) argument parser."""
    parser = argparse.ArgumentParser(
        prog="ipm",
        description=(
            "Persistent terminal app that imports photos and videos from an iPhone over "
            "USB, and later deletes from the phone only what is verified on this Mac."
        ),
        epilog="Run 'ipm' with no arguments; everything else happens inside the app.",
    )
    parser.add_argument(
        "--dest",
        "-d",
        type=Path,
        metavar="FOLDER",
        help="set the destination folder (remembered for next time)",
    )
    parser.add_argument(
        "--concurrency",
        "-j",
        type=int,
        metavar="N",
        help="number of parallel downloads (default: 3)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        metavar="FILE",
        help="write a debug log to FILE (logging is off by default)",
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help=f"write a debug log to the default location ({default_log_path()})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="log at DEBUG level (only meaningful with --log/--log-file)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> tuple[Config, bool]:
    """Merge command-line flags into the stored configuration.

    :return: ``(config, changed)`` where *changed* says whether it is worth saving.
    """
    changed = False
    if args.dest is not None:
        config = config.with_destination(args.dest)
        changed = True
    if args.concurrency is not None:
        config = Config(
            destination=config.destination,
            concurrency=clamp_concurrency(args.concurrency),
            log_file=config.log_file,
        )
        changed = True
    log_file = args.log_file or (default_log_path() if args.log else None)
    if log_file is not None:
        config = Config(
            destination=config.destination,
            concurrency=config.concurrency,
            log_file=log_file,
        )
    return config, changed


def main(argv: list[str] | None = None) -> int:
    """Run the application.

    :param argv: Argument list for testing; defaults to :data:`sys.argv`.
    :return: Process exit code.
    """
    args = build_parser().parse_args(argv)
    config = load_config()
    config, changed = _apply_overrides(config, args)

    configure_logging(config.log_file, verbose=args.verbose)

    if changed:
        try:
            save_config(config)
        except OSError as exc:
            print(f"Warning: could not save settings: {exc}", file=sys.stderr)

    if config.destination is not None:
        try:
            config.destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"Cannot use the destination folder {config.destination}: "
                f"{exc.strerror or exc}",
                file=sys.stderr,
            )
            return 1

    # Imported here so that --help and --version work even if Textual is missing.
    from ipm.tui.app import run

    try:
        run(config)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
