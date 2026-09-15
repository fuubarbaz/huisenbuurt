"""Triggering a watch-loop cycle from the browser.

The cycle itself is already covered by the orchestrator tests. What is new here
is supervision: one at a time, progress that outlives the request that started
it, and a failure that says why instead of looking like a quiet no-op.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.pipeline.run_manager import RunManager


@pytest.fixture
def manager():
    return RunManager()


# -- single flight -----------------------------------------------------------


async def test_a_second_start_is_refused_while_one_is_in_flight(manager):
    """Two clicks must not become two cycles: the rate limiter is per-process
    and sized for one caller, so concurrency doubles what every upstream sees."""
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return 1

    assert manager.start(slow) is True
    assert manager.start(slow) is False          # refused, not queued

    gate.set()
    await manager._task
    assert manager.snapshot()["alerts_sent"] == 1


async def test_a_new_run_is_allowed_once_the_last_finished(manager):
    async def quick():
        return 0

    assert manager.start(quick) is True
    await manager._task
    assert manager.start(quick) is True
    await manager._task


async def test_a_refused_start_leaves_no_unawaited_coroutine(manager):
    """The factory is called inside start(), not by the caller — passing a
    coroutine in would leave one dangling every time a start was refused."""
    gate = asyncio.Event()
    calls = 0

    async def slow():
        await gate.wait()
        return 0

    def factory():
        nonlocal calls
        calls += 1
        return slow()

    manager.start(factory)
    manager.start(factory)                       # refused before calling factory
    await asyncio.sleep(0)                       # let the accepted task begin
    assert calls == 1

    gate.set()
    await manager._task


# -- reporting ---------------------------------------------------------------


async def test_the_outcome_outlives_the_run(manager):
    """A cycle that finished between two polls must not read as 'idle'."""
    async def quick():
        return 3

    manager.start(quick)
    await manager._task
    snap = manager.snapshot()

    assert snap["status"] == "done"
    assert snap["alerts_sent"] == 3
    assert snap["running"] is False
    assert snap["finished_at"] is not None


async def test_a_failure_reports_the_reason(manager):
    """Huispedia answering 403 and 'no new listings' look identical unless the
    error is surfaced — and Huispedia does currently answer 403."""
    async def boom():
        raise RuntimeError("403 Forbidden from huispedia.nl")

    manager.start(boom)
    await manager._task
    snap = manager.snapshot()

    assert snap["status"] == "failed"
    assert "403" in snap["error"]


async def test_progress_notes_are_captured(manager):
    async def chatty():
        manager.note("discovering new listings")
        manager.note("scored Teststraat 1 at 7.2", stage="scoring")
        return 0

    manager.start(chatty)
    await manager._task
    snap = manager.snapshot()

    assert any("discovering" in line for line in snap["log"])
    assert any("7.2" in line for line in snap["log"])


async def test_the_log_is_bounded(manager):
    """A long cycle must not grow the log without limit."""
    for i in range(RunManager.MAX_LOG_LINES + 40):
        manager.note(f"line {i}")

    log = manager.snapshot()["log"]
    assert len(log) == RunManager.MAX_LOG_LINES
    assert "line 0" not in " ".join(log)          # oldest dropped, newest kept


async def test_the_snapshot_is_a_copy(manager):
    """A caller iterating the log while a cycle appends to it would otherwise
    hit 'list changed size during iteration'."""
    manager.note("one")
    snap = manager.snapshot()
    manager.note("two")

    assert len(snap["log"]) == 1


# -- cancelling --------------------------------------------------------------


async def test_a_running_cycle_can_be_cancelled(manager):
    async def forever():
        await asyncio.sleep(3600)
        return 0

    manager.start(forever)
    assert await manager.cancel() is True
    assert manager.snapshot()["status"] == "cancelled"
    assert manager.running is False


async def test_cancelling_nothing_is_reported_not_raised(manager):
    assert await manager.cancel() is False


# -- the endpoints -----------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    with TestClient(main_module.app) as c:
        yield c


def test_status_is_idle_before_anything_runs(client):
    body = client.get("/run").json()
    assert body["status"] == "idle"
    assert body["running"] is False


def test_a_second_trigger_is_a_409(client, monkeypatch):
    gate = asyncio.Event()

    class SlowOrchestrator:
        def __init__(self, *a, **kw): pass
        async def run_once(self):
            await gate.wait()
            return 0

    monkeypatch.setattr(main_module, "Orchestrator", SlowOrchestrator)

    assert client.post("/run").status_code == 202
    assert client.post("/run").status_code == 409

    gate.set()
    client.delete("/run")


def test_cancelling_when_idle_is_a_409(client):
    assert client.delete("/run").status_code == 409


def test_a_region_can_be_supplied_with_the_trigger(client, monkeypatch):
    seen = {}

    class RecordingOrchestrator:
        def __init__(self, *a, region_filter=None, **kw):
            seen["region_filter"] = region_filter
        async def run_once(self):
            return 0

    monkeypatch.setattr(main_module, "Orchestrator", RecordingOrchestrator)

    res = client.post("/run", json={"regions": "Amsterdam"})
    assert res.status_code == 202

    import time
    for _ in range(20):
        if not client.get("/run").json()["running"]:
            break
        time.sleep(0.05)

    assert not seen["region_filter"].is_empty
    assert "amsterdam" in str(seen["region_filter"])


def test_an_invalid_region_is_a_422(client):
    res = client.post("/run", json={"regions": "Amsterdam, ???"})
    assert res.status_code == 422
    assert "not a city name or a postcode" in res.json()["detail"]


def test_no_region_field_falls_back_to_settings(client, monkeypatch):
    """Omitting the field entirely, not just sending it blank, must still let
    WOONAGENT_REGIONS apply — a browser client that never sends the key
    should not silently clear a daemon-wide filter."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "regions", "Almelo")
    seen = {}

    class RecordingOrchestrator:
        def __init__(self, *a, region_filter=None, **kw):
            seen["region_filter"] = region_filter
        async def run_once(self):
            return 0

    monkeypatch.setattr(main_module, "Orchestrator", RecordingOrchestrator)
    client.post("/run", json={})

    import time
    for _ in range(20):
        if not client.get("/run").json()["running"]:
            break
        time.sleep(0.05)

    assert "almelo" in str(seen["region_filter"])
