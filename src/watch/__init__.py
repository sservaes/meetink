"""WatchManager — auto-record meetings from your calendar.

Long-running thread that polls Calendar.app every 60 s, schedules
1-min-before notifications with Skip action, drives /start /stop in
response to event boundaries + meeting-active signals.

Lifecycle:
  start()      spawn the watcher thread (idempotent)
  stop()       signal exit; an in-flight recording is NOT stopped
  is_running() / status_summary() / skip_next() for the REPL surface

State per event (keyed by stable EventKit id):

  pending    — in calendar, hasn't fired yet
  notified   — 1-min warning sent; recording will start at event time
  skipped    — user clicked Skip on the notification
  recording  — actively capturing
  deferred   — should record but blocked by a current recording
  completed  — recording finished
  expired    — window passed without recording

Switching policy:
  - If we're already recording when a new event's start time arrives,
    the new event becomes `deferred`. We never interrupt an active
    capture mid-conversation just because the calendar said so.
  - Recording only ends when meeting-active goes false for >=
    MEETING_END_QUIET_TICKS consecutive polls (silence-derived end
    is replaced by Zoom/Meet/Teams process detection so brainstorms
    don't trigger a false stop).
  - When a recording ends, we re-evaluate deferred events: if any are
    still in their window, the highest-scored one starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


MK_HOME = Path(os.environ.get("MEETINK_HOME", os.path.expanduser("~/.meetink")))
MK_AGENT = MK_HOME / "bin" / "MeetinkAgent.app" / "Contents" / "MacOS" / "meetink-agent"

# Cadence knobs. The watcher's own loop tick is fast (5 s) so state
# transitions feel snappy, but expensive checks (calendar poll, agent
# subprocess) happen on their own slower cadences.
LOOP_TICK_S = 5.0
CALENDAR_POLL_S = 60.0
ACTIVE_POLL_S = 30.0
NOTIFY_LEAD_S = 60.0          # 1-min-before notification
NOTIFY_TIMEOUT_S = 60         # how long to wait for a Skip click
EXPIRE_AFTER_END_S = 600      # 10 min past end with no record = expired

# Number of consecutive meeting-active=false polls required to declare
# the current call ended. With ACTIVE_POLL_S=30 s, 4 ticks = 2 min of
# the conferencing app being absent. Avoids brief Zoom hiccups stopping
# a real recording, while still catching post-meeting cleanup quickly.
MEETING_END_QUIET_TICKS = 4


class EventStatus(Enum):
    PENDING = "pending"
    NOTIFIED = "notified"
    SKIPPED = "skipped"
    RECORDING = "recording"
    DEFERRED = "deferred"
    COMPLETED = "completed"
    EXPIRED = "expired"


@dataclass
class WatchedEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    attendees: list = field(default_factory=list)
    rsvp_status: str = "none"
    location: str = ""
    notes: str = ""
    calendar_title: str = ""
    status: EventStatus = EventStatus.PENDING
    notified_at: Optional[datetime] = None
    recorded_at: Optional[datetime] = None
    project: Optional[str] = None  # resolved by router at notify time

    def score(self) -> int:
        """Higher = more deserving of being recorded when conflicting
        events are simultaneous. Tuneable; tied to user's stated
        overlap policy: accepted RSVP > with attendees > earliest."""
        s = 0
        if self.rsvp_status == "accepted":
            s += 100
        elif self.rsvp_status == "tentative":
            s += 10
        # Each attendee adds a point, capped — clearly multi-person
        # events should beat solo focus blocks.
        s += min(50, len(self.attendees))
        return s


def _parse_iso(s: str) -> datetime:
    """Parse ISO-8601 with timezone. EventKit emits with `Z` suffix; we
    coerce to a +00:00 datetime so all comparisons are in UTC."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Agent shell-outs (parsed JSON; each call <1 s in the steady state)
# ---------------------------------------------------------------------------

def _agent_events(hours: int = 8) -> list[dict]:
    if not MK_AGENT.is_file():
        return []
    try:
        proc = subprocess.run(
            [str(MK_AGENT), "events", "--hours", str(hours)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
        return json.loads(proc.stdout)
    except Exception:
        return []


def _agent_meeting_active() -> dict:
    if not MK_AGENT.is_file():
        return {"active": False, "source": None, "signals": []}
    try:
        proc = subprocess.run(
            [str(MK_AGENT), "meeting-active"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return {"active": False, "source": None, "signals": []}
        return json.loads(proc.stdout)
    except Exception:
        return {"active": False, "source": None, "signals": []}


def _agent_notify(title: str, body: str, actions: list[str],
                  default: str, timeout: int) -> str:
    """Blocking call. Caller usually runs this on a dedicated worker
    thread because the agent waits up to `timeout` seconds for a click."""
    if not MK_AGENT.is_file():
        return default
    try:
        proc = subprocess.run(
            [str(MK_AGENT), "notify",
             "--title", title,
             "--body", body,
             "--actions", ",".join(actions),
             "--default", default,
             "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=timeout + 15,
        )
        return proc.stdout.strip() or default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Launcher dispatch (re-uses cmd_start / cmd_stop / cmd_project)
# ---------------------------------------------------------------------------

LAUNCHER = Path(__file__).resolve().parent.parent.parent / "bin" / "meetink"


def _project_use(name: str) -> None:
    if not name:
        return
    subprocess.run([str(LAUNCHER), "project", "use", name],
                   check=False, capture_output=True)


def _start_recording_subprocess(env_extras: dict[str, str]) -> bool:
    env = {**os.environ, **env_extras}
    proc = subprocess.run(
        [str(LAUNCHER), "start"],
        check=False, capture_output=True, env=env, text=True,
    )
    if proc.returncode != 0:
        print(f"[watch] /start failed: {proc.stderr.strip()}",
              file=sys.stderr)
        return False
    return True


def _stop_recording_subprocess() -> None:
    subprocess.run([str(LAUNCHER), "stop"],
                   check=False, capture_output=True)


# ---------------------------------------------------------------------------
# WatchManager
# ---------------------------------------------------------------------------

class WatchManager:
    _instance: "WatchManager | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "WatchManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._events: dict[str, WatchedEvent] = {}
        self._currently_recording: Optional[str] = None
        self._inactive_streak: int = 0
        self._last_calendar_poll: float = 0.0
        self._last_active_poll: float = 0.0

    # -- lifecycle ----------------------------------------------------------

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.is_running():
                return False
            self._stop_event.clear()
            self._inactive_streak = 0
            self._thread = threading.Thread(
                target=self._loop, name="meetink-watch", daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Signal the watcher to exit. An in-flight recording is NOT
        stopped — that's the user's call (they may want it to keep
        running through /watch off)."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)

    # -- REPL surface -------------------------------------------------------

    def status_summary(self) -> dict:
        """Snapshot for /watch status. Only the next handful of upcoming
        events to keep output bounded."""
        with self._lock:
            now = _now()
            upcoming = sorted(
                [e for e in self._events.values()
                 if e.status in (EventStatus.PENDING, EventStatus.NOTIFIED,
                                 EventStatus.DEFERRED)
                 and e.end > now],
                key=lambda e: e.start,
            )
            return {
                "running": self.is_running(),
                "recording_id": self._currently_recording,
                "recording_title": (
                    self._events[self._currently_recording].title
                    if self._currently_recording
                       and self._currently_recording in self._events
                    else None
                ),
                "upcoming": [
                    {
                        "id": e.id,
                        "title": e.title,
                        "start": e.start.astimezone().strftime("%H:%M"),
                        "end": e.end.astimezone().strftime("%H:%M"),
                        "status": e.status.value,
                        "project": e.project,
                        "rsvp": e.rsvp_status,
                    }
                    for e in upcoming[:8]
                ],
            }

    def skip_next(self) -> Optional[str]:
        """Mark the soonest pending/notified event as skipped. Returns
        the title that was skipped, or None if nothing was eligible."""
        with self._lock:
            now = _now()
            candidates = sorted(
                [e for e in self._events.values()
                 if e.status in (EventStatus.PENDING, EventStatus.NOTIFIED)
                 and e.end > now],
                key=lambda e: e.start,
            )
            if not candidates:
                return None
            target = candidates[0]
            target.status = EventStatus.SKIPPED
            return target.title

    # -- main loop ----------------------------------------------------------

    def _loop(self) -> None:
        try:
            self._poll_calendar()
        except Exception as e:
            print(f"[watch] initial calendar poll failed: {e}",
                  file=sys.stderr)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[watch] tick failed: {e}", file=sys.stderr)
            self._stop_event.wait(LOOP_TICK_S)

    def _tick(self) -> None:
        now = _now()
        now_wall = time.time()

        # Periodic calendar refresh
        if now_wall - self._last_calendar_poll > CALENDAR_POLL_S:
            self._poll_calendar()

        # Notifications: fire 1-min-before for any pending event whose
        # start is within NOTIFY_LEAD_S. Each notification spawns its
        # own worker thread because _agent_notify blocks on user click.
        with self._lock:
            for e in list(self._events.values()):
                if e.status != EventStatus.PENDING:
                    continue
                lead = (e.start - now).total_seconds()
                if 0 < lead <= NOTIFY_LEAD_S:
                    self._fire_notification(e)

        # Recording start/stop based on event boundaries + meeting-active.
        # Done outside the lock when possible (subprocess calls).
        self._maybe_start_recording(now)
        if self._currently_recording is not None:
            self._maybe_end_recording(now, now_wall)

        # Expire events whose window has long passed and never recorded.
        with self._lock:
            for e in self._events.values():
                if e.status in (EventStatus.PENDING, EventStatus.NOTIFIED,
                                EventStatus.DEFERRED):
                    if (now - e.end).total_seconds() > EXPIRE_AFTER_END_S:
                        e.status = EventStatus.EXPIRED

    # -- calendar refresh ---------------------------------------------------

    def _poll_calendar(self) -> None:
        events = _agent_events(hours=8)
        with self._lock:
            self._last_calendar_poll = time.time()
            seen = set()
            for raw in events:
                eid = raw["id"]
                seen.add(eid)
                if eid in self._events:
                    e = self._events[eid]
                    # Update mutable fields but preserve status. A
                    # rescheduled event would change start/end here;
                    # re-fire notification path will pick that up.
                    e.title = raw["title"]
                    e.start = _parse_iso(raw["start"])
                    e.end = _parse_iso(raw["end"])
                    e.attendees = raw.get("attendees", [])
                    e.rsvp_status = raw.get("rsvpStatus", "none")
                    e.location = raw.get("location", "")
                    e.notes = raw.get("notes", "")
                    e.calendar_title = raw.get("calendarTitle", "")
                else:
                    self._events[eid] = WatchedEvent(
                        id=eid,
                        title=raw["title"],
                        start=_parse_iso(raw["start"]),
                        end=_parse_iso(raw["end"]),
                        attendees=raw.get("attendees", []),
                        rsvp_status=raw.get("rsvpStatus", "none"),
                        location=raw.get("location", ""),
                        notes=raw.get("notes", ""),
                        calendar_title=raw.get("calendarTitle", ""),
                    )
            # Drop events that are no longer in the calendar (cancelled
            # or moved past the lookahead window) AND haven't yet been
            # acted on. Keep recordings/skipped/expired so the status
            # report stays meaningful.
            for eid in list(self._events.keys()):
                if eid in seen:
                    continue
                if self._events[eid].status in (
                    EventStatus.PENDING, EventStatus.NOTIFIED,
                    EventStatus.DEFERRED,
                ):
                    del self._events[eid]

    # -- notification ------------------------------------------------------

    def _fire_notification(self, e: WatchedEvent) -> None:
        """Marks the event NOTIFIED, resolves project, dispatches the
        notification on its own thread (since the agent blocks until
        click or timeout)."""
        # Project routing (LLM) — do it once per event, cached on the
        # event itself. Cheap when no projects exist, ~200ms with local
        # backend, ~2-3s with claude.
        try:
            from .router import resolve_project
            e.project = resolve_project(e)
        except Exception as exc:
            print(f"[watch] project routing failed: {exc}", file=sys.stderr)
            e.project = None

        e.status = EventStatus.NOTIFIED
        e.notified_at = _now()

        body_parts = [f"In 1 min: {e.title}"]
        if e.project:
            body_parts.append(f"→ {e.project}")
        body = "  ".join(body_parts)

        ev = e  # capture for the closure
        def worker():
            response = _agent_notify(
                title="meetink — auto-record",
                body=body,
                actions=["Skip"],
                default="Continue",
                timeout=NOTIFY_TIMEOUT_S,
            )
            if response.strip().lower() == "skip":
                with self._lock:
                    if ev.status == EventStatus.NOTIFIED:
                        ev.status = EventStatus.SKIPPED
        threading.Thread(target=worker, daemon=True,
                         name=f"meetink-notify-{e.id[:8]}").start()

    # -- recording start ---------------------------------------------------

    def _maybe_start_recording(self, now: datetime) -> None:
        """At each tick, pick at most one event to start. Defers any
        others whose start time has arrived but a recording is already
        in flight."""
        if self._currently_recording is not None:
            with self._lock:
                # Mark anything that should have started but can't, as
                # deferred (rather than letting it sit notified forever).
                for e in self._events.values():
                    if e.status == EventStatus.NOTIFIED and \
                       e.start <= now <= e.end:
                        e.status = EventStatus.DEFERRED
            return

        with self._lock:
            ready = [
                e for e in self._events.values()
                if e.status in (EventStatus.NOTIFIED, EventStatus.DEFERRED)
                and e.start <= now <= e.end
            ]
            if not ready:
                return
            # Conflict resolution: highest score wins. Ties broken by
            # earliest start. _agent_notify can prompt the user on
            # genuine ties (TODO: phase 3 tie-breaker UI).
            ready.sort(key=lambda e: (-e.score(), e.start))
            chosen = ready[0]

        # Drop the lock while we shell out — both subprocesses can take
        # 1-2 s on a cold whisper-server.
        if chosen.project:
            _project_use(chosen.project)

        env_extras = self._metadata_env(chosen)
        ok = _start_recording_subprocess(env_extras)
        with self._lock:
            if ok:
                chosen.status = EventStatus.RECORDING
                chosen.recorded_at = _now()
                self._currently_recording = chosen.id
                self._inactive_streak = 0
                self._last_active_poll = time.time()
            else:
                # Mark expired so we don't keep retrying every tick.
                chosen.status = EventStatus.EXPIRED

    def _metadata_env(self, e: WatchedEvent) -> dict[str, str]:
        """Build the env dict consumed by main.swift's transcript-header
        writer (capture binary). One MEETINK_EVENT_* per metadata field."""
        attendee_strs = []
        for a in e.attendees:
            name = a.get("name", "").strip()
            email = a.get("email", "").strip()
            if name and email and name != email:
                attendee_strs.append(f"{name} <{email}>")
            elif name:
                attendee_strs.append(name)
            elif email:
                attendee_strs.append(email)
        return {
            "MEETINK_EVENT_TITLE":     e.title,
            "MEETINK_EVENT_START":     e.start.isoformat(),
            "MEETINK_EVENT_END":       e.end.isoformat(),
            "MEETINK_EVENT_ATTENDEES": ", ".join(attendee_strs),
            "MEETINK_EVENT_LOCATION":  e.location,
            "MEETINK_EVENT_NOTES":     e.notes,
            "MEETINK_EVENT_RSVP":      e.rsvp_status,
            "MEETINK_EVENT_CALENDAR":  e.calendar_title,
            "MEETINK_EVENT_PROJECT":   e.project or "",
        }

    # -- recording end ----------------------------------------------------

    def _maybe_end_recording(self, now: datetime, now_wall: float) -> None:
        """Poll meeting-active. End the recording when the conferencing
        app has been absent for MEETING_END_QUIET_TICKS consecutive
        polls. Process detection > silence detection — brainstorm
        gaps don't false-positive."""
        if now_wall - self._last_active_poll < ACTIVE_POLL_S:
            return
        result = _agent_meeting_active()
        with self._lock:
            self._last_active_poll = now_wall
            if result.get("active"):
                self._inactive_streak = 0
                return
            self._inactive_streak += 1
            if self._inactive_streak < MEETING_END_QUIET_TICKS:
                return
            recording_id = self._currently_recording

        # Drop the lock to shell out /stop
        _stop_recording_subprocess()
        with self._lock:
            if recording_id and recording_id in self._events:
                self._events[recording_id].status = EventStatus.COMPLETED
            self._currently_recording = None
            self._inactive_streak = 0


def get_manager() -> WatchManager:
    return WatchManager.get()
