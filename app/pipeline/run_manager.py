"""Single-flight supervisor for a manually triggered cycle.

A cycle is minutes long, not seconds: it scrapes up to
``max_listings_per_cycle`` listings with a 2-6 second politeness pause between
detail pages, and enriches each one with about a dozen upstream calls. So the
API cannot run it inside a request — it starts a background task and the client
polls for progress.

Two guarantees, and they are the whole point of the class:

* **One at a time.** Two clicks on a button must not become two concurrent
  cycles. That would double the request rate every upstream sees, and the rate
  limiter is per-process and sized for one caller.
* **Progress survives the request that started it.** State lives here rather
  than in the task, so a poll arriving after the task finished still gets the
  outcome instead of "idle" — otherwise a run that completed between two polls
  looks like it never happened.

Deliberately in-memory and process-local. This is not a job queue: restart the
server and a running cycle is gone, which is correct for a personal tool where
the next cycle simply picks up whatever was missed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunManager:
    """Owns at most one in-flight cycle and the record of the last one."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._state: dict[str, Any] = {
            "status": "idle",          # idle | running | done | failed | cancelled
            "started_at": None,
            "finished_at": None,
            "alerts_sent": None,
            # NullNotifier reports success, so a cycle with no Telegram
            # configured still counts "sent" alerts. Without this flag the
            # summary would claim deliveries that never left the process.
            "alerts_enabled": True,
            "error": None,
            "stage": None,
            "processed": 0,
            "log": [],
        }

    # -- inspection --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        """A copy, so a caller iterating the log cannot see it mutate."""
        state = dict(self._state)
        state["log"] = list(state["log"])
        state["running"] = self.running
        return state

    # -- progress, called from inside the cycle ----------------------------

    #: Keeps the newest lines and bounds memory on a long run.
    MAX_LOG_LINES = 60

    def note(self, message: str, *, stage: str | None = None) -> None:
        if stage:
            self._state["stage"] = stage
        self._state["log"].append(f"{_now()}  {message}")
        del self._state["log"][:-self.MAX_LOG_LINES]

    def count_processed(self) -> None:
        self._state["processed"] += 1

    # -- control -----------------------------------------------------------

    def start(
        self, factory: Callable[[], Awaitable[int]], *, alerts_enabled: bool = True
    ) -> bool:
        """Begin a cycle. Returns False if one is already in flight.

        ``factory`` is called here rather than the coroutine being passed in,
        so a refused start does not leave an un-awaited coroutine behind.
        """
        if self.running:
            return False

        self._state.update(status="running", started_at=_now(), finished_at=None,
                           alerts_sent=None, error=None, stage="starting",
                           processed=0, log=[], alerts_enabled=alerts_enabled)
        self.note("cycle started")
        self._task = asyncio.create_task(self._supervise(factory))
        return True

    async def _supervise(self, factory: Callable[[], Awaitable[int]]) -> None:
        try:
            sent = await factory()
        except asyncio.CancelledError:
            self._state.update(status="cancelled", finished_at=_now(), stage=None)
            self.note("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — the whole point is to record it
            # A failing cycle must show the reason. Huispedia answering 403 and
            # "no new listings" look identical from the outside otherwise.
            log.exception("manual cycle failed")
            self._state.update(status="failed", finished_at=_now(), stage=None,
                               error=f"{type(exc).__name__}: {exc}")
            self.note(f"failed: {exc}")
        else:
            self._state.update(status="done", finished_at=_now(), stage=None,
                               alerts_sent=sent)
            delivered = "alert(s) sent" if self._state["alerts_enabled"] \
                else "would have alerted (no notifier configured)"
            self.note(f"cycle complete: {sent} {delivered}")

    async def cancel(self) -> bool:
        """Stop the in-flight cycle. Returns False if there was nothing to stop."""
        if not self.running:
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        # Settle the state here rather than trusting the handler inside
        # _supervise. A task cancelled before its body ever ran never reaches
        # that handler, which would leave status "running" for good while
        # `running` reported False — the exact contradiction this class exists
        # to prevent.
        if self._state["status"] == "running":
            self._state.update(status="cancelled", finished_at=_now(), stage=None)
            self.note("cancelled before the cycle started")
        return True
