"""Unified command line entry point for local Codebase Intelligence workflows."""

from __future__ import annotations

import argparse
import os

# Child processes use fixed argument lists and never invoke a shell.
import subprocess  # nosec B404
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from codebase_intelligence import __version__

_POLL_SECONDS = 0.2
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _port(value: str) -> int:
    """Return a valid TCP port for argparse."""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be a number") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codebase-intelligence",
        description="Index a codebase and investigate it with cited answers.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="Start the API and web app in one terminal.")
    demo.add_argument("--api-host", default="127.0.0.1", help="API bind address.")
    demo.add_argument("--api-port", default=8000, type=_port, help="API port.")
    demo.add_argument("--ui-host", default="127.0.0.1", help="Web app bind address.")
    demo.add_argument("--ui-port", default=8501, type=_port, help="Web app port.")

    api = commands.add_parser("api", help="Start only the FastAPI service.")
    api.add_argument("--host", default="127.0.0.1", help="API bind address.")
    api.add_argument("--port", default=8000, type=_port, help="API port.")

    ui = commands.add_parser("ui", help="Start only the Streamlit web app.")
    ui.add_argument("--host", default="127.0.0.1", help="Web app bind address.")
    ui.add_argument("--port", default=8501, type=_port, help="Web app port.")
    ui.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help="URL of the running API service.",
    )

    commands.add_parser("worker", help="Start the standalone shared-Qdrant worker.")
    return parser


def _api_command(host: str, port: int) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "uvicorn",
        "codebase_intelligence.api.app:app",
        "--host",
        host,
        "--port",
        str(port),
    )


def _ui_command(host: str, port: int) -> tuple[str, ...]:
    app_path = Path(__file__).resolve().parent / "ui" / "app.py"
    return (
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    )


def _worker_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "codebase_intelligence.worker")


def _runtime_environment(
    *,
    api_host: str | None = None,
    api_port: int | None = None,
    api_base_url: str | None = None,
    ui_host: str | None = None,
    ui_port: int | None = None,
    inline_worker: bool | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    if api_host is not None:
        env["CODEBASE_INTEL_API_HOST"] = api_host
    if api_port is not None:
        env["CODEBASE_INTEL_API_PORT"] = str(api_port)
    if api_base_url is not None:
        env["CODEBASE_INTEL_API_BASE_URL"] = api_base_url
    if ui_host is not None:
        env["CODEBASE_INTEL_UI_HOST"] = ui_host
    if ui_port is not None:
        env["CODEBASE_INTEL_UI_PORT"] = str(ui_port)
    if inline_worker is not None:
        env["CODEBASE_INTEL_INLINE_WORKER"] = "true" if inline_worker else "false"
    return env


def _stop_processes(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    running = [process for process in processes if process.poll() is None]
    for process in running:
        with suppress(ProcessLookupError):
            process.terminate()

    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
    for process in running:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()

    for process in running:
        if process.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)


def _run_commands(commands: Sequence[Sequence[str]], env: Mapping[str, str]) -> int:
    """Run explicit child commands until one exits, then clean up every child."""

    processes: list[subprocess.Popen[bytes]] = []
    try:
        for command in commands:
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                list(command),
                env=dict(env),
                start_new_session=True,
            )
            processes.append(process)
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    return exit_code if exit_code >= 0 else 1
            time.sleep(_POLL_SECONDS)
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        print(f"Could not start Codebase Intelligence: {error}", file=sys.stderr)
        return 1
    finally:
        _stop_processes(processes)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested Codebase Intelligence service command."""

    args = _parser().parse_args(argv)
    if args.command == "demo":
        api_base_url = f"http://{args.api_host}:{args.api_port}"
        env = _runtime_environment(
            api_host=args.api_host,
            api_port=args.api_port,
            api_base_url=api_base_url,
            ui_host=args.ui_host,
            ui_port=args.ui_port,
            inline_worker=True,
        )
        print(f"Workbench: http://{args.ui_host}:{args.ui_port}")
        print("Press Ctrl+C to stop both services.")
        return _run_commands(
            (_api_command(args.api_host, args.api_port), _ui_command(args.ui_host, args.ui_port)),
            env,
        )
    if args.command == "api":
        api_base_url = f"http://{args.host}:{args.port}"
        env = _runtime_environment(
            api_host=args.host,
            api_port=args.port,
            api_base_url=api_base_url,
        )
        return _run_commands((_api_command(args.host, args.port),), env)
    if args.command == "ui":
        env = _runtime_environment(
            api_base_url=args.api_base_url,
            ui_host=args.host,
            ui_port=args.port,
        )
        return _run_commands((_ui_command(args.host, args.port),), env)
    return _run_commands((_worker_command(),), _runtime_environment())


if __name__ == "__main__":
    raise SystemExit(main())
