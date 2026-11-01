"""Run SubZero AI.

    python -m subzero_ai               start the app, using config.env
    python -m subzero_ai --test-mode   start with sample sign-ins (and a pretend Claude if there's no API key)
    python -m subzero_ai check         check config.env, Slack, and the Claude API key
    python -m subzero_ai check --model provider/id   run only the model safety check on one model
    python -m subzero_ai claude-login  sign in the Claude subscription (Claude Code) that claude-bridge/ models use
    python -m subzero_ai backup [LABEL] back up the database, attached files, and prompts now
    python -m subzero_ai backups       list the backups
    python -m subzero_ai restore NAME  put a backup back (stop the app first; "latest" picks the newest)
    python -m subzero_ai recovery-code make a new recovery code for signing in on the mini PC
    python -m subzero_ai reset-setup   open the setup page again (it only answers on the mini PC)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import urllib.request
import webbrowser
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from . import __version__
from .config import Settings, load_settings

# Exit code that asks for a restart: the Windows service starts the app again, and run.ps1 loops on it.
RESTART_EXIT_CODE = 3
COMMANDS = ["run", "check", "claude-login", "backup", "backups", "restore", "recovery-code", "reset-setup"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m subzero_ai", description="SubZero AI: mentor-approved Claude.")
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="run",
                        help="run (default) starts the app; see the list above for the others")
    parser.add_argument("name", nargs="?",
                        help="for restore: the backup's file name or path, or latest; for backup: a label")
    parser.add_argument("--model", help="for check: run only the model safety check on this model (provider/id)")
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

    if args.model and args.command != "check":
        print("--model only works with the check command.")
        return 2
    if args.command == "check" and args.model:
        return safety_check_tool(args.model, settings)
    if args.command == "check":
        from .checks import print_report, run_checks
        return print_report(asyncio.run(run_checks(settings)))
    if args.command == "claude-login":
        return claude_login_tool(settings)
    if args.command != "run":
        return run_tool(args.command, args.name, settings)

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
    server = uvicorn.Server(config)
    restart = threading.Event()

    def request_restart() -> None:
        restart.set()
        server.should_exit = True

    app.state.request_restart = request_restart
    server.run()
    if restart.is_set():
        logging.getLogger("subzero").info("Restarting to load the new settings")
        return RESTART_EXIT_CODE
    return 0


def app_is_running(settings: Settings) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{settings.port}/healthz", timeout=2):
            return True
    except Exception:
        return False


def safety_check_tool(model: str, settings: Settings, client=None) -> int:
    """Run only the model safety check on one model, store the result, and print it. Exit code 0 on a pass."""
    from . import model_check, saved_settings
    from .ai_backend import make_client, resolve_backend
    from .db import Database

    db = Database(settings.db_path)
    saved_settings.load_into(settings, db)  # the providers an admin turned on
    model, problem = saved_settings.check_model(model, settings.ai_providers, resolve_backend(settings))
    if problem:
        print(problem)
        return 2

    async def run():
        own = client is None
        ai = client or make_client(settings)
        try:
            return await model_check.run_model_check(ai, settings, model)
        finally:
            if own and hasattr(ai, "aclose"):
                await ai.aclose()

    problem = model_check.judge_problem(settings)
    if problem:
        print(problem)
        return 2
    print(f"Model safety check on {model} (judge: {settings.model_check_judge}). This takes a minute or two.")
    result = asyncio.run(run())
    print("")
    for case in result.cases:
        label = model_check.CASE_LABELS.get(case.case, case.case)
        print(f"  {'PASS' if case.passed else 'FAIL'}  {label}" + ("" if case.passed else f": {case.detail}"))
    model_check.save_result(db, result, "check command")
    db.audit("model_check_done", actor_id=None, details={
        "model": model, "chat_passed": result.chat_passed, "screener_passed": result.screener_passed,
        "failed": [c.case for c in result.failed], "check_version": result.check_version, "by": "check command"})
    print("")
    print(f"Chats: {'passed' if result.chat_passed else 'FAILED'}. "
          f"Screener: {'passed' if result.screener_passed else 'FAILED'}.")
    return 0 if result.chat_passed and result.screener_passed else 1


def claude_login_tool(settings: Settings, run=None) -> int:
    """Sign in to Claude Code for claude-bridge/ models. The login is kept in <data>/bridge/claude-config, apart from
    any other Claude Code login on this PC. `run` stands in for subprocess.run in tests."""
    import subprocess

    from .bridge_login import login_env
    from .pi_client import bridge_logged_in, claude_cli_path, settings_bridge_env
    cli = claude_cli_path(settings.pi_gateway_script or None)
    if cli is None:
        print("Couldn't find the Claude Code program in pi_gateway/node_modules. "
              "Install the AI helper's files (npm ci --ignore-scripts in the pi_gateway folder) and try again.")
        return 1
    env = login_env(settings.data_dir)
    print("Signing in to Claude. A browser window opens; sign in with the Claude Pro or Max account to use.")
    print("")
    try:
        code = (run or subprocess.run)([str(cli), "auth", "login", "--claudeai"], env=env).returncode
    except OSError as exc:
        print(f"Couldn't start Claude Code: {exc}")
        return 1
    if code != 0 or not bridge_logged_in(settings.data_dir):
        print("")
        print("The sign-in didn't finish. Run python -m subzero_ai claude-login to try again.")
        return 1
    print("")
    print("Signed in. Turn on \"Claude subscription (Claude Code)\" under AI providers in Settings to use it"
          + ("." if settings_bridge_env(settings) else " (it needs AI_BACKEND=pi)."))
    return 0


def run_tool(command: str, name: str | None, settings: Settings) -> int:
    """The maintenance commands. They print plain sentences, since mentors run them from a .cmd file."""
    from . import backup, setup_flow
    from .db import Database

    try:
        if command == "backup":
            Database(settings.db_path)  # make sure the database exists and is up to date
            path = backup.make_backup(settings, label=name or "manual")
            print(f"Backed up to {path}")
        elif command == "backups":
            found = backup.list_backups(settings)
            if not found:
                print(f"There are no backups in {settings.backups_path} yet.")
            for info in found:
                print(f"  {info.name}   {info.created:%Y-%m-%d %H:%M}   {backup.size_text(info.size)}")
        elif command == "restore":
            if not name:
                print("Say which backup to restore: a file name from 'backups', or latest.")
                return 2
            if app_is_running(settings):
                print("SubZero AI is running. Stop it first (restore.cmd does this for you), then try again.")
                return 1
            path = backup.find_backup(settings, name)
            result = backup.restore_backup(settings, path)
            print(f"Restored {path.name} (made {result['manifest'].get('created_at', 'at an unknown time')}).")
            if result["safety_backup"]:
                print(f"The data from before the restore was saved as {result['safety_backup'].name}.")
        elif command == "recovery-code":
            code = setup_flow.new_recovery_code(Database(settings.db_path))
            print("New recovery code (the old one no longer works). Keep it somewhere safe:")
            print("")
            print(f"    {code}")
            print("")
            print(f"Use it at http://localhost:{settings.port}/auth/recovery on this PC.")
        elif command == "reset-setup":
            setup_flow.reopen_setup(Database(settings.db_path))
            print(f"The setup page is open again at http://localhost:{settings.port}/setup on this PC.")
    except backup.BackupError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
