#!/usr/bin/python
import argparse
import io
import logging
import signal
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

from pc_teams_background import run

DEFAULT_INTERVAL_SECONDS = 15 * 60
DEFAULT_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5


def get_default_log_file() -> Path:
    log_dir = (Path(__file__).parent / "logs").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "runner.log"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Teams background generator continuously so it can be started "
            "at logon or hosted by another Windows process manager."
        )
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Seconds to wait between checks. Defaults to 900.",
    )
    parser.add_argument(
        "--settings-file",
        help="Optional path to a settings YAML file. Defaults to settings.yaml.",
    )
    parser.add_argument(
        "--log-file",
        default=str(get_default_log_file()),
        help="Path to the runner log file.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--force-first-run",
        action="store_true",
        help="Force regeneration on the first iteration only.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single iteration and exit.",
    )
    return parser


def configure_logging(log_file: Path, log_level: str) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pc_teams_background_runner")
    logger.setLevel(getattr(logging, log_level))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=DEFAULT_LOG_BYTES,
        backupCount=DEFAULT_LOG_BACKUPS,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    if sys.stdout is not None and hasattr(sys.stdout, "write"):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def flush_captured_output(logger: logging.Logger, buffer: io.StringIO) -> None:
    output = buffer.getvalue().strip()
    if not output:
        return

    for line in output.splitlines():
        logger.info(line)


def run_iteration(
    logger: logging.Logger, force: bool, settings_file: Optional[str]
) -> bool:
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            regenerated = run(force=force, settings_file=settings_file)
    except Exception:
        flush_captured_output(logger, buffer)
        raise

    flush_captured_output(logger, buffer)
    return regenerated


def register_signal_handlers(
    logger: logging.Logger, stop_event: threading.Event
) -> None:
    def _handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
        del frame
        logger.info("Received signal %s, stopping...", signum)
        stop_event.set()

    for signal_name in ["SIGINT", "SIGTERM", "SIGBREAK"]:
        sig = getattr(signal, signal_name, None)
        if sig is not None:
            signal.signal(sig, _handle_signal)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.interval_seconds <= 0 and not args.once:
        raise ValueError("--interval-seconds must be positive unless --once is used")

    log_file = Path(args.log_file).expanduser().resolve()
    logger = configure_logging(log_file, args.log_level)
    stop_event = threading.Event()
    register_signal_handlers(logger, stop_event)

    logger.info("Runner starting")
    logger.info("Settings file: %s", args.settings_file or "settings.yaml")
    logger.info("Interval: %s seconds", args.interval_seconds)
    logger.info("Log file: %s", log_file)

    iteration = 0
    while not stop_event.is_set():
        force = args.force_first_run and iteration == 0
        logger.info("Starting generator iteration %s", iteration + 1)
        try:
            regenerated = run_iteration(logger, force, args.settings_file)
            logger.info("Generator iteration complete (regenerated=%s)", regenerated)
        except Exception:
            logger.exception("Generator iteration failed")

        iteration += 1
        if args.once:
            break

        logger.info("Sleeping for %s seconds", args.interval_seconds)
        stop_event.wait(args.interval_seconds)

    logger.info("Runner exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
