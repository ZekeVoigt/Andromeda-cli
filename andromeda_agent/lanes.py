"""Lanes running alongside the turn that started them.

A synchronous `delegate` costs the sum of its lanes; three lanes that each take
40 seconds cost two minutes, and the parent sits idle for all of it. Running
them concurrently costs the longest one.

At most three children at a time (`MAX_CONCURRENT_LANES`), with staleness
thresholds of 450 seconds with no progress outside a tool and 1200 inside one,
because a tool can legitimately be slow and a model turn cannot.

**Surfaces are held, not shared.** The browser is one browser. A lane that needs
it takes an exclusive lock for its whole life, so two browser lanes queue rather
than typing into each other's page. The slot is taken *before* the surface, in
that order, always: taking the surface first and then waiting for a worker slot
is a deadlock as soon as all three slots are held by lanes waiting for the
surface.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

MAX_CONCURRENT_LANES = 3
STALE_IDLE_SECONDS = 450.0
STALE_IN_TOOL_SECONDS = 1200.0

Status = Literal["running", "done", "failed", "stale"]


@dataclass
class Lane:
    id: str
    specialist: str
    label: str
    task: str
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    status: Status = "running"
    result: Any = None
    error: str = ""
    # Updated as the lane works, so staleness is measured against progress
    # rather than against start time — a lane 15 minutes into honest work is
    # not stalled.
    last_progress_at: float = field(default_factory=time.time)
    current_tool: str = ""
    _future: Future | None = field(default=None, repr=False)

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    @property
    def is_stale(self) -> bool:
        if self.status != "running":
            return False
        idle = time.time() - self.last_progress_at
        limit = STALE_IN_TOOL_SECONDS if self.current_tool else STALE_IDLE_SECONDS
        return idle > limit

    def summary(self) -> str:
        state = "stale" if self.is_stale else self.status
        detail = f" · {self.current_tool}" if self.current_tool and state == "running" else ""
        return f"{self.id}  {state:<8} {self.specialist:<9} {int(self.elapsed)}s{detail}  {self.label}"


class LaneRegistry:
    """Every lane this session has started.

    One registry per session, shared with the tools that inspect it. Threads
    rather than processes: the work is network-bound, and a process would need
    the whole session pickled across to it.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_LANES) -> None:
        self._lanes: dict[str, Lane] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="andromeda-lane"
        )
        # Held for the whole life of a lane that needs the browser.
        self._browser_lock = threading.Lock()

    # ---- starting ---------------------------------------------------------

    def start(
        self,
        specialist: str,
        label: str,
        task: str,
        run: Callable[["Lane"], Any],
        exclusive_browser: bool = False,
    ) -> Lane:
        lane = Lane(
            id=f"l{uuid.uuid4().hex[:6]}",
            specialist=specialist,
            label=label or task[:60],
            task=task,
        )
        with self._lock:
            self._lanes[lane.id] = lane

        def body() -> Any:
            # Slot first, surface second. This function is already running on a
            # pool worker, so the slot is held; taking the browser here cannot
            # deadlock against a lane waiting for a slot.
            if exclusive_browser:
                self._browser_lock.acquire()
            try:
                return run(lane)
            finally:
                if exclusive_browser:
                    self._browser_lock.release()

        future = self._pool.submit(body)
        lane._future = future
        future.add_done_callback(lambda f: self._settle(lane, f))
        return lane

    def _settle(self, lane: Lane, future: Future) -> None:
        with self._lock:
            lane.finished_at = time.time()
            lane.current_tool = ""
            try:
                lane.result = future.result()
                lane.status = "done"
            except Exception as exc:  # noqa: BLE001 - recorded, never raised here
                lane.status = "failed"
                lane.error = str(exc)[:500]

    # ---- progress ---------------------------------------------------------

    def note_progress(self, lane: Lane, tool: str = "") -> None:
        with self._lock:
            lane.last_progress_at = time.time()
            lane.current_tool = tool

    # ---- inspecting -------------------------------------------------------

    def get(self, lane_id: str) -> Lane | None:
        with self._lock:
            return self._lanes.get((lane_id or "").strip())

    def all(self, active_only: bool = False) -> list[Lane]:
        with self._lock:
            lanes = list(self._lanes.values())
        lanes.sort(key=lambda lane: lane.started_at)
        return [lane for lane in lanes if lane.status == "running"] if active_only else lanes

    @property
    def running(self) -> list[Lane]:
        return self.all(active_only=True)

    def wait(self, lane_ids: list[str] | None, timeout: float = 0.0) -> list[Lane]:
        """Block until the named lanes finish, or until `timeout`.

        `timeout` of 0 means wait indefinitely, matching the hosted contract.
        Lanes still running when it expires are returned as they are — the
        caller reports them rather than being told nothing happened.
        """
        targets = (
            [lane for lane in self.all() if lane.id in set(lane_ids)]
            if lane_ids
            else self.running
        )
        if not targets:
            return []

        deadline = time.time() + timeout if timeout > 0 else None
        for lane in targets:
            if lane._future is None:
                continue
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            if remaining == 0.0:
                break
            try:
                lane._future.result(timeout=remaining)
            except Exception:  # noqa: BLE001 - status carries the outcome
                pass
        return targets

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
