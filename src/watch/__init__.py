"""WatchManager — auto-record meetings from your calendar AND impromptu
calls that start without one.

Long-running thread that polls Calendar.app every 60 s, schedules
1-min-before notifications with Skip action, drives /start /stop in
response to event boundaries + meeting-active signals.

Two recording paths:
  - Scheduled: a calendar event approaches → 1-min warning → record at
    start time → end ~10 s after the conferencing app goes away (fast
    confirm: switches to 5 s polling on the first inactive read).
  - Instant:   meeting-active flips true outside any scheduled window,
    stable for one 30 s poll → confirmation notification (Skip option,
    default = record after 60 s) → recording for the same end criteria.
    Skipping an instant meeting installs a 30-min cooldown so re-joining
    the same call doesn't re-prompt.

Lifecycle:
  start()      spawn the watcher thread (idempotent)
  stop()       signal exit; an in-flight recording is NOT stopped
  is_running() / status_summary() / skip_next() for the REPL surface

State per event (keyed by stable EventKit id, or `instant-<ts>`):

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
  - Instant detection is suppressed while a scheduled event is in its
    notification/recording window (or within 2 min of starting) so the
    scheduled flow always wins.
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

# Fast-confirm cadence. The instant we see one "inactive" poll while a
# recording is in flight, we drop the polling interval from ACTIVE_POLL_S
# to FAST_END_POLL_S and only require MEETING_END_QUIET_TICKS consecutive
# inactives to fire /stop. End-of-meeting detection becomes 10-15 s
# instead of 2 min — close to Granola's "press End → recording stops"
# feeling — without burning CPU on fast polling for the whole call.
#
# Tightened browser URL regexes (in MeetinkAgent) make false-positive
# inactive blips much rarer: Meet/Zoom/Teams all redirect away from
# their room-code URLs the instant the user presses End, so the first
# inactive poll after End is almost always real.
FAST_END_POLL_S = 5.0
MEETING_END_QUIET_TICKS = 2

# Instant-meeting detection.
#   CONFIRM_TICKS    — consecutive active polls before fire. 1 = 30 s of
#                       stable activity. Lower numbers catch calls faster
#                       but raise the false-positive risk of brief mic
#                       tests / "joining to check audio" actions.
#   SCHEDULED_BUFFER — don't trip instant detection if a scheduled event
#                       starts within this many seconds (the scheduled
#                       path owns it).
#   SKIP_COOLDOWN    — after Skip on an instant notification, suppress
#                       further instant detection for this long. Without
#                       it, the same call would re-prompt every 30 s.
#   END_COOLDOWN     — after an instant recording stops, suppress
#                       re-detection. Catches users wrapping a call where
#                       the conferencing app re-activates briefly.
INSTANT_CONFIRM_TICKS = 1
INSTANT_SCHEDULED_BUFFER_S = 120
INSTANT_SKIP_COOLDOWN_S = 1800
INSTANT_END_COOLDOWN_S = 300


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
    # Set on synthetic events created for instant meetings. None for
    # calendar-backed events. Populated with the source label
    # (zoom/meet/teams/webex/...) the agent flagged at detection.
    detected_source: Optional[str] = None

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
# Auto-sensitivity: map attendee count to a diarize-server preset.
#
# Scheduled events know their attendees from EventKit, so we can pick the
# right tuning before /start fires:
#   1-2 → focused  (1:1s — wide MARGIN to avoid bob/flavio confusion;
#                   low CLUSTER_THRESHOLD so one voice = one cluster)
#   3-5 → default  (small team meetings — balanced)
#   6+  → strict   (large meetings — preserve distinct voices, avoid
#                   misnaming a stranger as someone enrolled)
#
# Best-effort: a 2 s POST to the diarize-server. Failure (server off, user
# disabled diarize) silently no-ops — sensitivity tuning never blocks a
# recording. Instant meetings skip this entirely (no attendee signal).
# ---------------------------------------------------------------------------

def _pick_preset_from_attendees(attendees: list) -> str:
    n = max(1, len(attendees))
    if n <= 2:
        return "focused"
    if n <= 5:
        return "default"
    return "strict"


def _apply_sensitivity_preset(mode: str) -> bool:
    try:
        import urllib.request
        port = int(os.environ.get("MEETINK_DIARIZE_PORT", "8179"))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/sensitivity?mode={mode}",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def _list_diarize_profiles() -> list[str]:
    """Names of every enrolled profile on the diarize-server. Empty list
    if the server is off or returns nothing — caller treats that as
    'no profiles to whitelist against' and skips."""
    try:
        import urllib.request
        port = int(os.environ.get("MEETINK_DIARIZE_PORT", "8179"))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/profiles", timeout=2,
        ) as r:
            data = json.loads(r.read())
        return [p["name"] for p in data.get("profiles", [])]
    except Exception:
        return []


def _match_attendees_to_profiles(attendees: list) -> list[str]:
    """Pick the subset of enrolled profiles whose name appears as a
    token in any attendee's name or email. Token = anything separated by
    whitespace, dots, @, plus, hyphen, underscore. Word-boundary match
    avoids false-positives like 'alex' matching 'alexandra'."""
    import re
    profile_names = _list_diarize_profiles()
    if not profile_names or not attendees:
        return []
    haystack: set[str] = set()
    for a in attendees:
        for v in (a.get("name", ""), a.get("email", "")):
            for tok in re.split(r"[\s.,@+\-_/]+", v.lower()):
                if tok:
                    haystack.add(tok)
    return [p for p in profile_names if p.lower() in haystack]


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
    # /watch is an unattended path — the user is likely in the meeting,
    # not at the REPL. Suppress the auto-tail window that manual /start
    # opens; the live transcript is one /watch tail away if they want it.
    env = {**os.environ, "MEETINK_NO_TAIL": "1", **env_extras}
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
        # Instant detection state. _instant_streak counts consecutive
        # active polls; _instant_pending guards against double-firing
        # the confirmation notification while a worker is still waiting
        # for a click. Cooldowns suppress detection after Skip / end.
        self._instant_streak: int = 0
        self._instant_pending: bool = False
        self._last_meeting_active: dict = {
            "active": False, "source": None, "signals": [],
        }
        self._skip_cooldown_until: float = 0.0
        self._end_cooldown_until: float = 0.0

    # -- lifecycle ----------------------------------------------------------

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.is_running():
                return False
            self._stop_event.clear()
            # Reset transient state so a stop/start cycle clears any
            # leftover cooldowns (a user may /watch off after misclicking
            # Skip; restarting should let detection trigger again).
            self._inactive_streak = 0
            self._instant_streak = 0
            self._instant_pending = False
            self._skip_cooldown_until = 0.0
            self._end_cooldown_until = 0.0
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
            rec_ev = (
                self._events.get(self._currently_recording)
                if self._currently_recording else None
            )
            return {
                "running": self.is_running(),
                "recording_id": self._currently_recording,
                "recording_title": rec_ev.title if rec_ev else None,
                "recording_source": rec_ev.detected_source if rec_ev else None,
                "instant_pending": self._instant_pending,
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

        # Single meeting-active poll per polling window. Cadence adapts:
        #   - Steady state: ACTIVE_POLL_S (30 s) — keeps CPU/battery cost
        #     low while no transition is being decided.
        #   - End confirmation: FAST_END_POLL_S (5 s) — kicks in as soon
        #     as we see a single inactive poll while recording, so we can
        #     fire /stop within ~10 s of the user pressing End instead of
        #     waiting another full 30 s for the next slow poll.
        # Result is cached on the manager; both end-detection (currently
        # recording) and instant-detection (idle) consume it. Maintains
        # _inactive_streak / _instant_streak counters in lockstep.
        with self._lock:
            in_fast_confirm = (
                self._currently_recording is not None
                and self._inactive_streak >= 1
            )
        poll_interval = FAST_END_POLL_S if in_fast_confirm else ACTIVE_POLL_S
        if now_wall - self._last_active_poll > poll_interval:
            self._poll_meeting_active()

        # Recording start/stop based on event boundaries + meeting-active.
        # Done outside the lock when possible (subprocess calls).
        self._maybe_start_recording(now)
        if self._currently_recording is not None:
            self._maybe_end_recording(now, now_wall)
        else:
            self._maybe_start_instant_recording(now, now_wall)

        # Expire events whose window has long passed and never recorded.
        with self._lock:
            for e in self._events.values():
                if e.status in (EventStatus.PENDING, EventStatus.NOTIFIED,
                                EventStatus.DEFERRED):
                    if (now - e.end).total_seconds() > EXPIRE_AFTER_END_S:
                        e.status = EventStatus.EXPIRED

    # -- meeting-active polling --------------------------------------------

    def _poll_meeting_active(self) -> None:
        """One agent shell-out per ACTIVE_POLL_S. Updates both streak
        counters atomically so they stay consistent with each other —
        active poll increments _instant_streak / resets _inactive_streak,
        inactive poll does the opposite."""
        result = _agent_meeting_active()
        with self._lock:
            self._last_active_poll = time.time()
            self._last_meeting_active = result
            if result.get("active"):
                self._inactive_streak = 0
                self._instant_streak += 1
            else:
                self._inactive_streak += 1
                self._instant_streak = 0

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
                # Synthetic instant-meeting events are owned by the
                # _maybe_start_instant_recording worker thread — that
                # path handles its own notification + /start. If this
                # sweep also picks them up, two parallel /start calls
                # race and one logs "/start failed: Already recording".
                and e.detected_source is None
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

        # Auto-tune diarize sensitivity from the attendee count before
        # /start fires (so the very first /identify call hits the right
        # preset). No-ops cleanly if diarize-server is off.
        preset = _pick_preset_from_attendees(chosen.attendees)
        if _apply_sensitivity_preset(preset):
            print(
                f"[watch] sensitivity → {preset} "
                f"({len(chosen.attendees)} attendees, "
                f"{chosen.title!r})",
                file=sys.stderr,
            )

        # Restrict /identify to enrolled profiles whose names match the
        # attendee list. Passed via MEETINK_WHITELIST so cmd_start sees
        # the same source of truth as `/start alex stacey` — empty env
        # var means cmd_start clears any stale whitelist for a clean
        # slate. Eliminates the failure mode where Mike's voice scores
        # 0.89 against ALEX in a meeting Alex isn't even in.
        matched = _match_attendees_to_profiles(chosen.attendees)
        env_extras = self._metadata_env(chosen)
        env_extras["MEETINK_WHITELIST"] = ",".join(matched)
        if matched:
            print(
                f"[watch] whitelist → {matched} "
                f"(matched from {chosen.title!r})",
                file=sys.stderr,
            )
        else:
            print(
                f"[watch] whitelist cleared "
                f"(no enrolled attendees in {chosen.title!r})",
                file=sys.stderr,
            )

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
        """Decide whether to stop the current recording. Reads the
        cached _inactive_streak (maintained by _poll_meeting_active),
        so this is cheap to call every loop tick.

        Process detection > silence detection — brainstorm gaps don't
        false-positive (we end only when the conferencing app has been
        absent for MEETING_END_QUIET_TICKS consecutive polls; cadence
        drops to FAST_END_POLL_S once we've seen the first inactive)."""
        with self._lock:
            if self._inactive_streak < MEETING_END_QUIET_TICKS:
                return
            recording_id = self._currently_recording

        # Drop the lock to shell out /stop
        _stop_recording_subprocess()
        with self._lock:
            was_instant = False
            if recording_id and recording_id in self._events:
                ev = self._events[recording_id]
                ev.status = EventStatus.COMPLETED
                was_instant = ev.detected_source is not None
            self._currently_recording = None
            self._inactive_streak = 0
            self._instant_streak = 0
            # Brief blips after wrap-up (e.g. user switches Zoom windows
            # while saying goodbye) shouldn't immediately re-arm instant
            # detection on the same call.
            if was_instant:
                self._end_cooldown_until = (
                    time.time() + INSTANT_END_COOLDOWN_S
                )

    # -- instant-meeting detection ----------------------------------------

    def _maybe_start_instant_recording(
        self, now: datetime, now_wall: float
    ) -> None:
        """Detect calls that start without a calendar event and offer
        to record. Suppressed during scheduled-event windows + cooldowns;
        requires INSTANT_CONFIRM_TICKS active polls before firing.

        Notification is fire-and-forget: a worker thread blocks on the
        agent until the user clicks Skip or the timeout expires (default
        = record). _instant_pending guards against re-entry while the
        user is still deciding."""
        with self._lock:
            if self._instant_pending:
                return
            if now_wall < self._skip_cooldown_until:
                return
            if now_wall < self._end_cooldown_until:
                return
            # Don't compete with the scheduled path. Anything notified /
            # deferred / recording owns the moment; anything pending
            # within INSTANT_SCHEDULED_BUFFER_S of starting also does
            # (it's about to claim the next minute anyway).
            for e in self._events.values():
                if e.status in (EventStatus.NOTIFIED, EventStatus.DEFERRED,
                                EventStatus.RECORDING):
                    return
                if e.status == EventStatus.PENDING:
                    until_start = (e.start - now).total_seconds()
                    if 0 < until_start < INSTANT_SCHEDULED_BUFFER_S:
                        return

            if self._instant_streak < INSTANT_CONFIRM_TICKS:
                return
            active = self._last_meeting_active
            if not active.get("active"):
                return

            # Reserve the slot. Reset streak so a Skip-then-stay-in-call
            # doesn't immediately re-arm; the cooldown set by the worker
            # is what governs re-entry from here on.
            self._instant_pending = True
            self._instant_streak = 0
            source = active.get("source") or "meeting"
            signals = list(active.get("signals", []))

            instant_id = f"instant-{int(now_wall)}"
            ev = WatchedEvent(
                id=instant_id,
                title="(instant meeting)",
                start=now,
                # Big window so EXPIRE_AFTER_END_S never trips while the
                # call is in flight. Real end is driven by meeting-active.
                end=now + timedelta(hours=8),
                attendees=[],
                rsvp_status="none",
                location="",
                notes=f"detected via {', '.join(signals)}" if signals else "",
                calendar_title="",
                status=EventStatus.NOTIFIED,
                notified_at=now,
                detected_source=source,
            )
            self._events[instant_id] = ev

        body = f"Detected {source} call. Recording in 60 s — Skip to ignore."

        def worker() -> None:
            response = _agent_notify(
                title="meetink — instant meeting",
                body=body,
                actions=["Skip"],
                default="Continue",
                timeout=NOTIFY_TIMEOUT_S,
            )
            if response.strip().lower() == "skip":
                with self._lock:
                    ev.status = EventStatus.SKIPPED
                    self._skip_cooldown_until = (
                        time.time() + INSTANT_SKIP_COOLDOWN_S
                    )
                    self._instant_pending = False
                return

            # Default / Continue / timeout → record. Re-check that we
            # aren't now blocked by a scheduled recording that started
            # while the user was deciding.
            with self._lock:
                if self._currently_recording is not None:
                    ev.status = EventStatus.EXPIRED
                    self._instant_pending = False
                    return

            # Instant meetings have no attendee signal — explicitly
            # pass an empty whitelist so cmd_start clears any stale
            # one from the previous scheduled meeting (match-all is
            # the safe default for impromptu calls).
            env_extras = self._instant_metadata_env(ev)
            env_extras["MEETINK_WHITELIST"] = ""
            ok = _start_recording_subprocess(env_extras)
            with self._lock:
                if ok:
                    ev.status = EventStatus.RECORDING
                    ev.recorded_at = _now()
                    self._currently_recording = ev.id
                    self._inactive_streak = 0
                else:
                    ev.status = EventStatus.EXPIRED
                self._instant_pending = False

        threading.Thread(target=worker, daemon=True,
                         name="meetink-instant").start()

    def _instant_metadata_env(self, e: WatchedEvent) -> dict[str, str]:
        """Header env for instant recordings. Skips scheduled-only
        fields (start/end/attendees/etc.) so the transcript header
        doesn't carry misleading values; the capture binary writes
        its own `Started:` line which is the truthful timestamp."""
        return {
            "MEETINK_EVENT_TITLE":   e.title,
            "MEETINK_EVENT_INSTANT": "true",
            "MEETINK_EVENT_SOURCE":  e.detected_source or "",
            "MEETINK_EVENT_NOTES":   e.notes,
            "MEETINK_EVENT_PROJECT": e.project or "",
        }


def get_manager() -> WatchManager:
    return WatchManager.get()
