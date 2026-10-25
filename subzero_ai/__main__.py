"""Run SubZero AI.

    python -m subzero_ai               start the app, using config.env
    python -m subzero_ai --test-mode   start with sample sign-ins (and a pretend Claude if there's no API key)
    python -m subzero_ai check         check config.env, Slack, and the Claude API key
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import webbrowser
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from . import __version__
from .config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m subzero_ai", description="SubZero AI: mentor-approved Claude.")
    parser.add_argument("command", nargs="?", choices=["run", "check"], default="run",
                        help="run (default) starts the app; check tests the settings")
    parser.add_argument("--config", help="path to config.env")
    parser.add_argument("--test-mode", action="store_true",
                        help="sample sign-ins on this PC, data in ./test-data, and a pretend Claude without an API key")
    parser.add_argument("--open", action="store_true", help="open the app in the browser once it starts")
    parser.add_argument("--verbose", action="store_true", help="log more detail")
    parser.add_argument("--version", action="version", version=f"SubZero AI {__version__}")
    return parser


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    log_dir.mkdir(parents=True, exist_ok=True)
    to_file = TimedRotatingFileHandler(log_dir / "subzero.log", when="midnight", backupCount=14, encoding="utf-8")
    to_file.setFormatter(fmt)
    root.addHandler(to_file)
    for noisy in ("urllib3", "websockets", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict = {}
    if args.test_mode:
        overrides["dev_login"] = True
        if not os.environ.get("DATA_DIR"):
            overrides["data_dir"] = Path.cwd() / "test-data"
    # Test mode only reads test-data/config.env, never a real config.env sitting in the app folder.
    settings = load_settings(args.config, search_cwd=not args.test_mode, **overrides)
    if args.test_mode and not settings.anthropic_api_key:
        settings.fake_claude = True
    setup_logging(settings.data_dir / "logs", args.verbose)

    if args.command == "check":
        from .checks import print_report, run_checks
        return print_report(asyncio.run(run_checks(settings)))

    import uvicorn

    from .web import create_app

    app = create_app(settings)
    if args.open:
        page = "auth/dev" if settings.dev_login else ""
        threading.Timer(1.5, webbrowser.open, [f"http://localhost:{settings.port}/{page}"]).start()
    config = uvicorn.Config(
        app, host=settings.host, port=settings.port,
        # Trust the X-Forwarded headers from the Cloudflare connector, which runs on this PC.
        proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1",
        log_config=None, access_log=False, server_header=False,
        # Open live-update connections would otherwise hold up Ctrl+C and service restarts.
        timeout_graceful_shutdown=3,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
