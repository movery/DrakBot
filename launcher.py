#!/usr/bin/env python3
"""Launcher for DrakBot.

Wraps `main.run()` with the operational concerns that plain `python main.py`
doesn't handle:

- **Single instance** — acquires an advisory file lock so a second launcher
  refuses to start. Multiple instances on the same token race to handle every
  interaction, so this must be enforced.
- **Per-run logging** — writes a timestamped, rotating log file under `logs/`
  in addition to the console, and captures unhandled exceptions.
- **Pre-flight checks** — confirms dependencies are importable (i.e. the venv
  is active) and that DISCORD_TOKEN is set before attempting to connect.

This is the intended entry point in production:

    source .venv/bin/activate && python launcher.py
"""
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(_BASE_DIR, "drakbot.lock")
LOG_DIR = os.path.join(_BASE_DIR, "logs")
LOG_LEVEL = logging.INFO

log = logging.getLogger("launcher")


def acquire_single_instance_lock():
    """Take an exclusive advisory lock and return the open file handle.

    fcntl.flock is released automatically when the process exits — even on a
    crash — so there's no stale-PID problem to clean up. The handle must stay
    open (and referenced) for the lifetime of the process to hold the lock.
    """
    import fcntl

    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        sys.stderr.write(
            f"Another DrakBot instance already holds the lock ({LOCK_PATH}).\n"
            "Refusing to start a second instance — stop the running one first.\n"
        )
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def setup_logging() -> str:
    """Configure root logging to a timestamped rotating file plus the console.
    Returns the path of the log file for this run."""
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(LOG_DIR, f"drakbot_{timestamp}.log")

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    return log_path


def verify_environment() -> str:
    """Check that prerequisites are met; exit with a clear message otherwise.
    Returns the validated Discord token."""
    try:
        import discord  # noqa: F401
        from dotenv import load_dotenv
    except ImportError as exc:
        sys.stderr.write(
            f"Missing dependency: {exc.name}. Activate the virtualenv first:\n"
            "    source .venv/bin/activate\n"
        )
        sys.exit(1)

    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.stderr.write("DISCORD_TOKEN is not set (check your .env). Aborting.\n")
        sys.exit(1)
    return token


def main():
    # Lock first, before any side effects, so a duplicate exits cleanly.
    lock_file = acquire_single_instance_lock()
    log_path = setup_logging()
    token = verify_environment()

    log.info("Starting DrakBot (pid %s); logging to %s", os.getpid(), log_path)
    import main as bot_main
    try:
        bot_main.run(token)
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down.")
    except Exception:
        log.exception("Bot exited with an unhandled exception.")
        raise
    finally:
        log.info("DrakBot stopped.")
        lock_file.close()


if __name__ == "__main__":
    main()
