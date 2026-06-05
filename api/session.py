"""api/session.py — in-memory run sessions + the async-HITL handoff primitives.

One `Session` per tailoring run, keyed by run_id. It holds only the *volatile*
coordination state the SSE stream and the HITL endpoints need; the durable pipeline
artifacts live on disk in `outputs/<run_id>/` (the checkpoint substrate, D-07/R-06)
exactly as in a CLI run. Sessions are GC'd by TTL so a long-lived server doesn't leak.

The pipeline runs in a background thread (UI Step 3). Two cross-thread channels:

1. **Events (pipeline thread → SSE):** `add_event` appends to a buffer AND wakes any
   waiting consumer via a `threading.Condition`. The SSE generator (Step 3) replays
   the buffer to a late subscriber, then blocks on the condition for new events — so
   no event is missed regardless of when the browser connects.
2. **HITL decision (HITL endpoint → paused pipeline thread):** the pipeline calls
   `wait_hitl(checkpoint, payload)` and blocks; the `POST /hitl` endpoint calls
   `submit_hitl(response)` to hand the human's decision back and unblock it. One
   pending checkpoint at a time (the pipeline is paused while awaiting it).

Both channels are plain `threading` primitives — correct for the thread↔threadpool
model FastAPI uses, and framework-agnostic (the same Session serves a future WS/poll
transport without change).
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Session", "SessionStore", "SessionError"]

# Status lifecycle: created → running ⇄ awaiting_hitl → complete | error | stopped
STATUSES = {"created", "running", "awaiting_hitl", "complete", "error", "stopped"}
TERMINAL = {"complete", "error", "stopped"}


class SessionError(RuntimeError):
    """Raised on an invalid session transition (e.g. submitting HITL when none pending)."""


@dataclass
class Session:
    run_id: str
    mode: str = "demo"
    status: str = "created"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    events: list[dict] = field(default_factory=list)   # ordered, append-only; SSE replay buffer
    hitl_pending: dict | None = None                   # {checkpoint, payload} while awaiting a human
    result: dict | None = None                         # run_pipeline summary on completion
    error: str | None = None

    # Cross-thread coordination (not serialised).
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _hitl_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _hitl_response: dict | None = field(default=None, repr=False)

    # -- events: pipeline thread → SSE ------------------------------------- #

    def add_event(self, event: dict) -> dict:
        """Append an event and wake any SSE consumer. Returns the stored event
        (with a monotonic `seq` added so consumers can resume without duplication)."""
        with self._cond:
            stored = {"seq": len(self.events), **event}
            self.events.append(stored)
            self.updated_at = time.time()
            self._cond.notify_all()
            return stored

    def events_since(self, seq: int, *, timeout: float = 15.0) -> list[dict]:
        """Return events with seq >= `seq`, blocking up to `timeout` for new ones if
        none are buffered yet. Empty list on timeout (lets the SSE loop send a keep-alive)."""
        with self._cond:
            if seq >= len(self.events) and self.status not in TERMINAL:
                self._cond.wait(timeout)
            return self.events[seq:]

    def set_status(self, status: str) -> None:
        if status not in STATUSES:
            raise SessionError(f"unknown status {status!r}")
        with self._cond:
            self.status = status
            self.updated_at = time.time()
            self._cond.notify_all()

    # -- HITL: endpoint ⇄ paused pipeline thread --------------------------- #

    def wait_hitl(self, checkpoint: str, payload: dict, *, timeout: float | None = None) -> dict:
        """Called by the pipeline thread: publish the checkpoint and block until the
        human responds via `submit_hitl`. Returns the response dict."""
        self.hitl_pending = {"checkpoint": checkpoint, "payload": payload}
        self._hitl_response = None
        self._hitl_event.clear()
        self.set_status("awaiting_hitl")
        self.add_event({"type": "hitl_ready", "checkpoint": checkpoint, "payload": payload})
        if not self._hitl_event.wait(timeout):
            raise SessionError(f"HITL '{checkpoint}' timed out")
        self.hitl_pending = None
        self.set_status("running")
        return self._hitl_response or {}

    def submit_hitl(self, response: dict) -> None:
        """Called by the HITL endpoint: hand the human's decision to the paused
        pipeline thread. Raises if no checkpoint is currently awaiting input."""
        if self.hitl_pending is None or self.status != "awaiting_hitl":
            raise SessionError("no HITL checkpoint is awaiting input")
        self._hitl_response = response
        self._hitl_event.set()

    # -- serialisation (volatile primitives excluded) --------------------- #

    def public(self) -> dict:
        """JSON-safe snapshot for API responses (no threading objects)."""
        return {
            "run_id": self.run_id, "mode": self.mode, "status": self.status,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "event_count": len(self.events), "hitl_pending": self.hitl_pending,
            "result": self.result, "error": self.error,
        }


class SessionStore:
    """Thread-safe registry of live sessions, with filesystem TTL cleanup.

    `base_dir` is where per-session ephemeral state would live (tmp/<run_id>/); the
    pipeline's durable outputs are separate (outputs/<run_id>/). `cleanup_expired`
    drops terminal sessions older than the TTL and removes their tmp dir."""

    def __init__(self, base_dir: str | Path = "tmp", ttl_seconds: float = 3600.0):
        self.base_dir = Path(base_dir)
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, run_id: str, *, mode: str = "demo") -> Session:
        with self._lock:
            if run_id in self._sessions:
                raise SessionError(f"session {run_id!r} already exists")
            sess = Session(run_id=run_id, mode=mode)
            self._sessions[run_id] = sess
            (self.base_dir / run_id).mkdir(parents=True, exist_ok=True)
            return sess

    def get(self, run_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(run_id)

    def require(self, run_id: str) -> Session:
        sess = self.get(run_id)
        if sess is None:
            raise SessionError(f"no session {run_id!r}")
        return sess

    def list(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def delete(self, run_id: str) -> None:
        with self._lock:
            self._sessions.pop(run_id, None)
        shutil.rmtree(self.base_dir / run_id, ignore_errors=True)

    def cleanup_expired(self, *, now: float | None = None) -> list[str]:
        """Remove terminal sessions older than the TTL. Returns the removed run_ids."""
        now = time.time() if now is None else now
        removed = []
        for sess in self.list():
            if sess.status in TERMINAL and (now - sess.updated_at) > self.ttl_seconds:
                self.delete(sess.run_id)
                removed.append(sess.run_id)
        return removed
