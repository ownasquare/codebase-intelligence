from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from codebase_intelligence import __version__, cli


class FakeProcess:
    def __init__(self, polls: Sequence[int | None]) -> None:
        self._polls = list(polls)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._polls:
            result = self._polls.pop(0)
            if result is not None:
                self.returncode = result
            return result
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        self.returncode = 0
        return 0


def test_version_flag_matches_package(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])

    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"codebase-intelligence {__version__}"


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_invalid_ports_are_rejected(value: str) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(["api", "--port", value])

    assert result.value.code == 2


def test_demo_starts_explicit_api_and_ui_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeProcess([None, 0])
    ui = FakeProcess([None, None])
    processes = iter((api, ui))
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((command, kwargs))
        return next(processes)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    result = cli.main(["demo", "--api-port", "8100", "--ui-port", "8600"])

    assert result == 0
    assert len(calls) == 2
    assert calls[0][0][1:4] == ["-m", "uvicorn", "codebase_intelligence.api.app:app"]
    assert calls[1][0][1:4] == ["-m", "streamlit", "run"]
    assert calls[0][1]["start_new_session"] is True
    assert "shell" not in calls[0][1]
    assert calls[0][1]["env"]["CODEBASE_INTEL_API_BASE_URL"] == "http://127.0.0.1:8100"
    assert calls[1][1]["env"]["CODEBASE_INTEL_INLINE_WORKER"] == "true"
    assert ui.terminated is True


@pytest.mark.parametrize(
    ("arguments", "module_name"),
    [
        (["api"], "uvicorn"),
        (["ui"], "streamlit"),
        (["worker"], "codebase_intelligence.worker"),
    ],
)
def test_service_subcommands_delegate_to_one_explicit_child(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    module_name: str,
) -> None:
    captured: list[tuple[Sequence[Sequence[str]], dict[str, str]]] = []

    def fake_run(commands: Sequence[Sequence[str]], env: dict[str, str]) -> int:
        captured.append((commands, env))
        return 17

    monkeypatch.setattr(cli, "_run_commands", fake_run)

    assert cli.main(arguments) == 17
    command = captured[0][0][0]
    assert "-m" in command
    assert module_name in command


def test_keyboard_interrupt_stops_every_child(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeProcess([None])
    ui = FakeProcess([None])
    processes = iter((api, ui))

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_args, **_kwargs: next(processes))
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    result = cli._run_commands((("api",), ("ui",)), {})

    assert result == 130
    assert api.terminated is True
    assert ui.terminated is True


def test_start_failure_cleans_up_an_already_started_child(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeProcess([None])
    calls = 0

    def fake_popen(*_args: Any, **_kwargs: Any) -> FakeProcess:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("test launch failure")
        return api

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    result = cli._run_commands((("api",), ("ui",)), {})

    assert result == 1
    assert api.terminated is True
    assert "Could not start Codebase Intelligence" in capsys.readouterr().err


def test_stop_processes_force_kills_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess([None])

    def timeout_wait(timeout: float | None = None) -> int:
        del timeout
        if not process.killed:
            raise subprocess.TimeoutExpired(cmd="service", timeout=0)
        process.returncode = -9
        return -9

    monkeypatch.setattr(process, "wait", timeout_wait)

    cli._stop_processes([process])  # type: ignore[list-item]

    assert process.terminated is True
    assert process.killed is True
