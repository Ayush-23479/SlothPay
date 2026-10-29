"""Worker CLI must not touch the database without an explicit --run flag."""

from __future__ import annotations

import pytest

from backend.webhooks import worker


def test_help_never_invokes_async_worker(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("--help must not run the worker")

    monkeypatch.setattr(worker.asyncio, "run", unexpected)
    with pytest.raises(SystemExit) as exc:
        worker.main(["--help"])
    assert exc.value.code == 0
    assert "--run" in capsys.readouterr().out


def test_no_args_never_invokes_worker(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("no arguments must not run the worker")

    monkeypatch.setattr(worker.asyncio, "run", unexpected)
    assert worker.main([]) == 0
    assert "--run" in capsys.readouterr().out


def test_invalid_limit_fails_before_connecting(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid limit must not run the worker")

    monkeypatch.setattr(worker.asyncio, "run", unexpected)
    with pytest.raises(SystemExit) as exc:
        worker.main(["--run", "--limit", "0"])
    assert exc.value.code == 2


def test_unknown_flag_fails_before_connecting(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected arguments must not run the worker")

    monkeypatch.setattr(worker.asyncio, "run", unexpected)
    with pytest.raises(SystemExit) as exc:
        worker.main(["--helpful"])
    assert exc.value.code == 2


def test_explicit_run_uses_selected_limit(monkeypatch, capsys):
    seen = []

    async def fake_process_batch(limit):
        seen.append(limit)
        return []

    monkeypatch.setattr(worker, "process_batch", fake_process_batch)
    assert worker.main(["--run", "--limit", "3"]) == 0
    assert seen == [3]
    assert capsys.readouterr().out.strip() == "[]"
